"""Модели MCP: из них строятся JSON Schema входов и выходов тулзов.

Сюда попадают только поля, нужные клиенту. Служебные поля IACPaaS
(owner_id, code, original_id/link_id и т.п.) наружу не публикуются.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .config import MAX_DEPTH, MAX_LIMIT

# ---------- общие типы ----------

VertexType = Literal["КОРЕНЬ", "НЕТЕРМИНАЛ", "ТЕРМИНАЛ-СОРТ", "ТЕРМИНАЛ-ЗНАЧЕНИЕ"]
ValType = Literal["STRING", "INTEGER", "REAL", "BOOLEAN", "DATE", "BLOB"]
DeleteMode = Literal["deleteIR", "trashIR", "clearIR", "deleteSubGraph", "clearSubGraph"]
# float нужен для REAL (по документации это JSON-число), хотя официальная схема IACPaaS его не допускает
VertexValue = bool | int | float | str

TERMINAL_TYPES = ("ТЕРМИНАЛ-СОРТ", "ТЕРМИНАЛ-ЗНАЧЕНИЕ")
# Поля, которых нет в официальной схеме IACPaaS (additionalProperties: false): при импорте не отправляем
NON_SCHEMA_FIELDS = {"id", "specifier", "restrictor", "call_id"}

IrPath = Annotated[
    str,
    Field(
        description="Путь к ИР в «Мой Фонд» без префикса '<e-mail>/Мой Фонд/', например 'папка/Имя ИР'",
        min_length=1,
    ),
]
ConceptPath = Annotated[
    str,
    Field(description="Путь к вершине внутри ИР от корня, местоположения через '/' ('' - корень)"),
]
MetaconceptPath = Annotated[
    str,
    Field(description="Путь из меток метавершин, соответствующих вершинам в concept_path"),
]
Limit = Annotated[int, Field(ge=1, le=MAX_LIMIT, description="Максимум элементов в ответе")]
Offset = Annotated[int, Field(ge=0, le=1_000_000, description="Смещение страницы")]
Depth = Annotated[int, Field(ge=0, le=MAX_DEPTH, description="Глубина обхода (0 - только сама вершина)")]


# ---------- ИР в универсальном JSON-формате ----------


class Vertex(BaseModel):
    """Вершина (понятие) в универсальном формате. У вершины есть либо name, либо value."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = Field(None, description="Идентификатор вершины (только при экспорте, при импорте игнорируется)")
    name: str | None = Field(None, description="Имя вершины (для всех типов, кроме ТЕРМИНАЛ-ЗНАЧЕНИЕ)")
    value: VertexValue | None = Field(
        None, description="Значение (только для ТЕРМИНАЛ-ЗНАЧЕНИЕ), формат зависит от valtype"
    )
    type: VertexType
    valtype: ValType | None = Field(None, description="Тип значения (для ТЕРМИНАЛ-СОРТ и ТЕРМИНАЛ-ЗНАЧЕНИЕ)")
    meta: str = Field(description="Метка соответствующей метавершины")
    comment: str | None = None
    rcomment: str | None = Field(None, description="Комментарий входящего отношения")
    specifier: str | None = Field(None, description="Спецификатор отношения (только в метаинформации)")
    restrictor: str | None = Field(None, description="Ограничитель отношения (только в метаинформации)")
    original: str | None = Field(None, description="Путь к вершине-оригиналу, если это зависимый клон")
    link: str | None = Field(None, description="Путь к вершине, если это понятие-ссылка")
    successors: list["Vertex"] = Field(
        default_factory=list, description="Прямые потомки (только для КОРЕНЬ и НЕТЕРМИНАЛ)"
    )

    @model_validator(mode="after")
    def check_vertex(self) -> "Vertex":
        # oneOf схемы: ровно одно из name/value; по документации value - только у ТЕРМИНАЛ-ЗНАЧЕНИЕ
        if (self.name is None) == (self.value is None):
            raise ValueError("у вершины должно быть ровно одно из полей name или value")
        if (self.value is not None) != (self.type == "ТЕРМИНАЛ-ЗНАЧЕНИЕ"):
            raise ValueError("value задается только для ТЕРМИНАЛ-ЗНАЧЕНИЕ, у остальных типов - name")
        if self.valtype is not None and self.type not in TERMINAL_TYPES:
            raise ValueError("valtype задается только для ТЕРМИНАЛ-СОРТ и ТЕРМИНАЛ-ЗНАЧЕНИЕ")
        if self.successors and self.type in TERMINAL_TYPES:
            raise ValueError("потомки бывают только у КОРЕНЬ и НЕТЕРМИНАЛ")
        if self.link is not None and self.original is not None:
            raise ValueError("original не используется у понятия-ссылки (link)")
        return self


def _schema_dict(vertex: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in vertex.items() if k not in NON_SCHEMA_FIELDS}
    if out.get("type") in TERMINAL_TYPES:
        out.pop("successors", None)
    elif "successors" in out:
        out["successors"] = [_schema_dict(s) for s in out["successors"]]
    return out


