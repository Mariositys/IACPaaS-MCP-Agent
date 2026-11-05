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
Скопируйте шаблон и заполните `.env`:

```powershell
Copy-Item .env.example .env
```

Учетные данные берутся из `.env` или переменных окружения (переменные приоритетнее):

- `IACPAAS_API_KEY` - ключ администратора;
- или `IACPAAS_USERNAME` и `IACPAAS_PASSWORD`.

`.env` не коммитится. 
Запись по умолчанию выключена; включить: `IACPAAS_ALLOW_WRITE=true`.
Write-тулзы без `confirm=true` возвращают план операции и ничего не меняют.

## Каталог ИР

API IACPaaS не умеет перечислять ресурсы, поэтому `list_inforesources` читает локальный
`.iacpaas/catalog.json`. Скопируйте пример и замените запись на реальный ИР:

```powershell
Copy-Item .iacpaas/catalog.example.json .iacpaas/catalog.json
```

## MCP Inspector

Браузерный интерфейс для тулзов, ключи IACPaaS не нужны для запуска:

```powershell
npx -y @modelcontextprotocol/inspector uv run iacpaas-mcp
```

В Inspector видно 8 тулзов и их схемы. `list_inforesources` работает по локальному
каталогу; остальные чтения без ключей вернут `kind="auth"`.

## Агент

Нужны три переменные:

- `IACPAAS_AGENT_API_KEY`;
- `IACPAAS_AGENT_MODEL`;
- `IACPAAS_AGENT_BASE_URL`.

Пример для Polza:

```text
IACPAAS_AGENT_API_KEY=...
IACPAAS_AGENT_MODEL=...
IACPAAS_AGENT_BASE_URL=https://polza.ai/api/v1
```

```powershell
uv run iacpaas-agent "Сколько вершин в ИР «Пример»?"
```

Значений по умолчанию нет: если хотя бы одна переменная не задана, агент завершится
с ошибкой со списком отсутствующих. Ответ агента проходит детерминированную проверку:
claims без источника или без проверяемого `value`/`quote` отклоняются, после двух
починок выдается fail-safe.

Для prompt caching промпт собран стабильным префиксом: системные правила и история идут
первыми, новый вопрос — в конце. OpenAI, DeepSeek, Gemini и Grok кэшируют такой префикс
автоматически, Claude — через `cache_control`. Минимум обычно 1000+ токенов; попадания
видны в `usage` Polza.

## Веб-чат

```powershell
uv run iacpaas-web
```

Откройте `http://127.0.0.1:8000`. Интерфейс показывает каталог ИР, ответ агента,
бейдж проверки и источники claims.

Чат работает в read-only режиме и без авторизации пользователей: сервер слушает
`127.0.0.1`. Для ответов нужны `IACPAAS_AGENT_API_KEY`, `IACPAAS_AGENT_MODEL` и
`IACPAAS_AGENT_BASE_URL`; без ключей IACPaaS чтение вернет `kind="auth"`.
В модель передаются последние 3 хода; их `call_id` переименовываются в `h1_...`, `h2_...`
и остаются доступны валидатору.

## VS Code

Откройте папку `IACPaaS-MCP-Agent`, выполните `uv sync` и выберите агента **IACPaaS**
в списке агентов чата. MCP-сервер подхватывается из `.vscode/mcp.json`,
инструкции лежат в `.github/agents/iacpaas.agent.md`.

В этом режиме работает только инструкция агенту; детерминированный валидатор
ответов доступен в standalone-режиме (`iacpaas-agent`).

Примеры запросов:
- «Какие ИР есть в каталоге?»
- «Сколько вершин в ИР «Пример»?»

## Структура

- `src/iacpaas_mcp` - MCP-сервер;
- `src/iacpaas_agent` - LangGraph-агент и валидатор;
- `src/iacpaas_web` - браузерный чат на FastAPI;
- `docs` - архитектура, сценарии, соответствие API и решения;
- `.vscode/mcp.json`, `.github/agents/iacpaas.agent.md` - режим VS Code.

## Документация

- `docs/architecture.md` - схема режимов и цикла агента;
- `docs/scenarios.md` - ручные сценарии проверки;
- `docs/tool-mapping.md` - MCP-тулз -> операция IACPaaS;
- `docs/requirements.md` - требование -> реализация -> проверка;
- `docs/decisions.md` - принятые решения и ограничения.

## Статус

M0–M7 и веб-чат готовы. Отложено до v2: сжатие ответов, автотесты, LLM-judge.

