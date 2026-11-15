import hmac
import json
import re
from collections.abc import Iterator
from contextlib import suppress
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.server.lifespan import lifespan
from pydantic import Field, ValidationError

from iacpaas_agent.schemas import AgentAnswer
from iacpaas_agent.validator import Verdict, validate

from .catalog import list_entries, remove_entry, upsert_path
from .client import IacpaasClient
from .config import ALLOW_WRITE, get_apply_token
from .graph_cache import cache
from .models import (
    TERMINAL_TYPES,
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
    Offset,
    OperationPreview,
    OperationResult,
    QueryResult,
    Subgraph,
    TerminalValue,
    TerminalValues,
    VertexLookup,
    VertexMatch,
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
MAX_FIND_CHILDREN = 100


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
    vertex: dict[str, Any],
    concept_path: str = "",
    metaconcept_path: str = "",
    depth: int = 0,
    max_depth: int | None = None,
) -> Iterator[tuple[dict[str, Any], str, str, int]]:
    yield vertex, concept_path, metaconcept_path, depth
    if max_depth is not None and depth >= max_depth:
        return
    for child in vertex.get("successors", []):
        yield from walk(
            child,
            _join(concept_path, _segment(child)),
            _join(metaconcept_path, child.get("meta", "")),
            depth + 1,
            max_depth,
        )


def walk_with_ancestors(
    vertex: dict[str, Any],
    concept_path: str = "",
    metaconcept_path: str = "",
    depth: int = 0,
    max_depth: int | None = None,
    ancestors: tuple[str, ...] = (),
) -> Iterator[tuple[dict[str, Any], str, str, int, tuple[str, ...]]]:
    yield vertex, concept_path, metaconcept_path, depth, ancestors
    if max_depth is not None and depth >= max_depth:
        return
    label = str(vertex.get("name", vertex.get("value", "")))
    for child in vertex.get("successors", []):
        yield from walk_with_ancestors(
            child,
            _join(concept_path, _segment(child)),
            _join(metaconcept_path, child.get("meta", "")),
            depth + 1,
            max_depth,
            ancestors + (label,),
        )


def find_root(
    doc: dict[str, Any], target: str
) -> tuple[dict[str, Any], str, str, int] | None:
    for vertex, concept_path, metaconcept_path, depth in walk(doc):
        if concept_path == target:
            return vertex, concept_path, metaconcept_path, depth
    return None


def to_node(
    vertex: dict[str, Any],
    concept_path: str,
    metaconcept_path: str,
    depth: int,
    view: Literal["compact", "summary", "full"] = "full",
) -> GraphNode:
    label = vertex.get("name", vertex.get("value", ""))
    if view == "compact":
        return GraphNode(
            label=label,
            type=vertex["type"],
            meta=vertex.get("meta"),
            concept_path=concept_path,
        )
    if view == "summary":
        return GraphNode(
            label=label,
            type=vertex["type"],
            meta=vertex.get("meta"),
            concept_path=concept_path,
            metaconcept_path=metaconcept_path,
            depth=depth,
        )
    return GraphNode(
        id=vertex.get("id"),
        label=label,
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


@mcp.tool(annotations=READ, tags={"read"})
async def list_inforesources(
    query: Annotated[str, Field(max_length=200, description="Подстрока title или path")] = "",
    limit: Annotated[int, Field(ge=1, le=50, description="Максимум ресурсов")] = 20,
) -> CatalogListing | ErrorResult:
    """Найти ресурс в локальном catalog.txt."""
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
    refresh: bool = False,
) -> InfoResourceDocument | ErrorResult:
    """Получить ИР из локального кэша."""
    doc = await cache.load(client(ctx), path, refresh=refresh)
    if isinstance(doc, ErrorResult):
        return doc
    try:
        document = InfoResourceDocument.model_validate(doc)
    except ValidationError as e:
        return ErrorResult(error=f"Ответ IACPaaS не похож на ИР: {e}", kind="validation")
    if format == "metadata":
        document = document.model_copy(update={"successors": []})
    return record_evidence(ctx, document)