class InfoResourceDocument(Vertex):
    """ИР целиком в универсальном формате: сведения об ИР и корень на одном уровне."""

    title: str = Field(description="Имя ИР, совпадает с name корня")
    path: str = Field(description="Полный путь к ИР, оканчивается на '$;'")
    call_id: str = Field("", description="ID вызова для validate_answer")
    json_type: Literal["universal"] = "universal"
    ontology: str = Field(description="Путь к метаинформации (онтологии) ИР")
    context: list[str] = Field(default_factory=list, description="Пути к контекстным ИР")
    date: str | None = Field(None, description="Дата изменения DD.MM.YYYY-hh:mm:ss.mmm (только при экспорте)")
    creation: str | None = Field(None, description="Дата создания (только при экспорте)")

    def to_import_dict(self) -> dict[str, Any]:
        """JSON для импорта в формате IACPaaS: без None, id и пустого context."""
        data = _schema_dict(self.model_dump(mode="json", exclude_none=True))
        # context есть в документации, но не в схеме: отправляем, только если он задан
        if not data.get("context"):
            data.pop("context", None)
        return data


# ---------- граф ----------
# Подграф, подсчет и запросы считаются локально по экспорту: отдельных методов в API нет


class GraphNode(BaseModel):
    id: int | None = None
    label: VertexValue = Field(description="name или value вершины")
    type: VertexType
    valtype: ValType | None = None
    meta: str | None = None
    comment: str | None = None
    concept_path: str = Field(description="Путь к вершине внутри ИР; нужен для update/delete")
    metaconcept_path: str | None = Field(
        None, description="Путь по меткам метавершин; нужен для update/delete"
    )
    depth: int | None = Field(None, description="Расстояние от начальной вершины запроса")
    link: str | None = Field(None, description="Куда ведет ссылка, если вершина - ссылка")
    original: str | None = Field(None, description="Оригинал, если вершина - зависимый клон")

    @model_serializer(mode="plain")
    def serialize_node(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.model_fields if getattr(self, name) is not None}


class GraphEdge(BaseModel):
    source: str = Field(description="concept_path родительской вершины")
    target: str = Field(description="concept_path дочерней вершины")
    rcomment: str | None = None


class Subgraph(BaseModel):
    path: str
    start: str = Field(description="concept_path начальной вершины ('' - корень ИР)")
    depth: int | None
    view: Literal["compact", "summary", "full"] = Field("summary", description="Режим вывода")
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    offset: int = Field(0, description="Смещение текущей страницы")
    total: int = Field(0, description="Всего вершин в поддереве на заданной глубине")
    has_more: bool = Field(False, description="Есть ли следующая страница")
    next_offset: int | None = Field(None, description="offset следующей страницы")
    truncated: bool = Field(False, description="True, если показана не вся выборка")
    call_id: str = Field("", description="ID вызова для validate_answer")


class GraphCounts(BaseModel):
    path: str
    start: str
    nodes: int
    edges: int
    call_id: str = Field("", description="ID вызова для validate_answer")


class QueryResult(BaseModel):
    path: str = Field(description="Путь к ИР, к которому относится результат")
    query: str
    view: Literal["compact", "summary", "full"] = Field("compact", description="Режим вывода")
    matches: list[GraphNode]
    total: int
    offset: int = 0
    has_more: bool = False
    next_offset: int | None = None
    truncated: bool = False
    call_id: str = Field("", description="ID вызова для validate_answer")


class VertexMatch(BaseModel):
    field: str = Field(description="Где найдено совпадение: label, meta или comment")
    path: str = Field(description="concept_path найденной вершины; передайте его в get_subgraph")
    node: GraphNode
    children: list[GraphNode] = Field(
        default_factory=list,
        description="Потомки найденной вершины до запрошенной глубины",
    )
    children_truncated: bool = False


class VertexLookup(BaseModel):
    path: str
    text: str
    matches: list[VertexMatch]
    total: int
    truncated: bool = False
    call_id: str = Field("", description="ID вызова для validate_answer")


class TerminalValue(BaseModel):
    property: str = Field(description="Смысловая цепочка родителей терминала без служебных номеров")
    value: VertexValue = Field(description="Имя или значение терминала")
    meta: str | None = Field(None, description="Метапонятие терминала")
    concept_path: str = Field(description="Путь к терминалу внутри ИР")
    depth: int = Field(description="Расстояние от начальной вершины запроса")


class TerminalValues(BaseModel):
    path: str
    start: str = Field(description="concept_path начальной вершины ('' - корень ИР)")
    depth: int
    values: list[TerminalValue]
    total: int = 0
    offset: int = Field(0, description="Смещение текущей страницы")
    has_more: bool = False
    next_offset: int | None = None
    truncated: bool = False
    call_id: str = Field("", description="ID вызова для validate_answer")


# ---------- запись ----------


class OperationResult(BaseModel):
    success: bool
    operation: str
    path: str
    code: str | None = Field(None, description="Код ИР, который вернула платформа")
    call_id: str = Field("", description="ID вызова для validate_answer")


class OperationPreview(BaseModel):
    operation: str
    path: str
    details: dict[str, Any] = Field(default_factory=dict)
    call_id: str = Field("", description="ID вызова для validate_answer")


ErrorKind = Literal["not_found", "auth", "platform", "timeout", "validation"]


class ErrorResult(BaseModel):
    error: str
    kind: ErrorKind


class CatalogEntry(BaseModel):
    title: str = Field(description="Имя инфоресурса (последний сегмент path)")
    path: str = Field(min_length=1, description="Путь внутри «Мой Фонд» без e-mail и префикса")


class CatalogListing(BaseModel):
    source: Literal["configured_catalog"] = "configured_catalog"
    resources: list[CatalogEntry]
    total_matches: int
    truncated: bool
