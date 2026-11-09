# Архитектура

Прототип состоит из MCP-сервера-адаптера к IACPaaS и агента, который отвечает
только по результатам вызовов этого сервера.

## Режимы

```mermaid
flowchart LR
  U[Пользователь] --> V[VS Code Agent]
  U --> S[Standalone CLI]
  U --> W[Веб-чат]
  V -->|MCP, stdio| M[MCP-сервер IACPaaS]
  S --> A[LangGraph-агент]
  W --> A
  A -->|MCP, stdio| M
  M -->|list/path gate| K[(catalog.txt)]
  M -->|JSON/HTTP| P[(IACPaaS JSON API)]
```

- **VS Code** - MCP-сервер подключается из `.vscode/mcp.json`, поведение задает
  `.github/agents/iacpaas.agent.md`. Доступны read-only тулзы, `validate_answer` и
  write-тулзы; сервер хранит evidence чтений и операций в рамках MCP-процесса.
  Write выполняется только при `IACPAAS_ALLOW_WRITE=true` и `confirm=true`.
  Агент обязан вызвать проверку перед финальным ответом, но вызов инструктивный.
- **Standalone CLI** - `iacpaas-agent`: LangGraph-цикл, структурированный ответ,
  детерминированная проверка.
- **Веб-чат** - `iacpaas-web`: FastAPI и одна HTML-страница, тот же `run_agent`
  с read- и write-тулзами. Мутации разрешены только при `IACPAAS_ALLOW_WRITE=true`
  и после подтверждения плана. Все чаты и ходы хранятся в SQLite; evidence прошлых
  вызовов добавляется в пул с ID `m{message_id}_...`; трасса tool calls сохраняется
  в `messages.calls` и доступна в «Технических деталях» для ответов с claims.
  `AgentRuntime` создает один MCP-клиент в `lifespan` и кэширует наборы тулзов;
  CLI по-прежнему запускает MCP на один вызов.

## Цикл агента

```mermaid
flowchart LR
  A[agent: выбрать тулз] --> T[tools: вызов MCP]
  T -->|нужны данные| A
  T -->|данных достаточно| G[answer: AgentAnswer]
  G --> V[validate]
  V -->|ok| R[ответ пользователю]
  V -->|repair, не больше 2 раз| G
  V -->|попытки исчерпаны| F[fail-safe]
```

Состояние графа: вопрос, история из SQLite, сообщения, `evidence` (`call_id -> результат`;
только ходы, поместившиеся в бюджет), журнал вызовов, ответ, вердикт, число попыток и шагов.
Лимиты: 128 000 токенов контекста (8 000 резерв на ответ), 8 шагов сбора данных,
3 попытки ответа (первичная и две починки).

## Модули

| Модуль | Назначение |
|---|---|
| `src/iacpaas_mcp/server.py` | MCP-тулзы, проверки ошибок, подтверждение записи |
| `src/iacpaas_mcp/catalog.py` | локальный `catalog.txt`: поиск, проверка и обновление |
| `src/iacpaas_mcp/client.py` | HTTP-адаптер к IACPaaS, авторизация, `ErrorResult` |
| `src/iacpaas_mcp/models.py` | Pydantic-модели входов/выходов тулзов |
| `catalog.txt` | список доступных ресурсов: один `path` на строку |
| `src/iacpaas_mcp/config.py` | константы, базовый URL, секреты из `.env`/окружения |
| `src/iacpaas_agent/agent.py` | LangGraph-цикл, обертки MCP-тулзов, модель |
| `src/iacpaas_agent/schemas.py` | `AgentAnswer`, `Claim`, `Source` |
| `src/iacpaas_agent/validator.py` | детерминированная проверка ответа |
| `src/iacpaas_agent/prompts.py` | промпты и `PROMPT_VERSION` |
| `src/iacpaas_web/app.py` | FastAPI: чат, сессии, health |
| `src/iacpaas_web/store.py` | SQLite: чаты и все ходы без удаления |
| `src/iacpaas_web/index.html` | разметка чата |
| `src/iacpaas_web/static/style.css` | темная тема |
| `src/iacpaas_web/static/app.js` | логика чата и источники |

## Гарантии и границы

- Каждое утверждение в `AgentAnswer.claims` имеет источник: `call_id` и JSON Pointer.
- Валидатор проверяет существование источника, указателя, значения, формулу и
  отсутствие ответа без вызовов тулзов. Для текстового факта нужен точный `value`
  или дословная `quote`; без них claim отклоняется.
- Битый JSON или несовпадение схемы `AgentAnswer` не роняют запрос: они дают
  причину для repair; после трех попыток выдается fail-safe.
- `temperature=0` снижает разброс, но гарантию дает только валидатор.
- Prompt caching: системный промпт и история/evidence образуют стабильный префикс,
  новый вопрос идет в конце. Claude использует `cache_control`, OpenAI/DeepSeek/
  Gemini/Grok кэшируют автоматически.
- «Без внутренних знаний» означает: нет подтвержденного источника - нет утверждения
  в `claims`. Свободный текст `answer` валидатором не проверяется.
- В режиме VS Code валидатор вызывается отдельным тулзом `validate_answer`, но
  custom agent не принуждает модель к вызову. Гарантия появляется только в
  режимах, где completion tool нельзя обойти.
- Веб-чат без авторизации пользователей. Write-тулзы доступны, но мутации
  разрешены только при `IACPAAS_ALLOW_WRITE=true` и `confirm=true` после
  подтверждения плана. Все чаты и ходы хранятся в `data/chat.db` и не удаляются;
  в модель уходят только ходы в пределах 128k, отказы и fail-safe в контекст
  не включаются; исключение - refused-ход с write-вызовом, нужный для подтверждения.
- Сжатие ответов и автотесты отложены до v2.