@mcp.tool(annotations=READ, tags={"read"})
async def get_subgraph(
    ctx: Context,
    path: IrPath,
    start: ConceptPath = "",
    depth: Depth = 2,
    limit: Limit = 100,
    offset: Offset = 0,
    view: Literal["compact", "summary", "full"] = "summary",
    refresh: bool = False,
) -> Subgraph | ErrorResult:
    """Постранично получить подграф ИР."""
    doc = await cache.load(client(ctx), path, refresh=refresh)
    if isinstance(doc, ErrorResult):
        return doc

    target = _norm(start)
    root = find_root(doc, target)
    if root is None:
        return ErrorResult(error=f"Вершина '{start}' не найдена в ИР '{path}'", kind="not_found")

    entries = list(walk(root[0], root[1], root[2], 0, max_depth=depth))
    total = len(entries)
    page = entries[offset : offset + limit]
    nodes = [to_node(vertex, cp, mp, d, view) for vertex, cp, mp, d in page]
    included = {node.concept_path for node in nodes}

    edges: list[GraphEdge] = []
    for vertex, cp, _, _ in page:
        for child in vertex.get("successors", []):
            target_path = _join(cp, _segment(child))
            if target_path in included:
                edges.append(
                    GraphEdge(
                        source=cp,
                        target=target_path,
                        rcomment=child.get("rcomment") if view == "full" else None,
                    )
                )

    has_more = offset + len(page) < total
    return record_evidence(
        ctx,
        Subgraph(
            path=path,
            start=target,
            depth=depth,
            view=view,
            nodes=nodes,
            edges=edges,
            offset=offset,
            total=total,
            has_more=has_more,
            next_offset=offset + limit if has_more else None,
        ),
    )


@mcp.tool(annotations=READ, tags={"read"})
async def count_graph(
    ctx: Context,
    path: IrPath,
    start: ConceptPath = "",
    refresh: bool = False,
) -> GraphCounts | ErrorResult:
    """Подсчитать вершины и дуги."""
    doc = await cache.load(client(ctx), path, refresh=refresh)
    if isinstance(doc, ErrorResult):
        return doc
    target = _norm(start)
    root = find_root(doc, target)
    if root is None:
        return ErrorResult(error=f"Вершина '{start}' не найдена в ИР '{path}'", kind="not_found")
    ids: set[Any] = set()
    entries = 0
    for vertex, *_ in walk(root[0], root[1], root[2], 0):
        entries += 1
        ids.add(vertex.get("id", id(vertex)))
    return record_evidence(
        ctx,
        GraphCounts(path=path, start=target, nodes=len(ids), edges=entries - 1),
    )


@mcp.tool(annotations=READ, tags={"read"})
async def search_vertices(
    ctx: Context,
    path: IrPath,
    text: Annotated[str, Field(min_length=1, description="Имя/значение вершины или метка метавершины")],
    by: Literal["label", "meta"] = "label",
    exact: bool = False,
    start: ConceptPath = "",
    limit: Limit = 50,
    offset: Offset = 0,
    view: Literal["compact", "summary", "full"] = "compact",
    refresh: bool = False,
) -> QueryResult | ErrorResult:
    """Найти совпадения по названию или мете."""
    doc = await cache.load(client(ctx), path, refresh=refresh)
    if isinstance(doc, ErrorResult):
        return doc
    target = _norm(start)
    root = find_root(doc, target)
    if root is None:
        return ErrorResult(error=f"Вершина '{start}' не найдена в ИР '{path}'", kind="not_found")
    needle = text.casefold()
    matches: list[GraphNode] = []
    for vertex, cp, mp, d in walk(root[0], root[1], root[2], 0):
        field = vertex.get("meta", "") if by == "meta" else vertex.get("name", vertex.get("value", ""))
        hay = str(field).casefold()
        matched = hay == needle if exact else needle in hay
        if matched:
            matches.append(to_node(vertex, cp, mp, d, view))
    page = matches[offset : offset + limit]
    has_more = offset + len(page) < len(matches)
    return record_evidence(
        ctx,
        QueryResult(
            path=path,
            by=by,
            view=view,
            matches=page,
            total=len(matches),
            offset=offset,
            has_more=has_more,
            next_offset=offset + limit if has_more else None,
        ),
    )


