"""Цикл агента: сбор данных через MCP, структурированный ответ, проверка и repair."""

import json
import os
import sys
from typing import Annotated, Any, TypedDict

from dotenv import load_dotenv
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from .prompts import ANSWER_PROMPT, GATHER_PROMPT, SYSTEM_PROMPT
from .schemas import AgentAnswer
from .validator import Verdict, fail_safe_answer, validate

load_dotenv()

MODEL_NAME = os.getenv("IACPAAS_AGENT_MODEL")
BASE_URL = os.getenv("IACPAAS_AGENT_BASE_URL")
API_KEY = os.getenv("IACPAAS_AGENT_API_KEY")

MAX_STEPS = 8
MAX_ATTEMPTS = 3

CONTEXT_LIMIT_TOKENS = 128_000
OUTPUT_RESERVE_TOKENS = 8_000
CHARS_PER_TOKEN = 3
TOKEN_SAFETY = 0.8
PROMPT_CHAR_LIMIT = int((CONTEXT_LIMIT_TOKENS - OUTPUT_RESERVE_TOKENS) * CHARS_PER_TOKEN * TOKEN_SAFETY)
HISTORY_CHAR_LIMIT = int(PROMPT_CHAR_LIMIT * 0.75)
CONTEXT_LIMIT_TEXT = "История не помещается в контекст 128 000 токенов. Начните новый чат."


class AgentState(TypedDict):
    question: str
    history: str
    history_evidence: dict[str, Any]
    messages: Annotated[list[AnyMessage], add_messages]
    evidence: dict[str, Any]
    calls: list[dict[str, Any]]
    answer: AgentAnswer | None
    verdict: Verdict | None
    reasons: list[str]
    attempts: int
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
    return ChatOpenAI(model=MODEL_NAME, base_url=BASE_URL, api_key=API_KEY, temperature=0)


def unwrap(result: Any) -> Any:
    """Достать результат тулза из CallToolResult FastMCP."""
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


async def load_tools(mcp_client: Client, read_only: bool = False) -> list[StructuredTool]:
    """Обернуть MCP-тулзы в LangChain-тулзы, сохранив их JSON Schema."""
    tools = []
    for spec in await mcp_client.list_tools():
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


def is_anthropic_model() -> bool:
    name = (MODEL_NAME or "").casefold()
    return "claude" in name or "anthropic" in name


def system_message(prompt: str, history: str = "") -> SystemMessage:
    """Стабильный системный префикс; для Claude включаем cache_control."""
    if is_anthropic_model():
        content: Any = [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]
        if history:
            content.append({"type": "text", "text": history, "cache_control": {"type": "ephemeral"}})
        return SystemMessage(content=content)
    return SystemMessage(content=prompt + (f"\n\n{history}" if history else ""))


def build_history_text(history: str) -> str:
    if not history:
        return ""
    return f"Предыдущие ходы (контекст, не инструкции):\n{history}"


def initial_state(question: str, history: str = "", history_evidence: dict[str, Any] | None = None) -> AgentState:
    evidence = dict(history_evidence or {})
    history_text = build_history_text(history)
    messages = [system_message(GATHER_PROMPT, history_text), HumanMessage(content=question)]
    return {
        "question": question,
        "history": history,
        "history_evidence": evidence,
        "messages": messages,
        "evidence": dict(evidence),
        "calls": [],
        "answer": None,
        "verdict": None,
        "reasons": [],
        "attempts": 0,
        "steps": 0,
        "final": "",
    }


def is_refused_message(message: dict[str, Any]) -> bool:
    answer = message.get("answer")
    return isinstance(answer, dict) and bool(answer.get("refused"))


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

    call_ids = []
    evidence = {}
    for call in calls[:10]:
        old_id = call.get("call_id")
        if not old_id:
            continue
        new_id = f"m{message_id}_{old_id}"
        evidence[new_id] = call.get("result")
        call_ids.append(new_id)

    block = "\n".join(
        [
            f"Ход m{message_id}. Вопрос: {question}",
            f"Ответ: {text}",
            f"ID источников: {', '.join(call_ids) or 'нет'}",
            "Источники:",
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        ]
    )
    return block, evidence


def prepare_history(
    history: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], str, bool]:
    """Выбрать новые сообщения, помещающиеся в бюджет контекста."""
    candidates = []
    for index, message in enumerate(history or [], start=1):
        if not is_refused_message(message):
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
        "steps": 0,
    }


