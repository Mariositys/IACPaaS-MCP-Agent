"""Цикл агента: сбор данных через MCP, структурированный ответ, проверка и repair."""

import json
import os
import sys
from typing import Annotated, Any, TypedDict

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from .prompts import ANSWER_PROMPT, GATHER_PROMPT, SYSTEM_PROMPT
from .schemas import AgentAnswer
from .validator import Verdict, fail_safe_answer, validate

MODEL_NAME = os.getenv("IACPAAS_AGENT_MODEL", "gpt-4o-mini")
BASE_URL = os.getenv("IACPAAS_AGENT_BASE_URL")
API_KEY = os.getenv("IACPAAS_AGENT_API_KEY") or os.getenv("OPENAI_API_KEY")

MAX_STEPS = 8
MAX_ATTEMPTS = 3


class AgentState(TypedDict):
    question: str
    messages: Annotated[list[AnyMessage], add_messages]
    evidence: dict[str, Any]
    answer: AgentAnswer | None
    verdict: Verdict | None
    reasons: list[str]
    attempts: int
    steps: int
    final: str


def make_model() -> ChatOpenAI:
    kwargs: dict[str, Any] = {"model": MODEL_NAME, "temperature": 0}
    if BASE_URL:
        kwargs["base_url"] = BASE_URL
    if API_KEY:
        kwargs["api_key"] = API_KEY
    return ChatOpenAI(**kwargs)


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


async def load_tools(mcp_client: Client) -> list[StructuredTool]:
    """Обернуть MCP-тулзы в LangChain-тулзы, сохранив их JSON Schema."""
    tools = []
    for spec in await mcp_client.list_tools():
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


def initial_state(question: str) -> AgentState:
    return {
        "question": question,
        "messages": [SystemMessage(content=GATHER_PROMPT), HumanMessage(content=question)],
        "evidence": {},
        "answer": None,
        "verdict": None,
        "reasons": [],
        "attempts": 0,
        "steps": 0,
        "final": "",
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
            messages.append(
                ToolMessage(content=json.dumps(result, ensure_ascii=False, default=str), tool_call_id=call_id)
            )
        return {"messages": messages, "evidence": evidence}

    async def answer_node(state: AgentState) -> dict[str, Any]:
        reasons = ""
        if state["reasons"]:
            reasons = "Предыдущая проверка не прошла:\n" + "\n".join(f"- {r}" for r in state["reasons"])
        prompt = ANSWER_PROMPT.format(
            question=state["question"],
            evidence=json.dumps(state["evidence"], ensure_ascii=False, indent=2, default=str),
            reasons=reasons,
        )
        answer = await structured_model.ainvoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)])
        if isinstance(answer, dict):
            answer = AgentAnswer.model_validate(answer)
        return {"answer": answer, "attempts": state["attempts"] + 1}

    def validate_node(state: AgentState) -> dict[str, Any]:
        verdict = validate(state["answer"], state["evidence"])
        if verdict.ok and state["answer"] is not None:
            return {"verdict": verdict, "final": state["answer"].answer}
        return {"verdict": verdict, "reasons": verdict.reasons}

    def fail_node(state: AgentState) -> dict[str, Any]:
        answer = fail_safe_answer()
        return {"answer": answer, "final": answer.answer}

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


async def run_agent(question: str) -> str:
    transport = StdioTransport(command=sys.executable, args=["-m", "iacpaas_mcp"])
    async with Client(transport) as mcp_client:
        tools = await load_tools(mcp_client)
        graph = build_graph(make_model(), tools)
        result = await graph.ainvoke(initial_state(question))
    return result["final"]
