"""Заглушка CLI агента: логика появится в следующих вехах."""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="iacpaas-agent", description="Агент над IACPaaS")
    parser.add_argument("question", nargs="?")
    parser.parse_args()
    print("Агент пока не реализован.")


if __name__ == "__main__":
    main()