@mcp.tool(annotations=READ, tags={"read"})
async def locate_vertex(
    ctx: Context,
    path: IrPath,
    text: Annotated[str, Field(min_length=1, max_length=200, description="Подстрока названия, значения, меты или комментария")],
    limit: Annotated[int, Field(ge=1, le=50, description="Максимум совпадений")] = 5,
    start: ConceptPath = "",
    depth: Depth = 2,
    view: Literal["compact", "summary", "full"] = "compact",
) -> VertexLookup | ErrorResult:
    """Найти вершину, вернуть её path и потомков до depth."""
    if not text.strip():
        return ErrorResult(error="Пустой поисковый запрос", kind="validation")
    doc = await cache.load(client(ctx), path)
    if isinstance(doc, ErrorResult):
        return doc

    target = _norm(start)
    root = find_root(doc, target)
    if root is None:
        return ErrorResult(error=f"Вершина '{start}' не найдена в ИР '{path}'", kind="not_found")

    needle = text.strip().casefold()
    candidates: list[tuple[int, int, dict[str, Any], str, str, int, str]] = []
    for vertex, cp, mp, d in walk(root[0], root[1], root[2], 0):
        raw_label = vertex.get("name", vertex.get("value", ""))
        label = "" if raw_label is None else str(raw_label)
        raw_meta = vertex.get("meta")
        raw_comment = vertex.get("comment")
        fields = (
            ("label", label),
            ("meta", "" if raw_meta is None else str(raw_meta)),
            ("comment", "" if raw_comment is None else str(raw_comment)),
        )
        for field, value in fields:
            if value and needle in value.casefold():
                priority = 0 if label.casefold() == needle else 1 if label.casefold().startswith(needle) else 2
                candidates.append((priority, d, vertex, cp, mp, d, field))
                break

    candidates.sort(key=lambda item: (item[0], item[1], item[3].casefold()))
    total = len(candidates)
    matches: list[VertexMatch] = []
    for _, _, vertex, cp, mp, d, field in candidates[:limit]:
        children: list[GraphNode] = []
        truncated = False
        descendants = walk(vertex, cp, mp, 0, max_depth=depth)
        next(descendants, None)
        for child, child_path, child_meta_path, child_depth in descendants:
            if len(children) >= MAX_FIND_CHILDREN:
                truncated = True
                break
            children.append(to_node(child, child_path, child_meta_path, child_depth, view))
        matches.append(
            VertexMatch(
                field=field,
                path=cp,
                node=to_node(vertex, cp, mp, d, view),
                children=children,
                children_truncated=truncated,
            )
        )

    return record_evidence(
        ctx,
        VertexLookup(
            path=path,
            text=text.strip(),
            matches=matches,
            total=total,
            truncated=total > limit,
        ),
    )


@mcp.tool(annotations=READ, tags={"read"})
async def get_terminal_values(
    ctx: Context,
    path: IrPath,
    start: ConceptPath = "",
    starts: list[ConceptPath] | None = None,
    depth: Depth = 4,
    limit: Limit = 100,
    offset: Offset = 0,
    max_chars: Annotated[int, Field(ge=1000, le=100000, description="Максимальный размер страницы в символах")] = 12000,
    include_path: bool = False,
    refresh: bool = False,
) -> TerminalValues | ErrorResult:
    """Плоский список терминальных значений."""
    doc = await cache.load(client(ctx), path, refresh=refresh)
    if isinstance(doc, ErrorResult):
        return doc

    targets = [_norm(item) for item in (starts or []) if item.strip()]
    if not targets:
        targets = [_norm(start)]
    roots: list[tuple[dict[str, Any], str, str, int]] = []
    for target in targets:
        root = find_root(doc, target)
        if root is None:
            return ErrorResult(
                error=f"Вершина '{target}' не найдена в ИР '{path}'", kind="not_found"
            )
        roots.append(root)

    values: list[TerminalValue] = []
    for target, root in zip(targets, roots):
        for vertex, cp, _mp, d, ancestors in walk_with_ancestors(
            root[0], root[1], root[2], 0, depth
        ):
            if vertex.get("type") not in TERMINAL_TYPES:
                continue
            raw_value = vertex.get("value")
            if raw_value is None:
                raw_value = vertex.get("name")
            if raw_value is None:
                continue
            raw_meta = vertex.get("meta")
            chain = [label for label in ancestors[1:] if label and not label.isdigit()]
            values.append(
                TerminalValue(
                    property=" / ".join(chain)
                    or ancestors[0]
                    or target
                    or str(vertex.get("name", raw_value)),
                    value=raw_value,
                    meta=None if raw_meta is None else str(raw_meta),
                    concept_path=cp if include_path else None,
                    depth=d,
                    start=target if starts else None,
                )
            )

    total = len(values)
    candidates = values[offset : offset + limit]
    page: list[TerminalValue] = []
    used_chars = 0
    for item in candidates:
        item_chars = len(json.dumps(item.model_dump(mode="json"), ensure_ascii=False))
        if page and used_chars + item_chars > max_chars:
            break
        page.append(item)
        used_chars += item_chars

    has_more = offset + len(page) < total
    size_limited = has_more and len(page) < len(candidates)
    suggested_starts: list[str] = []
    if has_more:
        for root in roots:
            for child in root[0].get("successors", [])[:8]:
                child_path = _join(root[1], _segment(child))
                if child_path not in suggested_starts:
                    suggested_starts.append(child_path)
        suggested_starts = suggested_starts[:8]

    return record_evidence(
        ctx,
        TerminalValues(
            path=path,
            start=targets[0],
            starts=targets if starts else None,
            depth=depth,
            values=page,
            total=total,
            offset=offset,
            has_more=has_more,
            next_offset=offset + len(page) if has_more else None,
            truncated_reason="size" if size_limited else "limit" if has_more else "none",
            hint=(
                "Ответ усечён. Сузьте start или передайте starts для нужных веток; "
                "не обходите соседние компоненты отдельными get_subgraph."
                if has_more
                else None
            ),
            suggested_starts=suggested_starts,
        ),
    )


