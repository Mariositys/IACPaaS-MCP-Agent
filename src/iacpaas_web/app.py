"""Браузерный чат над агентом IACPaaS."""

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from iacpaas_agent.agent import MODEL_NAME, run_agent
from iacpaas_agent.prompts import PROMPT_VERSION
from iacpaas_agent.trace import TRACE_VERSION
from iacpaas_mcp.models import ErrorResult
from iacpaas_mcp.server import read_catalog

INDEX = Path(__file__).with_name("index.html")
app = FastAPI(title="IACPaaS Chat")


class ChatRequest(BaseModel):
    question: str = Field(min_length=1)
    history: list[dict[str, Any]] = Field(default_factory=list)

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
    return {"ok": True, "model": MODEL_NAME, "prompt_version": PROMPT_VERSION}


@app.get("/api/catalog")
async def catalog(query: str = "") -> dict[str, Any]:
    entries = read_catalog()
    if isinstance(entries, ErrorResult):
        return {"items": [], "error": entries.error, "kind": entries.kind}

    needle = query.casefold().strip()
    items = [
        entry.model_dump()
        for entry in entries
        if not needle
        or needle in entry.path.casefold()
        or needle in entry.title.casefold()
        or any(needle in tag.casefold() for tag in entry.tags)
    ]
    return {"items": items}


@app.post("/api/chat")
async def chat(request: ChatRequest) -> Any:
    try:
        result = await run_agent(request.question, read_only=True, history=request.history)
    except Exception as e:  # noqa: BLE001 - ошибка показывается в чате
        message = str(e)
        if "credentials" in message.casefold() or "api_key" in message.casefold():
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Не задан ключ модели (OPENAI_API_KEY или IACPAAS_AGENT_API_KEY).",
                    "kind": "auth",
                },
            )
        return JSONResponse(status_code=500, content={"error": message, "kind": "platform"})

    result["version"] = TRACE_VERSION
    result["prompt_version"] = PROMPT_VERSION
    return result
