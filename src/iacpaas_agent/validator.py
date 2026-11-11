"""Детерминированная проверка ответа агента по результатам вызовов тулзов."""

import ast
import json
import re
import unicodedata
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .schemas import AgentAnswer, Claim

FAIL_SAFE_TEXT = "Не удалось подтвердить ответ данными IACPaaS."


class Verdict(BaseModel):
    ok: bool
    reasons: list[str] = Field(default_factory=list)


def resolve_pointer(data: Any, pointer: str) -> Any:
    """Разрешить JSON Pointer. Пустая строка - весь результат."""
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
    """Привести текст к сравнимому виду без синонимов и стемминга."""
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
    """Проверить, что цитата встречается в значении по указателю."""
    haystack = actual if isinstance(actual, str) else json.dumps(actual, ensure_ascii=False, sort_keys=True)
    return normalize_text(quote) in normalize_text(haystack)


def evaluate_expr(expr: str, root: Any) -> Any:
    """Посчитать формулу с ref('/pointer') и sum/avg/min/max/count."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"синтаксис: {e.msg}") from e
    if sum(1 for _ in ast.walk(tree)) > 100:
        raise ValueError("формула слишком большая")
    return _eval_node(tree.body, root)


def _eval_node(node: ast.AST, root: Any) -> Any:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError("в формуле допустимы только числа")

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_node(node.operand, root)
        return value if isinstance(node.op, ast.UAdd) else -value

    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        left = _eval_node(node.left, root)
        right = _eval_node(node.right, root)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            raise ValueError("деление на ноль")
        return left / right

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        if name == "ref":
            arg = node.args[0] if len(node.args) == 1 else None
            if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                raise ValueError("ref принимает строку с JSON Pointer")
            return resolve_pointer(root, arg.value)
        if name in {"sum", "avg", "min", "max", "count"}:
            if len(node.args) != 1:
                raise ValueError(f"{name} принимает один аргумент")
            values = _eval_node(node.args[0], root)
            if not isinstance(values, list):
                raise ValueError(f"{name} ожидает список")
            if name == "count":
                return len(values)
            if not values:
                raise ValueError(f"{name} от пустого списка")
            numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if len(numbers) != len(values):
                raise ValueError(f"{name} ожидает числа")
            if name == "sum":
                return sum(numbers)
            if name == "avg":
                return sum(numbers) / len(numbers)
            if name == "min":
                return min(numbers)
            return max(numbers)

    raise ValueError("недопустимое выражение")


def validate(answer: AgentAnswer | str | dict[str, Any], evidence: dict[str, Any]) -> Verdict:
    """Проверить ответ по накопленным результатам тулзов."""
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
    if not answer.refused and not answer.requires_confirmation and not answer.claims:
        reasons.append("в ответе нет утверждений с источниками")
    if not answer.refused and not evidence:
        reasons.append("агент не вызвал ни одного тулза")

    reasons.extend(_check_write_preview(answer, evidence))
    for claim in answer.claims:
        reasons.extend(_check_claim(claim, evidence))

    return Verdict(ok=not reasons, reasons=reasons)


def _check_write_preview(answer: AgentAnswer, evidence: dict[str, Any]) -> list[str]:
    def is_preview(value: Any) -> bool:
        return isinstance(value, dict) and "operation" in value and "success" not in value

    all_previews = [call_id for call_id, value in evidence.items() if is_preview(value)]
    source_ids = {source.call_id for claim in answer.claims for source in claim.sources}
    relevant_previews = [call_id for call_id in source_ids if is_preview(evidence.get(call_id))]

    text = " ".join([answer.answer, *(claim.statement for claim in answer.claims)]).casefold()
    success_pattern = re.compile(
        r"\b(?:удал[её]н(?:а|о|ы)?|измен[её]н(?:а|о|ы)?|"
        r"выполнен(?:а|о|ы)?|обновл[её]н(?:а|о|ы)?|успешно)\b"
    )
    if answer.requires_confirmation:
        if not all_previews:
            return ["requires_confirmation=true без OperationPreview"]
        if success_pattern.search(text):
            return ["OperationPreview не является выполнением операции"]
        return []

    if relevant_previews and success_pattern.search(text):
        return ["write-тулз вернул OperationPreview; нельзя утверждать, что операция выполнена"]
    return []


def aggregate_kind(statement: str) -> str | None:
    text = normalize_text(statement)
    if "сумм" in text:
        return "sum"
    if "средн" in text or "average" in text:
        return "avg"
    if "максим" in text or "maximum" in text:
        return "max"
    if "миним" in text or "minimum" in text:
        return "min"
    return None


def is_range_statement(statement: str) -> bool:
    text = normalize_text(statement)
    return ("от " in text and " до " in text) or "диапазон" in text or "интервал" in text


def is_presence_statement(statement: str) -> bool:
    return "присутству" in normalize_text(statement)


def value_matches(claim_value: Any, actual: Any) -> bool:
    if same_value(claim_value, actual):
        return True
    if isinstance(claim_value, str) and isinstance(actual, str):
        return normalize_text(claim_value) in normalize_text(actual)
    return False


def aggregate_values(kind: str, values: list[Any]) -> Any:
    numbers = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
    if len(numbers) != len(values) or not numbers:
        raise ValueError("для агрегата нужны числовые значения")
    if kind == "sum":
        return sum(numbers)
    if kind == "avg":
        return sum(numbers) / len(numbers)
    if kind == "max":
        return max(numbers)
    return min(numbers)


def _check_claim(claim: Claim, evidence: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    has_expr = any(source.expr for source in claim.sources)
    if claim.value is None and claim.quote is None and not has_expr:
        return [f"«{claim.statement}»: нет value или quote для проверки"]

    quote_ok = False
    actuals: list[Any] = []
    plain_actuals: list[Any] = []
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

        if source.expr:
            if claim.value is None:
                reasons.append(f"для формулы '{source.expr}' не задано значение")
                continue
            try:
                actual = evaluate_expr(source.expr, root)
            except (ValueError, TypeError, ZeroDivisionError) as e:
                reasons.append(f"ошибка формулы '{source.expr}': {e}")
                continue
            if not same_value(claim.value, actual):
                reasons.append(f"«{claim.statement}»: ожидалось {claim.value!r}, в данных {actual!r}")
            actuals.append(actual)
        else:
            actuals.append(actual)
            plain_actuals.append(actual)
        if claim.quote is not None and quote_matches(claim.quote, actual):
            quote_ok = True

    if claim.value is not None:
        kind = aggregate_kind(claim.statement) if len(plain_actuals) > 1 and not has_expr else None
        if kind is not None:
            try:
                calculated = aggregate_values(kind, plain_actuals)
            except ValueError as e:
                reasons.append(f"«{claim.statement}»: {e}")
            else:
                if not same_value(claim.value, calculated):
                    reasons.append(f"«{claim.statement}»: ожидалось {claim.value!r}, в данных {calculated!r}")
        elif not has_expr:
            if is_presence_statement(claim.statement) and any(actual == 1 for actual in plain_actuals):
                pass
            elif is_range_statement(claim.statement):
                numbers = [
                    value
                    for value in plain_actuals
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                ]
                if len(set(numbers)) >= 2 and any(
                    same_value(claim.value, endpoint) for endpoint in (min(numbers), max(numbers))
                ):
                    pass
                else:
                    reasons.append(
                        f"«{claim.statement}»: ожидалось значение на границе диапазона, "
                        f"в данных {plain_actuals!r}"
                    )
            elif not any(value_matches(claim.value, actual) for actual in plain_actuals):
                actual = plain_actuals[0] if plain_actuals else None
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
