# IACPaaS MCP Agent

Учебный прототип: MCP-сервер для инфоресурсов IACPaaS и агент над ним.

## Запуск

```powershell
uv sync
uv run iacpaas-mcp
```

Сервер работает по stdio (для VS Code) или по HTTP:

```powershell
uv run iacpaas-mcp --transport http
```

В VS Code сервер подхватывается из `.vscode/mcp.json`.

## Авторизация

Базовый URL захардкожен: `https://iacpaas.dvo.ru`.
Учётные данные берутся из переменных окружения:

- `IACPAAS_API_KEY` — ключ администратора;
- или `IACPAAS_USERNAME` и `IACPAAS_PASSWORD`.

Запись по умолчанию выключена; включить: `IACPAAS_ALLOW_WRITE=true`.

## Структура

- `src/iacpaas_mcp` — MCP-сервер;
- `src/iacpaas_agent` — агент (пока заглушка).
