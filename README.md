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

## MCP Inspector

Браузерный интерфейс для тулзов, ключи IACPaaS не нужны для запуска:

```powershell
npx -y @modelcontextprotocol/inspector uv run iacpaas-mcp
```

В Inspector видно 9 тулзов и их схемы. Без ключей IACPaaS чтения вернут `kind="auth"`.

## Каталог ресурсов

`catalog.json` — локальный список доступных ИР. Формат:

```json
[
  {
    "resource_id": "",
    "title": "Пример",
    "path": "папка/Пример"
  }
]
```

`list_inforesources` ищет по `title` и `path`. Чтение разрешено только для пути,
который есть в каталоге; импорт и удаление обновляют каталог автоматически.
Путь можно переопределить через `IACPAAS_CATALOG`. В репозитории лежит пример
`папка/Пример` — замените его на реальный ресурс.

Для тестов есть `fixtures/mock-agent-ir.json`: 17 вершин, три раздела,
наблюдения, оборудование и текст методики. В корне JSON обязателен
`"json_type": "universal"`. Импортируйте его в папку `Загрузки`; после успешного
импорта в каталоге появится `Загрузки/Мок-ИР`. Перед живым импортом замените
`ontology` и `meta` на существующие в вашем фонде.

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

Откройте `http://127.0.0.1:8000`. Слева - список чатов, справа - ответы, бейдж проверки
и источники claims.

Все чаты и ходы хранятся в `data/chat.db` (SQLite) и не удаляются. Новый чат создается
при первом сообщении. В модель уходят только ходы, помещающиеся в жесткий лимит
128 000 токенов; остальные остаются в истории и видны в интерфейсе. Отказы и fail-safe
сохраняются, но в контекст модели не включаются.

Чат работает без авторизации пользователей и слушает `127.0.0.1`. MCP-сервер
запускается один раз в `lifespan` веб-приложения, а не на каждый запрос; тулзы
кэшируются в `AgentRuntime`. При остановке `uvicorn` дочерний MCP-процесс
завершается. Для ответов нужны `IACPAAS_AGENT_API_KEY`, `IACPAAS_AGENT_MODEL` и
`IACPAAS_AGENT_BASE_URL`; без ключей IACPaaS чтение вернет `kind="auth"`.
Write-операции доступны только при `IACPAAS_ALLOW_WRITE=true`; агент сначала
показывает план с `confirm=false` и выполняет изменение с `confirm=true` только
после явного подтверждения пользователя. История хранит аргументы write-вызовов,
поэтому подтверждение можно отправить следующим сообщением в том же чате.

## VS Code

Откройте папку `IACPaaS-MCP-Agent`, выполните `uv sync` и выберите агента **IACPaaS**
в списке агентов чата. MCP-сервер подхватывается из `.vscode/mcp.json`,
инструкции лежат в `.github/agents/iacpaas.agent.md`.

Агент обязан перед финальным ответом вызвать `validate_answer` с `AgentAnswer`.
Тулз проверяет claims по журналу evidence текущего MCP-процесса; при `ok=false`
агент должен исправить ответ и повторить. Это инструментальная проверка, но
нативный custom agent не гарантирует вызов: жёсткое принуждение есть только
в режимах с completion tool.

Доступны 5 read-only тулзов (включая `list_inforesources`), `validate_answer` и
3 write-тулза. Сначала агент ищет ресурс через `list_inforesources` по локальному
`catalog.json` и использует точный `path`; если ресурса нет в каталоге, чтение
отклоняется. Запись выполняется только при `IACPAAS_ALLOW_WRITE=true`; перед
`confirm=true` агент обязан показать план и получить явное подтверждение.

Примеры запросов:
- «Найди ресурс «Пример»» - `list_inforesources`.
- «Сколько вершин в ИР «Пример»?» - `count_graph`.
- «Найди вершину «Наблюдение» в ИР «Пример»» - `query_graph`.
- «Покажи подграф ИР «Пример» от вершины «Наблюдение» глубиной 2» - `get_subgraph`.

Диагностика: `uv run iacpaas-mcp --version` проверяет запуск сервера, MCP Inspector
показывает 9 тулзов и их схемы, включая `list_inforesources` и `validate_answer`.
Ошибки чтения приходят как `ErrorResult` с полем `kind` (`auth`, `not_found`,
`platform`, `validation`); при проблемах с агентом проверьте Output канала Copilot
и `.vscode/mcp.json`.

## Структура

- `src/iacpaas_mcp` - MCP-сервер;
- `src/iacpaas_agent` - LangGraph-агент и валидатор;
- `src/iacpaas_web` - браузерный чат на FastAPI и SQLite;
- `catalog.json` - локальный каталог ресурсов для `list_inforesources`;
- `fixtures/mock-agent-ir.json` - тестовый ИР на 17 вершин;
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

