"""MCP-сервер IACPaaS: тулзы поверх API импорта-экспорта платформы.

MCP tool               -> операция IACPaaS
get_inforesource       -> GET    /api/data/export/user-item
get_subgraph           -> GET    /api/data/export/user-item (start-target-concept-path, export-depth)
count_graph            -> GET    /api/data/export/user-item + подсчёт по дереву
query_graph            -> GET    /api/data/export/user-item + поиск по дереву
validate_resource      -> локально, модель InfoResourceDocument
import_inforesource    -> POST   /api/data/import
update_vertex          -> PUT    /api/data/update
delete_inforesource    -> DELETE /api/data/delete
"""

import json
import re
from collections.abc import Iterator
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from pydantic import Field, ValidationError

from .client import IacpaasClient
from .config import ALLOW_WRITE
from .models import (
    ConceptPath,
    DeleteMode,
    Depth,
    GraphCounts,
    GraphEdge,
    GraphNode,
    InfoResourceDocument,
    IrPath,
    JsonType,
    Limit,
    MetaconceptPath,
    OperationResult,
    QueryResult,
    Subgraph,
    ValidationIssue,
    ValidationReport,
)


@lifespan
async def client_lifespan(server: FastMCP):
    client = IacpaasClient()
    try:
        yield {"client": client}
    finally:
        await client.aclose()


mcp = FastMCP(
    "IACPaaS MCP",
    instructions="Доступ к инфоресурсам IACPaaS: чтение графа, подсчёт, поиск, валидация и запись. "
    "ИР адресуется путём в «Мой Фонд» без '<e-mail>/Мой Фонд/'.",
    lifespan=client_lifespan,
)

