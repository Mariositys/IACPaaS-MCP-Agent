import asyncio
import json
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from iacpaas_agent.agent import MODEL_NAME, WRITE_TOOLS, AgentRuntime, run_agent
from iacpaas_mcp.config import get_apply_token

from . import store
from .markdown_render import render_markdown

INDEX = Path(__file__).with_name("index.html")
STATIC = Path(__file__).with_name("static")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init_db()
    runtime = AgentRuntime()
    _app.state.agent_runtime = runtime
    try:
        await runtime.start()
    except Exception as exc:  # noqa: BLE001 - веб-сервер должен запуститься для диагностики
        logger.warning("MCP runtime не запущен: %s", exc)
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(title="IACPaaS Chat", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(Exception)
async def unhandled_exception(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Необработанная ошибка веб-чата", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"error": f"Внутренняя ошибка сервера: {exc}", "kind": "platform"},
    )


class ChatRequest(BaseModel):
    question: str = Field(min_length=1)
    dialogue_id: str | None = None

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("вопрос пуст")
        return value


class OperationRequest(BaseModel):
    dialogue_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    stamp = max((STATIC / name).stat().st_mtime for name in ("app.js", "style.css"))
    html = INDEX.read_text(encoding="utf-8")
    return html.replace("{{asset_version}}", str(int(stamp)))


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "model": MODEL_NAME or "не задана"}


@app.get("/api/dialogues")
async def dialogues() -> dict[str, Any]:
    return {"items": store.list_dialogues()}


@app.get("/api/dialogues/{dialogue_id}")
async def dialogue(dialogue_id: str) -> Any:
    current = store.get_dialogue(dialogue_id)
    if current is None:
        return JSONResponse(status_code=404, content={"error": "Чат не найден", "kind": "not_found"})
    messages = [_with_answer_html(message) for message in store.get_messages(dialogue_id)]
    return {"dialogue": current, "messages": messages}


def _pending_preview(dialogue_id: str, call_id: str) -> dict[str, Any] | None:
    for message in reversed(store.get_messages(dialogue_id)):
        for call in message.get("calls") or []:
            if call.get("call_id") != call_id:
                continue
            result = call.get("result")
            if (
                isinstance(result, dict)
                and "operation" in result
                and "success" not in result
                and call.get("status", "pending") == "pending"
            ):
                return call
    return None


@app.post("/api/operations/confirm")
async def confirm_operation(request: OperationRequest, http_request: Request) -> Any:
    call = _pending_preview(request.dialogue_id, request.call_id)
    if call is None:
        return JSONResponse(status_code=404, content={"error": "Операция не найдена", "kind": "not_found"})
    tool = call.get("tool")
    if tool not in WRITE_TOOLS:
        return JSONResponse(status_code=400, content={"error": "Это не preview-операция", "kind": "validation"})
    apply_tool = tool.replace("preview_", "apply_", 1)
    runtime = getattr(http_request.app.state, "agent_runtime", None)
    if runtime is None:
        return JSONResponse(status_code=503, content={"error": "MCP runtime не запущен", "kind": "platform"})

    args = dict(call.get("args") or {})
    args["confirmation_token"] = get_apply_token()
    try:
        result = await runtime.call_tool(apply_tool, args)
    except Exception as exc:  # noqa: BLE001 - ошибка возвращается в UI
        logger.exception("Ошибка подтверждения операции")
        result = {"error": str(exc), "kind": "platform"}
    failed = isinstance(result, dict) and bool(result.get("error"))
    store.update_call(
        request.dialogue_id,
        request.call_id,
        status="failed" if failed else "confirmed",
        operation_result=result,
    )
    return {"ok": not failed, "result": result}


@app.post("/api/operations/cancel")
async def cancel_operation(request: OperationRequest) -> Any:
    call = _pending_preview(request.dialogue_id, request.call_id)
    if call is None:
        return JSONResponse(status_code=404, content={"error": "Операция не найдена", "kind": "not_found"})
    store.update_call(request.dialogue_id, request.call_id, status="cancelled")
    return {"ok": True}


def _chat_error(exc: Exception) -> dict[str, Any]:
    message = str(exc)
    lower = message.casefold()
    if "credentials" in lower or "api_key" in lower or "iacpaas_agent" in lower:
        return {
            "error": (
                "Не заданы параметры модели: IACPAAS_AGENT_API_KEY, "
                "IACPAAS_AGENT_MODEL, IACPAAS_AGENT_BASE_URL."
            ),
            "kind": "auth",
        }
    return {"error": message, "kind": "platform"}


def _with_answer_html(result: dict[str, Any]) -> dict[str, Any]:
    answer = result.get("answer")
    text = answer.get("answer") if isinstance(answer, dict) else None
    if not isinstance(text, str) or not text:
        text = result.get("final")
    result["answer_html"] = render_markdown(text if isinstance(text, str) else "")
    return result


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest, http_request: Request) -> StreamingResponse:
    dialogue_id = request.dialogue_id
    history = store.get_messages(dialogue_id) if dialogue_id and store.get_dialogue(dialogue_id) else []
    runtime = getattr(http_request.app.state, "agent_runtime", None)

    async def generate():
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def emit(event: dict[str, Any]) -> None:
            if event.get("type") == "answer":
                event = dict(event)
                answer = event.get("answer")
                text = answer.get("answer") if isinstance(answer, dict) else None
                if not isinstance(text, str) or not text:
                    text = event.get("final")
                event["answer_html"] = render_markdown(text if isinstance(text, str) else "")
            await queue.put(event)

        async def work() -> None:
            try:
                result = await run_agent(
                    request.question,
                    read_only=False,
                    allow_preview=True,
                    history=history,
                    runtime=runtime,
                    on_event=emit,
                )
                result = _with_answer_html(result)
                result["dialogue_id"] = store.save_message(dialogue_id, request.question, result)
                await queue.put({"type": "final", "result": result})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - ошибка передается клиенту событием
                logger.exception("Ошибка /api/chat/stream")
                await queue.put({"type": "error", **_chat_error(exc)})

        task = asyncio.create_task(work())
        try:
            while True:
                event = await queue.get()
                yield _sse(event)
                if event.get("type") in {"final", "error"}:
                    break
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
