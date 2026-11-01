"""CLI агента: запуск вопроса, запись трассы или replay без сети."""

import argparse
import asyncio
import sys
from pathlib import Path

from .agent import run_agent
from .trace import replay, write_trace


def main() -> None:
    parser = argparse.ArgumentParser(prog="iacpaas-agent", description="Агент над IACPaaS")
    parser.add_argument("question", nargs="?", help="вопрос о содержимом инфоресурса")
    parser.add_argument("--trace", type=Path, help="записать трассу запуска в JSONL")
    parser.add_argument("--replay", type=Path, help="пересчитать проверку из трассы без сети")
    args = parser.parse_args()

    try:
        if args.replay:
            trace, verdict = replay(args.replay)
            print(trace["final"])
            if not verdict.ok:
                for reason in verdict.reasons:
                    print(f"- {reason}", file=sys.stderr)
            return

        if not args.question:
            parser.error("нужен вопрос или --replay")

        result = asyncio.run(run_agent(args.question))
        if args.trace:
            write_trace(args.trace, result)
        print(result["final"])
    except Exception as e:  # CLI показывает ошибку и завершается
        print(f"Ошибка: {e}", file=sys.stderr)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
