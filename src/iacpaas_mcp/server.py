"""MCP-сервер IACPaaS: тулзы поверх API импорта-экспорта платформы.

MCP tool               -> операция IACPaaS
list_inforesources     -> локальный catalog.txt
get_inforesource       -> GET    /api/data/export/user-item
get_subgraph           -> GET    /api/data/export/user-item (start-target-concept-path, export-depth)
count_graph            -> GET    /api/data/export/user-item + подсчет по дереву
query_graph            -> GET    /api/data/export/user-item + поиск по дереву
validate_answer        -> локальная проверка AgentAnswer по evidence сессии
import_inforesource    -> POST   /api/data/import
update_vertex          -> PUT    /api/data/update
delete_inforesource    -> DELETE /api/data/delete
"""

import re
from collections.abc import Iterator
from contextlib import suppress
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.server.lifespan import lifespan
from pydantic import Field, ValidationError

from iacpaas_agent.schemas import AgentAnswer
from iacpaas_agent.validator import Verdict, validate

from .catalog import find_entry, list_entries, remove_entry, upsert_path
from .client import IacpaasClient
from .config import ALLOW_WRITE
from .models import (
    CatalogListing,
    ConceptPath,
    DeleteMode,
    Depth,
    ErrorResult,
    GraphCounts,
    GraphEdge,
    GraphNode,
    InfoResourceDocument,
    IrPath,
    Limit,
    MetaconceptPath,
    OperationPreview,
    OperationResult,
    QueryResult,
    Subgraph,
)


@lifespan
async def client_lifespan(server: FastMCP):
    client = IacpaasClient()
    try:
        yield {"client": client, "evidence": {}}
    finally:
        await client.aclose()


mcp = FastMCP(
    "IACPaaS MCP",
    instructions="Доступ к инфоресурсам IACPaaS: чтение графа, подсчет, поиск, валидация и запись. "
    "ИР адресуется путем в «Мой Фонд» без '<e-mail>/Мой Фонд/'.",
    lifespan=client_lifespan,
)

