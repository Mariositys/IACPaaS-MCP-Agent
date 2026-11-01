"""CLI агента: iacpaas-agent "вопрос"."""

import argparse
import asyncio

from .agent import run_agent


def main() -> None:
    parser = argparse.ArgumentParser(prog="iacpaas-agent", description="Агент над IACPaaS")
    parser.add_argument("question", help="вопрос о содержимом инфоресурса")
    args = parser.parse_args()

    try:
        print(asyncio.run(run_agent(args.question)))
    except Exception as e:
        print(f"Ошибка: {e}")
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