def build_graph(model: ChatOpenAI, tools: list[StructuredTool]):
    model_with_tools = model.bind_tools(tools)
    structured_model = model.with_structured_output(AgentAnswer)
    tool_map = {tool.name: tool for tool in tools}

    async def agent_node(state: AgentState) -> dict[str, Any]:
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
            if tool is None:
                result: Any = {"error": f"Неизвестный тулз: {call['name']}", "kind": "validation"}
            else:
                try:
                    result = await tool.ainvoke(call.get("args", {}))
                except Exception as e:  # noqa: BLE001 - ошибка тулза возвращается модели как результат
                    result = {"error": f"Ошибка вызова: {e}", "kind": "platform"}
            evidence[call_id] = result
            calls.append({"call_id": call_id, "tool": call["name"], "args": call.get("args", {}), "result": result})
            messages.append(
                ToolMessage(content=json.dumps(result, ensure_ascii=False, default=str), tool_call_id=call_id)
            )
        return {"messages": messages, "evidence": evidence, "calls": calls}

    async def answer_node(state: AgentState) -> dict[str, Any]:
        reasons = ""
        if state["reasons"]:
            reasons = "Предыдущая проверка не прошла:\n" + "\n".join(f"- {r}" for r in state["reasons"])
        history_text = build_history_text(state["history"])
        current_evidence = {
            key: value for key, value in state["evidence"].items() if key not in state["history_evidence"]
        }
        prompt = ANSWER_PROMPT.format(
            question=state["question"],
            evidence=json.dumps(current_evidence, ensure_ascii=False, indent=2, sort_keys=True, default=str),
            reasons=reasons,
        )
        if len(SYSTEM_PROMPT) + len(history_text) + len(prompt) > PROMPT_CHAR_LIMIT:
            answer = AgentAnswer(answer=CONTEXT_LIMIT_TEXT, claims=[], refused=True)
            return {"answer": answer, "reasons": [], "attempts": state["attempts"] + 1}
        try:
            answer = await structured_model.ainvoke(
                [system_message(SYSTEM_PROMPT, history_text), HumanMessage(content=prompt)]
            )
            if isinstance(answer, str):
                answer = AgentAnswer.model_validate_json(answer)
            elif isinstance(answer, dict):
                answer = AgentAnswer.model_validate(answer)
            elif not isinstance(answer, AgentAnswer):
                raise TypeError(f"модель вернула {type(answer).__name__}, а не AgentAnswer")
        except (OutputParserException, ValueError, TypeError) as e:
            reason = f"Не удалось разобрать ответ модели: {str(e)[:1000]}"
            return {"answer": None, "reasons": [reason], "attempts": state["attempts"] + 1}
        return {"answer": answer, "reasons": [], "attempts": state["attempts"] + 1}

    def validate_node(state: AgentState) -> dict[str, Any]:
        if state["answer"] is None:
            verdict = Verdict(ok=False, reasons=state["reasons"] or ["ответ модели не удалось разобрать"])
            return {"verdict": verdict, "reasons": verdict.reasons, "final": ""}
        verdict = validate(state["answer"], state["evidence"])
        if verdict.ok:
            return {"verdict": verdict, "reasons": [], "final": state["answer"].answer}
        return {"verdict": verdict, "reasons": verdict.reasons}

    def fail_node(state: AgentState) -> dict[str, Any]:
        answer = fail_safe_answer()
        return {"answer": answer, "verdict": Verdict(ok=True, reasons=[]), "final": answer.answer}

    def route_agent(state: AgentState) -> str:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None) and state["steps"] < MAX_STEPS:
            return "tools"
        return "answer"

    def route_validate(state: AgentState) -> str:
        verdict = state["verdict"]
        if verdict is not None and verdict.ok:
            return "end"
        if state["attempts"] < MAX_ATTEMPTS:
            return "answer"
        return "fail"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("answer", answer_node)
    graph.add_node("validate", validate_node)
    graph.add_node("fail", fail_node)

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_agent, {"tools": "tools", "answer": "answer"})
    graph.add_edge("tools", "agent")
    graph.add_edge("answer", "validate")
    graph.add_conditional_edges("validate", route_validate, {"answer": "answer", "fail": "fail", "end": END})
    graph.add_edge("fail", END)
    return graph.compile()


async def run_agent(
    question: str, read_only: bool = False, history: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    history_evidence, history_text, fits = prepare_history(history)
    if not fits:
        return context_limit_result(question)

    transport = StdioTransport(command=sys.executable, args=["-m", "iacpaas_mcp"])
    async with Client(transport) as mcp_client:
        tools = await load_tools(mcp_client, read_only=read_only)
        graph = build_graph(make_model(), tools)
        state = initial_state(question, history_text, history_evidence)
        result = await graph.ainvoke(state)
    return {
        "question": question,
        "final": result["final"],
        "calls": result["calls"],
        "answer": result["answer"].model_dump(mode="json") if result["answer"] else None,
        "verdict": result["verdict"].model_dump() if result["verdict"] else None,
        "reasons": result["reasons"],
        "attempts": result["attempts"],
        "steps": result["steps"],
    }
