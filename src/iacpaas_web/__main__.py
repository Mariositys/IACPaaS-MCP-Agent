"""Запуск браузерного чата: iacpaas-web."""

import uvicorn


def main() -> None:
    uvicorn.run("iacpaas_web.app:app", host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