@mcp.tool(annotations=READ, tags={"read", "validation"})
async def validate_answer(ctx: Context, answer: AgentAnswer) -> Verdict:
    """Проверить AgentAnswer по evidence текущей сессии."""
    return validate(answer, evidence_store(ctx))


def _apply_blocked(token: str) -> ErrorResult | None:
    if not hmac.compare_digest(token, get_apply_token()):
        return ErrorResult(error="Неверный токен подтверждения", kind="auth")
    return write_blocked()


@mcp.tool(annotations=READ, tags={"preview"})
async def preview_import(
    ctx: Context,
    folder: Annotated[str, Field(description="Папка-контейнер в «Мой Фонд», без имени ИР и без '<e-mail>/Мой Фонд/'")],
    resource: InfoResourceDocument,
    clear_if_exists: bool = False,
) -> OperationPreview | ErrorResult:
    """Подготовить импорт ИР."""
    path = _join(folder, resource.title)
    return record_evidence(
        ctx,
        OperationPreview(
            operation="import",
            path=path,
            details={"folder": folder, "title": resource.title, "clear_if_exists": clear_if_exists},
        ),
    )


@mcp.tool(annotations=WRITE, tags={"write", "internal"})
async def apply_import(
    ctx: Context,
    folder: str,
    resource: InfoResourceDocument,
    confirmation_token: str,
    clear_if_exists: bool = False,
) -> OperationResult | ErrorResult:
    """Выполнить импорт; вызывается веб-сервером."""
    blocked = _apply_blocked(confirmation_token)
    if blocked:
        return blocked
    path = _join(folder, resource.title)
    data = await client(ctx).import_(folder, resource.to_import_dict(), clear_if_exists)
    if isinstance(data, ErrorResult):
        return data
    result = record_evidence(
        ctx, OperationResult(success=True, operation="import", path=path, code=data.get("code"))
    )
    with suppress(Exception):
        upsert_path(path)
    cache.invalidate(path)
    return result


@mcp.tool(annotations=READ, tags={"preview"})
async def preview_update(
    ctx: Context,
    path: IrPath,
    concept_path: ConceptPath,
    metaconcept_path: MetaconceptPath,
    mark: Annotated[str, Field(description="Новая метка вершины (строкой)")],
    comment: str | None = None,
) -> OperationPreview | ErrorResult:
    """Подготовить изменение вершины."""
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


@mcp.tool(annotations=WRITE, tags={"write", "internal"})
async def apply_update(
    ctx: Context,
    path: IrPath,
    concept_path: ConceptPath,
    metaconcept_path: MetaconceptPath,
    mark: str,
    confirmation_token: str,
    comment: str | None = None,
) -> OperationResult | ErrorResult:
    """Выполнить изменение вершины; вызывается веб-сервером."""
    blocked = _apply_blocked(confirmation_token)
    if blocked:
        return blocked
    data = await client(ctx).update(path, concept_path, metaconcept_path, mark, comment)
    if isinstance(data, ErrorResult):
        return data
    cache.invalidate(path)
    return record_evidence(
        ctx, OperationResult(success=True, operation="update", path=path, code=data.get("code"))
    )


@mcp.tool(annotations=READ, tags={"preview"})
async def preview_delete(
    ctx: Context,
    path: IrPath,
    mode: Annotated[
        DeleteMode,
        Field(description="deleteIR/trashIR/clearIR - весь ИР; deleteSubGraph/clearSubGraph - подграф по concept_path"),
    ],
    concept_path: ConceptPath | None = None,
    metaconcept_path: MetaconceptPath | None = None,
) -> OperationPreview | ErrorResult:
    """Подготовить удаление или очистку."""
    if mode.endswith("SubGraph") and (concept_path is None or metaconcept_path is None):
        return ErrorResult(error=f"Для {mode} нужны concept_path и metaconcept_path", kind="validation")
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


@mcp.tool(annotations=WRITE, tags={"write", "internal"})
async def apply_delete(
    ctx: Context,
    path: IrPath,
    mode: DeleteMode,
    confirmation_token: str,
    concept_path: ConceptPath | None = None,
    metaconcept_path: MetaconceptPath | None = None,
) -> OperationResult | ErrorResult:
    """Выполнить удаление или очистку; вызывается веб-сервером."""
    blocked = _apply_blocked(confirmation_token)
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
    if mode in {"deleteIR", "trashIR"}:
        with suppress(Exception):
            remove_entry(path)
    cache.invalidate(path)
    return result
