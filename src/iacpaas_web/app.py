"""Браузерный чат над агентом IACPaaS."""

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

from iacpaas_agent.agent import MODEL_NAME, AgentRuntime, run_agent
from iacpaas_agent.prompts import PROMPT_VERSION

from . import store

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


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    stamp = max((STATIC / name).stat().st_mtime for name in ("app.js", "style.css"))
    html = INDEX.read_text(encoding="utf-8")
    return html.replace("{{asset_version}}", str(int(stamp)))


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "model": MODEL_NAME or "не задана", "prompt_version": PROMPT_VERSION}


@app.get("/api/dialogues")
async def dialogues() -> dict[str, Any]:
    return {"items": store.list_dialogues()}


@app.get("/api/dialogues/{dialogue_id}")
async def dialogue(dialogue_id: str) -> Any:
    current = store.get_dialogue(dialogue_id)
    if current is None:
        return JSONResponse(status_code=404, content={"error": "Чат не найден", "kind": "not_found"})
    return {"dialogue": current, "messages": store.get_messages(dialogue_id)}


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
            await queue.put(event)

        async def work() -> None:
            try:
                result = await run_agent(
                    request.question,
                    read_only=False,
                    history=history,
                    runtime=runtime,
                    on_event=emit,
                )
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


@app.post("/api/chat")
async def chat(request: ChatRequest, http_request: Request) -> Any:
    try:
        dialogue_id = request.dialogue_id
        history = store.get_messages(dialogue_id) if dialogue_id and store.get_dialogue(dialogue_id) else []
        runtime = getattr(http_request.app.state, "agent_runtime", None)
        result = await run_agent(request.question, read_only=False, history=history, runtime=runtime)
        result["dialogue_id"] = store.save_message(dialogue_id, request.question, result)
        return result
    except Exception as e:  # noqa: BLE001 - ошибка показывается в чате
        message = str(e)
        logger.exception("Ошибка /api/chat")
        lower = message.casefold()
        if "credentials" in lower or "api_key" in lower or "iacpaas_agent" in lower:
            return JSONResponse(
                status_code=500,
                content={
                    "error": (
                        "Не заданы параметры модели: IACPAAS_AGENT_API_KEY, "
                        "IACPAAS_AGENT_MODEL, IACPAAS_AGENT_BASE_URL."
                    ),
                    "kind": "auth",
                },
            )
        return JSONResponse(status_code=500, content={"error": message, "kind": "platform"})
