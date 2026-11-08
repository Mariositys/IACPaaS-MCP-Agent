"""Браузерный чат над агентом IACPaaS."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
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
    return INDEX.read_text(encoding="utf-8")


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


@app.post("/api/chat")
async def chat(request: ChatRequest, http_request: Request) -> Any:
    dialogue_id = request.dialogue_id
    history = store.get_messages(dialogue_id) if dialogue_id and store.get_dialogue(dialogue_id) else []
    runtime = getattr(http_request.app.state, "agent_runtime", None)

    try:
        result = await run_agent(request.question, read_only=False, history=history, runtime=runtime)
    except Exception as e:  # noqa: BLE001 - ошибка показывается в чате
        message = str(e)
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

    result["dialogue_id"] = store.save_message(dialogue_id, request.question, result)
    return result