READ = {"readOnlyHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}


def client(ctx: Context) -> IacpaasClient:
    return ctx.lifespan_context["client"]


def write_blocked() -> ErrorResult | None:
    if not ALLOW_WRITE:
        return ErrorResult(error="Запись запрещена (IACPAAS_ALLOW_WRITE=false)", kind="auth")
    return None


MAX_EVIDENCE = 50


def evidence_store(ctx: Context) -> dict[str, Any]:
    return ctx.lifespan_context["evidence"]


def record_evidence(ctx: Context, result: Any) -> Any:
    call_id = str(ctx.request_id)
    stored = result.model_copy(update={"call_id": call_id})
    store = evidence_store(ctx)
    if len(store) >= MAX_EVIDENCE:
        oldest = next(iter(store), None)
        if oldest is not None:
            store.pop(oldest, None)
    store[call_id] = stored.model_dump(mode="json")
    return stored


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


async def export_checked(ctx: Context, path: str, **kwargs: Any) -> dict[str, Any] | ErrorResult:
    try:
        entry = find_entry(path)
    except Exception as e:  # noqa: BLE001 - ошибка файла возвращается клиенту
        return ErrorResult(error=f"catalog.txt: {e}", kind="platform")
    if entry is None:
        return ErrorResult(
            error=f"Ресурс '{path}' отсутствует в catalog.txt. Сначала вызовите list_inforesources.",
            kind="not_found",
        )
    doc = await client(ctx).export(path, **kwargs)
    if isinstance(doc, ErrorResult):
        return doc
    if not isinstance(doc, dict):
        return ErrorResult(error="Ответ IACPaaS не является объектом", kind="platform")
    return doc


async def export_from(
    ctx: Context, path: str, start: str, depth: int | None
) -> Iterator[tuple[dict[str, Any], str, str, int]] | ErrorResult:
    """Экспортировать ИР от вершины start и вернуть обход ее поддерева."""
    doc = await export_checked(ctx, path, start_concept_path=start or None, depth=depth)
    if isinstance(doc, ErrorResult):
        return doc
    target = _norm(start)
    for vertex, cp, mp, _ in walk(doc):
        if cp == target:
            return walk(vertex, cp, mp)
    return ErrorResult(error=f"Вершина '{start}' не найдена в ИР '{path}'", kind="not_found")


def to_node(
    vertex: dict[str, Any],
    concept_path: str,
    metaconcept_path: str,
    depth: int,
    view: Literal["summary", "full"] = "full",
) -> GraphNode:
    if view == "summary":
        return GraphNode(
            label=vertex.get("name", vertex.get("value", "")),
            type=vertex["type"],
            meta=vertex.get("meta"),
            concept_path=concept_path,
            metaconcept_path=metaconcept_path,
            depth=depth,
        )
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
async def list_inforesources(
    query: Annotated[str, Field(max_length=200, description="Подстрока title или path")] = "",
    limit: Annotated[int, Field(ge=1, le=50, description="Максимум ресурсов")] = 20,
) -> CatalogListing | ErrorResult:
    """Найти ресурс в локальном catalog.txt. Это не живой список фонда IACPaaS."""
    try:
        return list_entries(query, limit)
    except Exception as e:  # noqa: BLE001 - ошибка файла возвращается клиенту
        return ErrorResult(error=f"catalog.txt: {e}", kind="platform")


@mcp.tool(annotations=READ, tags={"read"})
async def get_inforesource(
    ctx: Context,
    path: IrPath,
    format: Literal["metadata", "json"] = "metadata",
    json_type: Literal["universal"] = "universal",
) -> InfoResourceDocument | ErrorResult:
    """Получить ИР: metadata - сведения и корень без потомков, json - весь ИР."""
    depth = 0 if format == "metadata" else None
    doc = await export_checked(ctx, path, json_type=json_type, depth=depth)
    if isinstance(doc, ErrorResult):
        return doc
    try:
        document = InfoResourceDocument.model_validate(doc)
    except ValidationError as e:
        return ErrorResult(error=f"Ответ IACPaaS не похож на ИР: {e}", kind="validation")
    return record_evidence(ctx, document)


@mcp.tool(annotations=READ, tags={"read"})
async def get_subgraph(
    ctx: Context,
    path: IrPath,
    start: ConceptPath = "",
    depth: Depth = 2,
    limit: Limit = 200,
    view: Literal["summary", "full"] = "summary",
) -> Subgraph | ErrorResult:
    """Получить подграф ИР. view=summary - без комментариев и служебных полей, full - все данные."""
    result = await export_from(ctx, path, start, depth)
    if isinstance(result, ErrorResult):
        return result
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    truncated = False
    for vertex, cp, mp, d in result:
        if len(nodes) == limit:
            truncated = True
            break
        nodes.append(to_node(vertex, cp, mp, d, view))
        for child in vertex.get("successors", []):
            edges.append(
                GraphEdge(
                    source=cp,
                    target=_join(cp, _segment(child)),
                    rcomment=child.get("rcomment") if view == "full" else None,
                )
            )
    included = {n.concept_path for n in nodes}
    edges = [e for e in edges if e.target in included]
    return record_evidence(
        ctx,
        Subgraph(
            path=path,
            start=_norm(start),
            depth=depth,
            view=view,
            nodes=nodes,
            edges=edges,
            truncated=truncated,
        ),
    )


@mcp.tool(annotations=READ, tags={"read"})
async def count_graph(ctx: Context, path: IrPath, start: ConceptPath = "") -> GraphCounts | ErrorResult:
    """Подсчитать вершины и дуги ИР целиком или поддерева от вершины start."""
    result = await export_from(ctx, path, start, None)
    if isinstance(result, ErrorResult):
        return result
    ids: set[Any] = set()
    entries = 0
    for vertex, *_ in result:
        entries += 1
        # Повторно использованная вершина (link) встречается в дереве снова, но вершиной считается один раз
        ids.add(vertex.get("id", id(vertex)))
    return record_evidence(
        ctx,
        GraphCounts(path=path, start=_norm(start), nodes=len(ids), edges=entries - 1),
    )


@mcp.tool(annotations=READ, tags={"read"})
async def query_graph(
    ctx: Context,
    path: IrPath,
    query: Literal["find_by_label", "find_by_meta"],
    text: Annotated[str, Field(min_length=1, description="Имя/значение вершины или метка метавершины")],
    exact: bool = False,
    start: ConceptPath = "",
    limit: Limit = 50,
) -> QueryResult | ErrorResult:
    """Найти вершины по имени/значению (find_by_label) или по метке метавершины (find_by_meta)."""
    result = await export_from(ctx, path, start, None)
    if isinstance(result, ErrorResult):
        return result
    needle = text.casefold()
    matches: list[GraphNode] = []
    for vertex, cp, mp, d in result:
        field = vertex.get("meta", "") if query == "find_by_meta" else vertex.get("name", vertex.get("value", ""))
        hay = str(field).casefold()
        if (hay == needle) if exact else (needle in hay):
            matches.append(to_node(vertex, cp, mp, d))
    return record_evidence(
        ctx,
        QueryResult(
            path=path, query=query, matches=matches[:limit], total=len(matches), truncated=len(matches) > limit
        ),
    )


@mcp.tool(annotations=READ, tags={"read", "validation"})
async def validate_answer(ctx: Context, answer: AgentAnswer) -> Verdict:
    """Проверить AgentAnswer по результатам read-тулзов текущей MCP-сессии."""
    return validate(answer, evidence_store(ctx))


# ---------- write ----------


@mcp.tool(annotations=WRITE, tags={"write"})
async def import_inforesource(
    ctx: Context,
    folder: Annotated[str, Field(description="Папка-контейнер в «Мой Фонд», без имени ИР и без '<e-mail>/Мой Фонд/'")],
    resource: InfoResourceDocument,
    clear_if_exists: bool = False,
    confirm: bool = False,
) -> OperationResult | OperationPreview | ErrorResult:
    """Создать ИР импортом JSON. Без confirm возвращает план операции и ничего не меняет."""
    path = _join(folder, resource.title)
    if not confirm:
        return record_evidence(
            ctx,
            OperationPreview(
                operation="import",
                path=path,
                details={"folder": folder, "title": resource.title, "clear_if_exists": clear_if_exists},
            ),
        )
    blocked = write_blocked()
    if blocked:
        return blocked
    data = await client(ctx).import_(folder, resource.to_import_dict(), clear_if_exists)
    if isinstance(data, ErrorResult):
        return data
    result = record_evidence(
        ctx, OperationResult(success=True, operation="import", path=path, code=data.get("code"))
    )
    with suppress(Exception):
        upsert_path(path)
    return result


@mcp.tool(annotations=WRITE, tags={"write"})
async def update_vertex(
    ctx: Context,
    path: IrPath,
    concept_path: ConceptPath,
    metaconcept_path: MetaconceptPath,
    mark: Annotated[str, Field(description="Новая метка вершины (строкой)")],
    comment: str | None = None,
    confirm: bool = False,
) -> OperationResult | OperationPreview | ErrorResult:
    """Изменить метку и/или комментарий вершины. Без confirm возвращает план операции."""
    if not confirm:
        return record_evidence(
            ctx,
            OperationPreview(
                operation="update",
                path=path,
                details={
                    "concept_path": concept_path,
                    "metaconcept_path": metaconcept_path,
                    "mark": mark,
                    "comment": comment,
                },
            ),
        )
    blocked = write_blocked()
    if blocked:
        return blocked
    data = await client(ctx).update(path, concept_path, metaconcept_path, mark, comment)
    if isinstance(data, ErrorResult):
        return data
    return record_evidence(
        ctx, OperationResult(success=True, operation="update", path=path, code=data.get("code"))
    )


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
    confirm: bool = False,
) -> OperationResult | OperationPreview | ErrorResult:
    """Удалить/очистить ИР или подграф. Без confirm возвращает план операции."""
    if mode.endswith("SubGraph") and (concept_path is None or metaconcept_path is None):
        return ErrorResult(error=f"Для {mode} нужны concept_path и metaconcept_path", kind="validation")
    if not confirm:
        return record_evidence(
            ctx,
            OperationPreview(
                operation=mode,
                path=path,
                details={
                    "concept_path": concept_path,
                    "metaconcept_path": metaconcept_path,
                },
            ),
        )
    blocked = write_blocked()
    if blocked:
        return blocked
    data = await client(ctx).delete(path, mode, concept_path, metaconcept_path)
    if isinstance(data, ErrorResult):
        return data
    if mode in {"deleteIR", "trashIR"}:
        check = await client(ctx).export(path)
        if not isinstance(check, ErrorResult):
            return ErrorResult(
                error=(
                    f"IACPaaS сообщил об успешном {mode} для '{path}', "
                    "но ресурс всё ещё доступен через export"
                ),
                kind="platform",
            )
        if check.kind != "not_found":
            return ErrorResult(
                error=f"Не удалось подтвердить удаление '{path}': {check.error}",
                kind=check.kind,
            )
    result = record_evidence(
        ctx, OperationResult(success=True, operation=mode, path=path, code=data.get("code"))
    )
    if mode in {"deleteIR", "trashIR", "clearIR"}:
        with suppress(Exception):
            remove_entry(path)
    return result
