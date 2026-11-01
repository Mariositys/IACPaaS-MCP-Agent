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
Write-тулзы без `confirm=true` возвращают план операции и ничего не меняют.

## Каталог ИР

API IACPaaS не умеет перечислять ресурсы, поэтому `list_inforesources` читает локальный
`.iacpaas/catalog.json`. Скопируйте пример и замените запись на реальный ИР:

```powershell
Copy-Item .iacpaas/catalog.example.json .iacpaas/catalog.json
```

## Агент

Нужен ключ модели: `OPENAI_API_KEY` или `IACPAAS_AGENT_API_KEY`.

```powershell
uv run iacpaas-agent "Сколько вершин в ИР «Пример»?"
```

Модель по умолчанию — `gpt-4o-mini`; изменить можно через `IACPAAS_AGENT_MODEL`,
свой OpenAI-совместимый endpoint — через `IACPAAS_AGENT_BASE_URL`.
Ответ агента проходит детерминированную проверку: утверждение без источника
из результатов тулзов не выдаётся пользователю.

Трасса и replay:

```powershell
uv run iacpaas-agent "Сколько вершин в ИР «Пример»?" --trace traces/run.jsonl
uv run iacpaas-agent --replay traces/run.jsonl
```

Replay пересчитывает проверку по записанным вызовам и ответу, без LLM и сети.

## Структура

- `src/iacpaas_mcp` — MCP-сервер;
- `src/iacpaas_agent` — агент (пока заглушка).