READ = {"readOnlyHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}


def client(ctx: Context) -> IacpaasClient:
    return ctx.lifespan_context["client"]


def require_write_allowed() -> None:
    if not ALLOW_WRITE:
        raise ToolError("Запись запрещена (IACPAAS_ALLOW_WRITE=false)")


# ---------- обход экспортированного дерева ----------

# Местоположение пути: либо в тройных кавычках (может содержать '/'), либо до следующего '/'
SEGMENT = re.compile(r"‴[^‴]*‴|[^/]+")


def _segment(vertex: dict[str, Any]) -> str:
    label = str(vertex.get("name", vertex.get("value", "")))
    return f"‴{label}‴" if "/" in label else label


def _join(base: str, segment: str) -> str:
    return f"{base}/{segment}" if base else segment


def _norm(concept_path: str) -> str:
    return "/".join(s.strip() for s in SEGMENT.findall(concept_path) if s.strip())


def walk(
    vertex: dict[str, Any], concept_path: str = "", metaconcept_path: str = "", depth: int = 0
) -> Iterator[tuple[dict[str, Any], str, str, int]]:
    """Обход в глубину: (вершина, concept_path, metaconcept_path, глубина). Корень ИР - пустые пути."""
    yield vertex, concept_path, metaconcept_path, depth
    for child in vertex.get("successors", []):
        yield from walk(
            child,
            _join(concept_path, _segment(child)),
            _join(metaconcept_path, child.get("meta", "")),
            depth + 1,
        )


async def export_from(ctx: Context, path: str, start: str, depth: int | None):
    """Экспортировать ИР от вершины start и вернуть обход её поддерева."""
    doc = await client(ctx).export(path, start_concept_path=start or None, depth=depth)
    target = _norm(start)
    for vertex, cp, mp, _ in walk(doc):
        if cp == target:
            return walk(vertex, cp, mp)
    raise ToolError(f"Вершина '{start}' не найдена в ИР '{path}'")


def to_node(vertex: dict[str, Any], concept_path: str, metaconcept_path: str, depth: int) -> GraphNode:
    return GraphNode(
        id=vertex.get("id"),
        label=vertex.get("name", vertex.get("value", "")),
        type=vertex["type"],
        valtype=vertex.get("valtype"),
        meta=vertex.get("meta"),
        comment=vertex.get("comment"),
        concept_path=concept_path,
        metaconcept_path=metaconcept_path,
        depth=depth,
        link=vertex.get("link"),
        original=vertex.get("original"),
    )


# ---------- read ----------


@mcp.tool(annotations=READ, tags={"read"})
async def get_inforesource(
    ctx: Context,
    path: IrPath,
    format: Literal["metadata", "json"] = "metadata",
    json_type: JsonType = "universal",
) -> dict[str, Any]:
    """Получить ИР: metadata - сведения об ИР и корень без потомков, json - весь ИР в формате json_type."""
    depth = 0 if format == "metadata" else None
    return await client(ctx).export(path, json_type, depth=depth)


@mcp.tool(annotations=READ, tags={"read"})
async def get_subgraph(
    ctx: Context,
    path: IrPath,
    start: ConceptPath = "",
    depth: Depth = 2,
    limit: Limit = 200,
) -> Subgraph:
    """Получить подграф ИР (вершины и дуги) от вершины start на глубину depth."""
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    truncated = False
    for vertex, cp, mp, d in await export_from(ctx, path, start, depth):
        if len(nodes) == limit:
            truncated = True
            break
        nodes.append(to_node(vertex, cp, mp, d))
        for child in vertex.get("successors", []):
            edges.append(GraphEdge(source=cp, target=_join(cp, _segment(child)), rcomment=child.get("rcomment")))
    included = {n.concept_path for n in nodes}
    edges = [e for e in edges if e.target in included]
    return Subgraph(path=path, start=start, depth=depth, nodes=nodes, edges=edges, truncated=truncated)


@mcp.tool(annotations=READ, tags={"read"})
async def count_graph(ctx: Context, path: IrPath, start: ConceptPath = "") -> GraphCounts:
    """Подсчитать вершины и дуги ИР целиком или поддерева от вершины start."""
    ids: set[Any] = set()
    entries = 0
    for vertex, *_ in await export_from(ctx, path, start, None):
        entries += 1
        # Повторно использованная вершина (link) встречается в дереве снова, но вершиной считается один раз
        ids.add(vertex.get("id", id(vertex)))
    return GraphCounts(path=path, start=start, nodes=len(ids), edges=entries - 1)


@mcp.tool(annotations=READ, tags={"read"})
async def query_graph(
    ctx: Context,
    path: IrPath,
    query: Literal["find_by_label", "find_by_meta"],
    text: Annotated[str, Field(min_length=1, description="Имя/значение вершины или метка метавершины")],
    exact: bool = False,
    start: ConceptPath = "",
    limit: Limit = 50,
) -> QueryResult:
    """Найти вершины по имени/значению (find_by_label) или по метке метавершины (find_by_meta)."""
    needle = text.casefold()
    matches: list[GraphNode] = []
    for vertex, cp, mp, d in await export_from(ctx, path, start, None):
        field = vertex.get("meta", "") if query == "find_by_meta" else vertex.get("name", vertex.get("value", ""))
        hay = str(field).casefold()
        if (hay == needle) if exact else (needle in hay):
            matches.append(to_node(vertex, cp, mp, d))
    return QueryResult(query=query, matches=matches[:limit], total=len(matches), truncated=len(matches) > limit)


# ---------- control ----------


@mcp.tool(annotations={"readOnlyHint": True}, tags={"control"})
async def validate_resource(payload: dict[str, Any] | str) -> ValidationReport:
    """Проверить JSON ИР в универсальном формате теми же правилами, что и перед импортом."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as e:
            return ValidationReport(valid=False, errors=[ValidationIssue(pointer="", message=f"Невалидный JSON: {e}")])
    try:
        InfoResourceDocument.model_validate(payload)
    except ValidationError as e:
        issues = [
            ValidationIssue(pointer="".join(f"/{p}" for p in err["loc"]), message=err["msg"]) for err in e.errors()
        ]
        return ValidationReport(valid=False, errors=issues)
    return ValidationReport(valid=True)


# ---------- write ----------


@mcp.tool(annotations=WRITE, tags={"write"})
async def import_inforesource(
    ctx: Context,
    folder: Annotated[str, Field(description="Папка-контейнер в «Мой Фонд», без имени ИР и без '<e-mail>/Мой Фонд/'")],
    resource: InfoResourceDocument,
    clear_if_exists: bool = False,
) -> OperationResult:
    """Создать ИР импортом JSON (если ИР уже есть - дописать или очистить перед импортом)."""
    require_write_allowed()
    data = await client(ctx).import_(folder, resource.to_import_dict(), clear_if_exists)
    return OperationResult(success=True, operation="import", path=_join(folder, resource.title), code=data.get("code"))


@mcp.tool(annotations=WRITE, tags={"write"})
async def update_vertex(
    ctx: Context,
    path: IrPath,
    concept_path: ConceptPath,
    metaconcept_path: MetaconceptPath,
    mark: Annotated[str, Field(description="Новая метка вершины (строкой)")],
    comment: str | None = None,
) -> OperationResult:
    """Изменить метку и/или комментарий вершины. Пути берутся из get_subgraph/query_graph."""
    require_write_allowed()
    data = await client(ctx).update(path, concept_path, metaconcept_path, mark, comment)
    return OperationResult(success=True, operation="update", path=path, code=data.get("code"))


@mcp.tool(annotations=WRITE, tags={"write"})
async def delete_inforesource(
    ctx: Context,
    path: IrPath,
    mode: Annotated[
        DeleteMode,
        Field(description="deleteIR/trashIR/clearIR - весь ИР; deleteSubGraph/clearSubGraph - подграф по concept_path"),
    ],
    concept_path: ConceptPath | None = None,
    metaconcept_path: MetaconceptPath | None = None,
) -> OperationResult:
    """Удалить/очистить ИР целиком или подграф в нём."""
    require_write_allowed()
    if mode.endswith("SubGraph") and (concept_path is None or metaconcept_path is None):
        raise ToolError(f"Для {mode} нужны concept_path и metaconcept_path")
    data = await client(ctx).delete(path, mode, concept_path, metaconcept_path)
    return OperationResult(success=True, operation=mode, path=path, code=data.get("code"))
