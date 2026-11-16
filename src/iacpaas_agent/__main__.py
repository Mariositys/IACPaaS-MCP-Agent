import argparse
import asyncio
import json
import sys

from iacpaas_mcp.config import ALLOW_WRITE, get_apply_token

from .agent import AgentRuntime, run_agent


def _pending_preview(calls):
    for call in reversed(calls or []):
        result = call.get("result") if isinstance(call, dict) else None
        if (
            isinstance(call, dict)
            and str(call.get("tool", "")).startswith("preview_")
            and isinstance(result, dict)
            and "operation" in result
            and "success" not in result
        ):
            return call
    return None


def _confirm(call) -> bool:
    result = call["result"]
    print(f"Операция: {result.get('operation')}", file=sys.stderr)
    print(f"Инфоресурс: {result.get('path')}", file=sys.stderr)
    details = result.get("details") or {}
    if details:
        print(json.dumps(details, ensure_ascii=False, indent=2), file=sys.stderr)
    if not sys.stdin.isatty():
        print("Подтверждение недоступно: stdin не является терминалом.", file=sys.stderr)
        return False
    print("Выполнить операцию? [y/N]: ", end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip().lower() in {"y", "yes"}


async def _apply(runtime: AgentRuntime, call) -> None:
    tool = str(call["tool"]).replace("preview_", "apply_", 1)
    args = dict(call.get("args") or {})
    args["confirmation_token"] = get_apply_token()
    result = await runtime.call_tool(tool, args)
    if isinstance(result, dict) and result.get("success") is True:
        print(f"Операция выполнена: {result.get('operation')} {result.get('path')}", file=sys.stderr)
        return
    if isinstance(result, dict) and result.get("error"):
        print(f"Ошибка: {result['error']} ({result.get('kind', 'platform')})", file=sys.stderr)
        return
    print(f"Операция не выполнена: {result}", file=sys.stderr)


async def _run(question: str) -> None:
    async def emit(event):
        if event.get("type") == "status":
            print(event["message"], file=sys.stderr, flush=True)

    if not ALLOW_WRITE:
        result = await run_agent(question, read_only=True, on_event=emit)
        print(result["final"])
        return

    runtime = AgentRuntime()
    await runtime.start()
    try:
        result = await run_agent(
            question,
            read_only=False,
            allow_preview=True,
            runtime=runtime,
            on_event=emit,
        )
        print(result["final"])
        pending = _pending_preview(result["calls"])
        if pending is None:
            return
        if not _confirm(pending):
            print("Операция отменена.", file=sys.stderr)
            return
        await _apply(runtime, pending)
    finally:
        await runtime.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="iacpaas-agent", description="Агент над IACPaaS")
    parser.add_argument("question", help="вопрос о содержимом инфоресурса")
    args = parser.parse_args()

    try:
        asyncio.run(_run(args.question))
    except Exception as e:  # CLI показывает ошибку и завершается
        print(f"Ошибка: {e}", file=sys.stderr)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
