"""Детерминированная проверка ответа агента по результатам вызовов тулзов."""

import ast
import json
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


def same_value(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return abs(float(expected) - float(actual)) <= 1e-6
    return expected == actual


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
    if not answer.refused and not answer.claims:
        reasons.append("в ответе нет утверждений с источниками")
    if not answer.refused and not evidence:
        reasons.append("агент не вызвал ни одного тулза")

    for claim in answer.claims:
        reasons.extend(_check_claim(claim, evidence))

    return Verdict(ok=not reasons, reasons=reasons)


def _check_claim(claim: Claim, evidence: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
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

        if claim.value is not None and not same_value(claim.value, actual):
            reasons.append(f"«{claim.statement}»: ожидалось {claim.value!r}, в данных {actual!r}")

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
