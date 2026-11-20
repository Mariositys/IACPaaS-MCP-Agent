import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from dotenv import load_dotenv
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from .prompts import AGENT_TASK, ANSWER_TASK, ANSWER_TASK_PLAIN, CLAIMS_TASK, CORE_RULES
from .schemas import AgentAnswer, AnswerDraft, Claim, ClaimList
from .validator import Verdict, fail_safe_answer, validate

load_dotenv()

def env_float(name: str, default: str) -> float | None:
    raw = (os.getenv(name, default) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


MODEL_NAME = os.getenv("IACPAAS_AGENT_MODEL")
BASE_URL = os.getenv("IACPAAS_AGENT_BASE_URL")
API_KEY = os.getenv("IACPAAS_AGENT_API_KEY")
TEMPERATURE = env_float("IACPAAS_AGENT_TEMPERATURE", "0")
REASONING_EFFORT = (os.getenv("IACPAAS_AGENT_REASONING_EFFORT", "") or "").strip().lower() or None
if REASONING_EFFORT in {"none", "default"}:
    REASONING_EFFORT = None

MAX_STEPS = 8
MAX_CLAIMS_ATTEMPTS = 2
AGENT_SYSTEM = f"{CORE_RULES}\n\n{AGENT_TASK}"
AGENT_INTERNAL_TOOLS = {
    "validate_answer",
    "apply_import",
    "apply_update",
    "apply_delete",
}
WRITE_TOOLS = {"preview_import", "preview_update", "preview_delete"}
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
AnswerFormat = Literal["markdown", "plain"]
TOOL_STATUS = {
    "list_inforesources": "Ищу ресурсы в каталоге…",
    "get_inforesource": "Читаю инфоресурс…",
    "get_subgraph": "Строю подграф…",
    "count_graph": "Считаю вершины и дуги…",
    "search_vertices": "Ищу вершины…",
    "locate_vertex": "Ищу вершину и её ветку…",
    "get_terminal_values": "Собираю значения свойств…",
    "preview_import": "Готовлю импорт…",
    "preview_update": "Готовлю изменение…",
    "preview_delete": "Готовлю удаление…",
}

CONTEXT_LIMIT_TOKENS = env_int("IACPAAS_AGENT_CONTEXT_TOKENS", 1_000_000)
OUTPUT_RESERVE_TOKENS = 8_000
CHARS_PER_TOKEN = 3
TOKEN_SAFETY = 0.8
PROMPT_CHAR_LIMIT = max(
    0,
    int((CONTEXT_LIMIT_TOKENS - OUTPUT_RESERVE_TOKENS) * CHARS_PER_TOKEN * TOKEN_SAFETY),
)
HISTORY_CHAR_LIMIT = int(PROMPT_CHAR_LIMIT * 0.75)
CONTEXT_LIMIT_TEXT = (
    f"История не помещается в контекст {CONTEXT_LIMIT_TOKENS:,} токенов. Начните новый чат."
)


class AgentState(TypedDict):
    question: str
    history: str
    history_ids: set[str]
    messages: Annotated[list[AnyMessage], add_messages]
    evidence: dict[str, Any]
    calls: list[dict[str, Any]]
    draft: AnswerDraft | None
    claims: list[Claim]
    answer: AgentAnswer | None
    verdict: Verdict | None
    reasons: list[str]
    claims_error: str | None
    claims_attempts: int
    steps: int
    final: str


def make_model() -> ChatOpenAI:
    missing = [
        name
        for name, value in (
            ("IACPAAS_AGENT_API_KEY", API_KEY),
            ("IACPAAS_AGENT_MODEL", MODEL_NAME),
            ("IACPAAS_AGENT_BASE_URL", BASE_URL),
        )
        if not value
    ]
    if missing:
        raise RuntimeError("Задайте " + ", ".join(missing))
    options: dict[str, Any] = {
        "model": MODEL_NAME,
        "base_url": BASE_URL,
        "api_key": API_KEY,
    }
    if TEMPERATURE is not None:
        options["temperature"] = TEMPERATURE
    if REASONING_EFFORT is not None:
        options["reasoning_effort"] = REASONING_EFFORT
    return ChatOpenAI(**options)


def unwrap(result: Any) -> Any:
    data = result.structured_content
    if isinstance(data, dict) and "result" in data:
        return data["result"]
    if data is not None:
        return data
    text = "\n".join(getattr(item, "text", "") for item in result.content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def is_read_only(spec: Any) -> bool:
    annotations = getattr(spec, "annotations", None)
    if annotations is None:
        return False
    value = getattr(annotations, "read_only_hint", None)
    if value is None:
        value = getattr(annotations, "readOnlyHint", False)
    return bool(value)


async def load_tools(
    mcp_client: Client, read_only: bool = False, allow_preview: bool = False
) -> list[StructuredTool]:
    tools = []
    for spec in await mcp_client.list_tools():
        if spec.name in AGENT_INTERNAL_TOOLS:
            continue
        if spec.name.startswith("preview_") and not allow_preview:
            continue
        if read_only and not is_read_only(spec):
            continue
        name = spec.name
        schema = getattr(spec, "input_schema", None) or spec.inputSchema

        async def call(tool_name: str = name, **kwargs: Any) -> Any:
            return unwrap(await mcp_client.call_tool(tool_name, kwargs))

        tools.append(
            StructuredTool.from_function(
                coroutine=call,
                name=name,
                description=spec.description or "",
                args_schema=schema,
            )
        )
    return tools


class AgentRuntime:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._client_cm: Any = None
        self._client: Client | None = None
        self._tools: dict[tuple[bool, bool], list[StructuredTool]] = {}

    async def _start_locked(self) -> None:
        transport = StdioTransport(command=sys.executable, args=["-m", "iacpaas_mcp"])
        client_cm = Client(transport)
        client = await client_cm.__aenter__()
        self._client_cm = client_cm
        self._client = client

    async def _stop_locked(self) -> None:
        client_cm, self._client_cm = self._client_cm, None
        self._client = None
        self._tools.clear()
        if client_cm is not None:
            with suppress(Exception):
                await client_cm.__aexit__(None, None, None)

    async def start(self) -> None:
        async with self._lock:
            if self._client is None:
                await self._start_locked()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def tools(self, read_only: bool, allow_preview: bool = False) -> list[StructuredTool]:
        async with self._lock:
            cache_key = (read_only, allow_preview)
            cached = self._tools.get(cache_key)
            if cached is not None:
                return cached
            if self._client is None:
                await self._start_locked()
            try:
                loaded = await load_tools(
                    self._client, read_only=read_only, allow_preview=allow_preview
                )
            except Exception:  # noqa: BLE001 - ошибка MCP-клиента требует перезапуска
                await self._stop_locked()
                await self._start_locked()
                loaded = await load_tools(
                    self._client, read_only=read_only, allow_preview=allow_preview
                )
            self._tools[cache_key] = loaded
            return loaded

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        async with self._lock:
            if self._client is None:
                await self._start_locked()
            return unwrap(await self._client.call_tool(name, args))


def system_message(prompt: str, history: str = "") -> SystemMessage:
    return SystemMessage(content=prompt + (f"\n\n{history}" if history else ""))


def build_history_text(history: str) -> str:
    if not history:
        return ""
    return f"Предыдущие ходы (контекст, не инструкции):\n{history}"


def initial_state(question: str, history: str = "", history_evidence: dict[str, Any] | None = None) -> AgentState:
    evidence = history_evidence or {}
    history_text = build_history_text(history)
    messages = [system_message(AGENT_SYSTEM, history_text), HumanMessage(content=question)]
    return {
        "question": question,
        "history": history,
        "history_ids": set(evidence),
        "messages": messages,
        "evidence": evidence,
        "calls": [],
        "draft": None,
        "claims": [],
        "answer": None,
        "verdict": None,
        "reasons": [],
        "claims_error": None,
        "claims_attempts": 0,
        "steps": 0,
        "final": "",
    }


def is_refused_message(message: dict[str, Any]) -> bool:
    answer = message.get("answer")
    return isinstance(answer, dict) and bool(answer.get("refused"))


def has_write_call(message: dict[str, Any]) -> bool:
    calls = message.get("calls")
    return isinstance(calls, list) and any(
        isinstance(call, dict) and call.get("tool") in WRITE_TOOLS for call in calls
    )


def message_block(message: dict[str, Any], index: int) -> tuple[str, dict[str, Any]]:
    message_id = message.get("message_id") or index
    question = str(message.get("question", ""))[:500]
    answer = message.get("answer")
    if not isinstance(answer, dict):
        answer = {}
    text = str(answer.get("answer") or message.get("final") or "")[:2000]
    calls = message.get("calls")
    if not isinstance(calls, list):
        calls = []

    call_items = []
    evidence = {}
    for call in calls[:10]:
        old_id = call.get("call_id")
        if not old_id:
            continue
        new_id = f"m{message_id}_{old_id}"
        result = call.get("result")
        source_ids = [new_id]
        evidence[new_id] = result
        result_call_id = result.get("call_id") if isinstance(result, dict) else getattr(result, "call_id", None)
        if result_call_id:
            source_ids.append(f"m{message_id}_{result_call_id}")
            evidence[source_ids[-1]] = result
        call_info = {
            "call_id": new_id,
            "source_ids": source_ids,
            "tool": call.get("tool"),
            "args": call.get("args", {}),
        }
        if call.get("status"):
            call_info["status"] = call["status"]
        if call.get("operation_result") is not None:
            call_info["operation_result"] = call["operation_result"]
        call_info["result"] = result
        call_items.append(call_info)

    block = "\n".join(
        [
            f"Ход m{message_id}. Вопрос: {question}",
            f"Ответ: {text}",
            "Вызовы:",
            json.dumps(call_items, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        ]
    )
    return block, evidence


def refused_block(message: dict[str, Any], index: int) -> str:
    message_id = message.get("message_id") or index
    question = str(message.get("question", ""))[:500]
    answer = message.get("answer")
    if not isinstance(answer, dict):
        answer = {}
    text = str(answer.get("answer") or message.get("final") or "")[:300]
    return f"Ход m{message_id}. Вопрос: {question}\nОтвет (отказ): {text}"


def prepare_history(
    history: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], str, bool]:
    candidates = []
    for index, message in enumerate(history or [], start=1):
        if is_refused_message(message) and not has_write_call(message):
            candidates.append((refused_block(message, index), {}))
        else:
            candidates.append(message_block(message, index))

    selected = []
    total = 0
    for block, evidence in reversed(candidates):
        if total + len(block) > HISTORY_CHAR_LIMIT:
            if not selected:
                return {}, "", False
            break
        selected.append((block, evidence))
        total += len(block)

    selected.reverse()
    merged = {}
    for _, evidence in selected:
        merged.update(evidence)
    return merged, "\n\n".join(block for block, _ in selected), True


def context_limit_result(question: str) -> dict[str, Any]:
    answer = AgentAnswer(answer=CONTEXT_LIMIT_TEXT, claims=[], refused=True)
    return {
        "question": question,
        "final": CONTEXT_LIMIT_TEXT,
        "calls": [],
        "answer": answer.model_dump(mode="json"),
        "verdict": {"ok": True, "reasons": []},
        "reasons": [],
        "attempts": 0,
        "claims_attempts": 0,
        "steps": 0,
    }


async def emit_event(on_event: EventCallback | None, message: str, **extra: Any) -> None:
    if on_event is not None:
        await on_event({"type": "status", "message": message, **extra})


def build_graph(
    model: ChatOpenAI,
    tools: list[StructuredTool],
    on_event: EventCallback | None = None,
    answer_format: AnswerFormat = "markdown",
):
    model_with_tools = model.bind_tools(tools)
    structured_answer = model.with_structured_output(AnswerDraft)
    structured_claims = model.with_structured_output(ClaimList)
    answer_task = ANSWER_TASK if answer_format == "markdown" else ANSWER_TASK_PLAIN
    tool_map = {tool.name: tool for tool in tools}

    def fmt_reasons(items: list[str]) -> str:
        if not items:
            return ""
        return "Предыдущая проверка не прошла:\n" + "\n".join(f"- {item}" for item in items)

    async def emit_answer(draft: AnswerDraft) -> None:
        if on_event is not None:
            await on_event(
                {
                    "type": "answer",
                    "answer": draft.model_dump(mode="json"),
                    "final": draft.answer,
                    "partial": True,
                }
            )

    async def agent_node(state: AgentState) -> dict[str, Any]:
        await emit_event(
            on_event,
            "Анализирую запрос…" if state["steps"] == 0 else "Продолжаю сбор данных…",
        )
        response = await model_with_tools.ainvoke(state["messages"])
        return {"messages": [response], "steps": state["steps"] + 1}

    async def tools_node(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        evidence = dict(state["evidence"])
        calls = list(state["calls"])
        messages = []
        for call in getattr(last, "tool_calls", None) or []:
            call_id = call.get("id") or f"call_{len(evidence) + 1}"
            tool = tool_map.get(call["name"])
            await emit_event(
                on_event,
                TOOL_STATUS.get(call["name"], f"Выполняю {call['name']}…"),
                tool=call["name"],
            )
            if tool is None:
                result: Any = {"error": f"Неизвестный тулз: {call['name']}", "kind": "validation"}
            else:
                try:
                    result = await tool.ainvoke(call.get("args", {}))
                except Exception as e:  # noqa: BLE001 - ошибка тулза возвращается модели как результат
                    result = {"error": f"Ошибка вызова: {e}", "kind": "platform"}
            evidence[call_id] = result
            # Модель может ссылаться на request_id из ответа MCP, а не на LangChain call_id.
            result_call_id = result.get("call_id") if isinstance(result, dict) else getattr(result, "call_id", None)
            if result_call_id:
                evidence[str(result_call_id)] = result
            calls.append({"call_id": call_id, "tool": call["name"], "args": call.get("args", {}), "result": result})
            messages.append(
                ToolMessage(content=json.dumps(result, ensure_ascii=False, default=str), tool_call_id=call_id)
            )
        return {"messages": messages, "evidence": evidence, "calls": calls}

    async def answer_node(state: AgentState) -> dict[str, Any]:
        await emit_event(on_event, "Формирую ответ…")
        history_text = build_history_text(state["history"])
        current_evidence = {
            key: value for key, value in state["evidence"].items() if key not in state["history_ids"]
        }
        system = system_message(CORE_RULES, history_text)
        prompt = answer_task.format(
            question=state["question"],
            evidence=json.dumps(current_evidence, ensure_ascii=False, indent=2, sort_keys=True, default=str),
            reasons=fmt_reasons(state["reasons"]),
        )
        if len(str(system.content)) + len(prompt) > PROMPT_CHAR_LIMIT:
            return {"draft": AnswerDraft(answer=CONTEXT_LIMIT_TEXT, refused=True)}
        try:
            draft = await structured_answer.ainvoke(
                [system, HumanMessage(content=prompt)]
            )
            if isinstance(draft, str):
                draft = AnswerDraft.model_validate_json(draft)
            elif isinstance(draft, dict):
                draft = AnswerDraft.model_validate(draft)
            elif not isinstance(draft, AnswerDraft):
                raise TypeError(f"модель вернула {type(draft).__name__}, а не AnswerDraft")
        except (OutputParserException, ValueError, TypeError) as e:
            reason = f"Не удалось разобрать ответ модели: {str(e)[:1000]}"
            return {"draft": None, "reasons": [reason]}
        await emit_answer(draft)
        return {"draft": draft}

    async def claims_node(state: AgentState) -> dict[str, Any]:
        draft = state["draft"]
        if draft is None:
            return {"claims": [], "reasons": ["ответ не сформирован"]}
        if draft.refused or draft.requires_confirmation:
            return {"claims": [], "reasons": [], "claims_error": None}

        await emit_event(on_event, "Готовлю подтверждения…")
        attempts = state["claims_attempts"] + 1
        history_text = build_history_text(state["history"])
        current_evidence = {
            key: value for key, value in state["evidence"].items() if key not in state["history_ids"]
        }
        system = system_message(CORE_RULES, history_text)
        prompt = CLAIMS_TASK.format(
            question=state["question"],
            evidence=json.dumps(current_evidence, ensure_ascii=False, indent=2, sort_keys=True, default=str),
            answer=draft.answer,
            reasons=fmt_reasons(state["reasons"]),
        )
        if len(str(system.content)) + len(prompt) > PROMPT_CHAR_LIMIT:
            return {
                "claims": [],
                "claims_error": "не хватило контекста для проверки claims",
                "claims_attempts": attempts,
            }
        try:
            claims_obj = await structured_claims.ainvoke(
                [system, HumanMessage(content=prompt)]
            )
            if isinstance(claims_obj, str):
                claims_obj = ClaimList.model_validate_json(claims_obj)
            elif isinstance(claims_obj, dict):
                claims_obj = ClaimList.model_validate(claims_obj)
            elif not isinstance(claims_obj, ClaimList):
                raise TypeError(f"модель вернула {type(claims_obj).__name__}, а не ClaimList")
        except (OutputParserException, ValueError, TypeError) as e:
            reason = f"Не удалось разобрать claims: {str(e)[:1000]}"
            return {"claims": [], "reasons": [reason], "claims_attempts": attempts}
        return {
            "claims": claims_obj.claims,
            "reasons": [],
            "claims_error": None,
            "claims_attempts": attempts,
        }

    async def validate_node(state: AgentState) -> dict[str, Any]:
        await emit_event(on_event, "Проверяю ответ…")
        draft = state["draft"]
        if draft is None:
            verdict = Verdict(ok=False, reasons=state["reasons"] or ["ответ модели не удалось разобрать"])
            return {"verdict": verdict, "reasons": verdict.reasons, "final": ""}

        answer = AgentAnswer(
            answer=draft.answer,
            claims=state["claims"],
            refused=draft.refused,
            requires_confirmation=draft.requires_confirmation,
        )
        if state["claims_error"]:
            verdict = Verdict(ok=False, reasons=[state["claims_error"]])
            return {
                "answer": answer,
                "verdict": verdict,
                "reasons": verdict.reasons,
                "final": draft.answer,
            }
        if state["reasons"]:
            verdict = Verdict(ok=False, reasons=state["reasons"])
            return {
                "answer": answer,
                "verdict": verdict,
                "reasons": verdict.reasons,
                "final": draft.answer,
            }

        verdict = validate(answer, state["evidence"])
        if verdict.ok:
            return {
                "answer": answer,
                "verdict": verdict,
                "reasons": [],
                "final": draft.answer,
            }
        return {
            "answer": answer,
            "verdict": verdict,
            "reasons": verdict.reasons,
            "final": draft.answer,
        }

    def fail_node(state: AgentState) -> dict[str, Any]:
        previews = [
            value
            for call_id, value in state["evidence"].items()
            if not call_id.startswith("m")
            and isinstance(value, dict)
            and "operation" in value
            and "success" not in value
        ]
        if previews:
            preview = previews[-1]
            answer = AgentAnswer(
                answer=(
                    f"Подготовлена операция {preview.get('operation')} для «{preview.get('path')}». "
                    "Изменения не применены. Подтвердите выполнение."
                ),
                claims=[],
                requires_confirmation=True,
            )
            return {
                "answer": answer,
                "claims": [],
                "verdict": Verdict(ok=True, reasons=[]),
                "reasons": [],
                "final": answer.answer,
            }

        draft = state["draft"]
        if draft is not None:
            answer = AgentAnswer(
                answer=draft.answer,
                claims=[],
                refused=draft.refused,
                requires_confirmation=draft.requires_confirmation,
            )
            fallback = state["reasons"] or ["ответ не прошёл проверку"]
            verdict = state["verdict"] or Verdict(ok=False, reasons=fallback)
            if verdict.ok:
                verdict = Verdict(ok=False, reasons=fallback)
            return {
                "answer": answer,
                "claims": [],
                "verdict": verdict,
                "reasons": verdict.reasons,
                "final": draft.answer,
            }

        if state["answer"] is not None:
            verdict = state["verdict"] or Verdict(ok=False, reasons=state["reasons"])
            return {
                "answer": state["answer"],
                "verdict": verdict,
                "reasons": state["reasons"],
                "final": state["answer"].answer,
            }

        answer = fail_safe_answer()
        return {
            "answer": answer,
            "claims": [],
            "verdict": Verdict(ok=False, reasons=state["reasons"]),
            "final": answer.answer,
        }

    def route_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None) and state["steps"] < MAX_STEPS:
            return "tools"
        return "answer"

    def route_answer(state: AgentState) -> str:
        return "fail" if state["draft"] is None else "claims"

    def route_validate(state: AgentState) -> str:
        verdict = state["verdict"]
        if verdict is not None and verdict.ok:
            return "end"
        if state["claims_error"]:
            return "fail"
        if state["draft"] is not None and (
            state["draft"].refused or state["draft"].requires_confirmation
        ):
            return "fail"
        if state["claims_attempts"] < MAX_CLAIMS_ATTEMPTS:
            return "claims"
        return "fail"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("answer", answer_node)
    graph.add_node("claims", claims_node)
    graph.add_node("validate", validate_node)
    graph.add_node("fail", fail_node)

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_agent, {"tools": "tools", "answer": "answer"})
    graph.add_edge("tools", "agent")
    graph.add_conditional_edges(
        "answer", route_answer, {"claims": "claims", "fail": "fail"}
    )
    graph.add_edge("claims", "validate")
    graph.add_conditional_edges(
        "validate", route_validate, {"claims": "claims", "fail": "fail", "end": END}
    )
    graph.add_edge("fail", END)
    return graph.compile()


async def run_agent(
    question: str,
    read_only: bool = False,
    allow_preview: bool = False,
    history: list[dict[str, Any]] | None = None,
    runtime: AgentRuntime | None = None,
    on_event: EventCallback | None = None,
    answer_format: AnswerFormat = "markdown",
) -> dict[str, Any]:
    history_evidence, history_text, fits = prepare_history(history)
    if not fits:
        return context_limit_result(question)

    if runtime is not None:
        tools = await runtime.tools(read_only, allow_preview=allow_preview)
        graph = build_graph(make_model(), tools, on_event, answer_format=answer_format)
        state = initial_state(question, history_text, history_evidence)
        result = await graph.ainvoke(state)
    else:
        transport = StdioTransport(command=sys.executable, args=["-m", "iacpaas_mcp"])
        async with Client(transport) as mcp_client:
            tools = await load_tools(
                mcp_client, read_only=read_only, allow_preview=allow_preview
            )
            graph = build_graph(make_model(), tools, on_event, answer_format=answer_format)
            state = initial_state(question, history_text, history_evidence)
            result = await graph.ainvoke(state)
    return {
        "question": question,
        "final": result["final"],
        "calls": result["calls"],
        "answer": result["answer"].model_dump(mode="json") if result["answer"] else None,
        "verdict": result["verdict"].model_dump() if result["verdict"] else None,
        "reasons": result["reasons"],
        "attempts": result["claims_attempts"] + 1,
        "claims_attempts": result["claims_attempts"],
        "steps": result["steps"],
    }
