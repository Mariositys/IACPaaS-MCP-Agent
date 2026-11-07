"""JSON-мост для внешних агентов: проверка AgentAnswer по evidence.

Читает один JSON-объект из stdin:
    {"answer": {...}, "evidence": {"call_id": {...}}}
Печатает один JSON-объект в stdout:
    {"ok": bool, "reasons": [...], "final": "..."}
"""

import json
import sys
from typing import Any

from .validator import FAIL_SAFE_TEXT, validate


def _answer_text(answer: Any) -> str:
    if isinstance(answer, dict):
        text = answer.get("answer")
        return text if isinstance(text, str) and text.strip() else FAIL_SAFE_TEXT
    if isinstance(answer, str):
        try:
            parsed = json.loads(answer)
        except json.JSONDecodeError:
            return FAIL_SAFE_TEXT
        if isinstance(parsed, dict) and isinstance(parsed.get("answer"), str):
            return parsed["answer"].strip() or FAIL_SAFE_TEXT
    return FAIL_SAFE_TEXT


def main() -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise TypeError("ожидался JSON-объект")
        answer = payload.get("answer")
        evidence = payload.get("evidence") or {}
        if not isinstance(evidence, dict):
            raise TypeError("evidence должен быть объектом")
        verdict = validate(answer, evidence)
    except Exception as exc:  # noqa: BLE001 - мост обязан вернуть JSON даже при ошибке
        result = {"ok": False, "reasons": [f"bridge: {exc}"], "final": FAIL_SAFE_TEXT}
    else:
        result = {
            "ok": verdict.ok,
            "reasons": verdict.reasons,
            "final": _answer_text(answer) if verdict.ok else FAIL_SAFE_TEXT,
        }

    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
