"""Запись трассы запуска и воспроизведение проверки без сети."""

import json
import sys
from pathlib import Path
from typing import Any

from .prompts import PROMPT_VERSION
from .schemas import AgentAnswer
from .validator import Verdict, validate

TRACE_VERSION = 1


def write_trace(path: Path, result: dict[str, Any]) -> None:
    trace = {
        "version": TRACE_VERSION,
        "prompt_version": PROMPT_VERSION,
        "question": result["question"],
        "calls": result["calls"],
        "answer": result["answer"],
        "verdict": result["verdict"],
        "final": result["final"],
        "attempts": result["attempts"],
        "steps": result["steps"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(trace, ensure_ascii=False, default=str) + "\n")


def read_trace(path: Path) -> dict[str, Any]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"трасса {path} пуста")
    return json.loads(lines[-1])


def replay(path: Path) -> tuple[dict[str, Any], Verdict]:
    """Пересчитать вердикт по записанным данным, не обращаясь к LLM и MCP."""
    trace = read_trace(path)
    answer = AgentAnswer.model_validate(trace["answer"])
    evidence = {call["call_id"]: call["result"] for call in trace["calls"]}
    verdict = validate(answer, evidence)

    recorded = trace.get("verdict") or {}
    if recorded.get("ok") != verdict.ok:
        print("Предупреждение: результат проверки не совпал с записанным", file=sys.stderr)
    return trace, verdict
