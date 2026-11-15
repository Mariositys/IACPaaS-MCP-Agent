import json
import unicodedata
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .schemas import AgentAnswer, Claim

FAIL_SAFE_TEXT = "Не удалось подтвердить ответ данными IACPaaS."


class Verdict(BaseModel):
    ok: bool
    reasons: list[str] = Field(default_factory=list)


def resolve_pointer(data: Any, pointer: str) -> Any:
    if pointer == "":
        return data
    current = data
    for raw in pointer.lstrip("/").split("/"):
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(part)
    return current


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value)
    text = text.translate(str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "–": "-", "—": "-", "−": "-"}))
    return " ".join(text.casefold().split())


def same_value(expected: Any, actual: Any) -> bool:
    if isinstance(expected, str) and isinstance(actual, str):
        return normalize_text(expected) == normalize_text(actual)
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(expected) - float(actual)) <= 1e-6
    return expected == actual


def quote_matches(quote: str, actual: Any) -> bool:
    haystack = actual if isinstance(actual, str) else json.dumps(actual, ensure_ascii=False, sort_keys=True)
    return normalize_text(quote) in normalize_text(haystack)


def validate(answer: AgentAnswer | str | dict[str, Any], evidence: dict[str, Any]) -> Verdict:
    if isinstance(answer, str):
        try:
            answer = json.loads(answer)
        except json.JSONDecodeError as e:
            return Verdict(ok=False, reasons=[f"ответ не JSON: {e}"])
    if isinstance(answer, dict):
        try:
            answer = AgentAnswer.model_validate(answer)
        except ValidationError as e:
            reasons = [f"{'/'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:5]]
            return Verdict(ok=False, reasons=reasons)
    if not isinstance(answer, AgentAnswer):
        return Verdict(ok=False, reasons=["ответ не является AgentAnswer"])

    reasons: list[str] = []
    if not answer.refused and answer.claims and not evidence:
        reasons.append("агент не вызвал ни одного тулза")

    reasons.extend(_check_write_preview(answer, evidence))
    for claim in answer.claims:
        reasons.extend(_check_claim(claim, evidence))

    return Verdict(ok=not reasons, reasons=reasons)


def _check_write_preview(answer: AgentAnswer, evidence: dict[str, Any]) -> list[str]:
    def is_preview(value: Any) -> bool:
        return isinstance(value, dict) and "operation" in value and "success" not in value

    if answer.requires_confirmation and not any(is_preview(value) for value in evidence.values()):
        return ["requires_confirmation=true без OperationPreview"]
    return []


def _check_claim(claim: Claim, evidence: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if claim.value is None and claim.quote is None:
        return [f"«{claim.statement}»: нет value или quote для проверки"]

    quote_ok = False
    actuals: list[Any] = []
    for source in claim.sources:
        if source.call_id not in evidence:
            reasons.append(f"источник '{source.call_id}' не найден среди вызовов")
            continue
        root = evidence[source.call_id]
        if isinstance(root, dict) and "error" in root and "kind" in root:
            reasons.append(f"источник '{source.call_id}' - ошибка тулза, а не данные")
            continue
        try:
            actual = resolve_pointer(root, source.pointer)
        except (KeyError, IndexError, ValueError, TypeError):
            reasons.append(f"в '{source.call_id}' нет указателя '{source.pointer}'")
            continue

        actuals.append(actual)
        if claim.quote is not None and quote_matches(claim.quote, actual):
            quote_ok = True

    if claim.value is not None and not any(same_value(claim.value, actual) for actual in actuals):
        actual = actuals[0] if actuals else None
        reasons.append(f"«{claim.statement}»: ожидалось {claim.value!r}, в данных {actual!r}")

    if claim.quote is not None and not quote_ok:
        reasons.append(f"«{claim.statement}»: цитата не найдена в источнике")

    if any(
        isinstance(evidence.get(source.call_id), dict) and evidence[source.call_id].get("truncated")
        for source in claim.sources
    ):
        text = claim.statement.casefold()
        if any(word in text for word in ("всего", "итого", "количество", "число", "total")):
            reasons.append("данные обрезаны, нельзя утверждать общее количество")

    return reasons


def fail_safe_answer() -> AgentAnswer:
    return AgentAnswer(answer=FAIL_SAFE_TEXT, claims=[], refused=True)
