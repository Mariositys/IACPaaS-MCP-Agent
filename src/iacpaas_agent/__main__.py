"""CLI агента: iacpaas-agent "вопрос"."""

import argparse
import asyncio
import sys

from .agent import run_agent


async def _run(question: str):
    async def emit(event):
        if event.get("type") == "status":
            print(event["message"], file=sys.stderr, flush=True)

    return await run_agent(question, on_event=emit)


def main() -> None:
    parser = argparse.ArgumentParser(prog="iacpaas-agent", description="Агент над IACPaaS")
    parser.add_argument("question", help="вопрос о содержимом инфоресурса")
    args = parser.parse_args()

    try:
        result = asyncio.run(_run(args.question))
        print(result["final"])
    except Exception as e:  # CLI показывает ошибку и завершается
        print(f"Ошибка: {e}", file=sys.stderr)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
