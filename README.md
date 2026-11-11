# IACPaaS MCP Agent

Учебный прототип: MCP-сервер для инфоресурсов IACPaaS и агент над ним.

## Запуск

Требуются Python 3.11+ и `uv`.

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

В Inspector видно 10 тулзов и их схемы. Без ключей IACPaaS сетевые чтения
(кроме `list_inforesources`) вернут `kind="auth"`.

## Каталог ресурсов

`catalog.txt` — локальный список доступных ИР: один путь на строку. Пустые
строки и строки, начинающиеся с `#`, игнорируются.

```text
# Ресурсы в «Мой Фонд»
Загрузки/Пример
```

`title` не хранится: он берется из последнего сегмента пути.

`list_inforesources` ищет по `title` и `path`. Чтение разрешено только для пути,
который есть в каталоге; импорт и удаление обновляют каталог автоматически.
Путь можно переопределить через `IACPAAS_CATALOG`.

## Агент

Нужны три обязательные переменные:

- `IACPAAS_AGENT_API_KEY`;
- `IACPAAS_AGENT_MODEL`;
- `IACPAAS_AGENT_BASE_URL`.

Уровень рассуждений задается `IACPAAS_AGENT_REASONING_EFFORT` (по умолчанию `low`).

Пример для Polza:

```text
IACPAAS_AGENT_API_KEY=...
IACPAAS_AGENT_MODEL=...
IACPAAS_AGENT_BASE_URL=https://polza.ai/api/v1
IACPAAS_AGENT_REASONING_EFFORT=low
```

```powershell
uv run iacpaas-agent "Сколько вершин в ИР «База металлопорошковых материалов»?"
```

Статусы выполнения печатаются в stderr, итоговый ответ — в stdout.
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
и источники claims. Трасса tool calls (аргументы и результаты) сохраняется в
`messages.calls` в `data/chat.db`; для ответов с claims она раскрывается в
«Технических деталях». Статусы выполнения приходят потоком через SSE, а итоговый
ответ появляется после валидации. CLI печатает только финальный ответ.

Все чаты и ходы хранятся в `data/chat.db` (SQLite) и не удаляются. Новый чат создается
при первом сообщении. В модель уходят только ходы, помещающиеся в жесткий лимит
1 000 000 токенов; остальные остаются в истории и видны в интерфейсе. Отказы и fail-safe
сохраняются, но в контекст модели не включаются; исключение - refused-ход с write-вызовом,
он нужен для подтверждения.

Чат работает без авторизации пользователей и слушает `127.0.0.1`. MCP-сервер
запускается один раз в `lifespan` веб-приложения, а не на каждый запрос; тулзы
кэшируются в `AgentRuntime`. При остановке `uvicorn` дочерний MCP-процесс
завершается. Для ответов нужны `IACPAAS_AGENT_API_KEY`, `IACPAAS_AGENT_MODEL` и
`IACPAAS_AGENT_BASE_URL`; без ключей IACPaaS сетевые чтения (кроме
`list_inforesources`) вернут `kind="auth"`.
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

Доступны 6 read-only тулзов (включая `list_inforesources` и `find_vertex`),
`validate_answer` и 3 write-тулза. Сначала агент ищет ресурс через `list_inforesources` по локальному
`catalog.txt` и использует точный `path`; если ресурса нет в каталоге, чтение
отклоняется. Запись выполняется только при `IACPAAS_ALLOW_WRITE=true`; перед
`confirm=true` агент обязан показать план и получить явное подтверждение.

Примеры запросов (подставьте ресурс из `catalog.txt` и существующую вершину):
- «Найди ресурс «<ресурс>»» - `list_inforesources`.
- «Сколько вершин в ИР «<ресурс>»?» - `count_graph`.
- «Найди вершину «<вершина>» в ИР «<ресурс>»» - `find_vertex`.
- «Покажи подграф ИР «<ресурс>» от вершины «<вершина>» глубиной 2» - `get_subgraph`.

`get_subgraph` по умолчанию использует `view="summary"` (без комментариев и лишних полей)
и поддерживает страницы через `offset`/`next_offset`. Для полного описания вершины
передайте `view="full"`. Полный экспорт ИР кэшируется в `data/graph-cache` на
300 секунд; `refresh=true` принудительно перечитывает ресурс.

Диагностика: `uv run iacpaas-mcp --version` проверяет запуск сервера, MCP Inspector
показывает 10 тулзов и их схемы, включая `list_inforesources`, `find_vertex` и `validate_answer`.
Ошибки чтения приходят как `ErrorResult` с полем `kind` (`auth`, `not_found`,
`platform`, `timeout`, `validation`); при проблемах с агентом проверьте Output канала Copilot
и `.vscode/mcp.json`.

## Структура

- `src/iacpaas_mcp` - MCP-сервер;
- `src/iacpaas_agent` - LangGraph-агент и валидатор;
- `src/iacpaas_web` - браузерный чат на FastAPI и SQLite;
- `catalog.txt` - локальный каталог ресурсов для `list_inforesources`;
- `docs` - архитектура, сценарии и соответствие API;
- `.vscode/mcp.json`, `.github/agents/iacpaas.agent.md` - режим VS Code.

## Документация

- `docs/architecture.md` - схема режимов и цикла агента;
- `docs/scenarios.md` - ручные сценарии проверки;
- `docs/tool-mapping.md` - MCP-тулз -> операция IACPaaS.
