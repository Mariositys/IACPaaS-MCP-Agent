const messages = document.getElementById("messages");
const empty = document.getElementById("empty");
const form = document.getElementById("composer");
const input = document.getElementById("question");
const send = document.getElementById("send");
const chatList = document.getElementById("chat-list");
const modelPill = document.getElementById("model");
const newChat = document.getElementById("new-chat");
const callRegistry = new Map();
let currentDialogueId = null;
let sending = false;
let abortController = null;

function h(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setAnswer(node, data) {
  const answer = data.answer || {};
  const fallback = answer.answer || data.final || "Нет ответа.";
  node.classList.add("answer-text", "markdown");
  if (typeof data.answer_html === "string" && data.answer_html) {
    node.innerHTML = data.answer_html;
  } else {
    node.textContent = fallback;
  }
}

function revealWords(node, onDone, onProgress) {
  const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
  const textNodes = [];
  while (walker.nextNode()) {
    if (walker.currentNode.nodeValue && walker.currentNode.nodeValue.trim()) {
      textNodes.push(walker.currentNode);
    }
  }

  const words = [];
  const blocks = [];
  for (const textNode of textNodes) {
    const parent = textNode.parentNode;
    if (!parent) continue;
    const fragment = document.createDocumentFragment();
    const blockChain = [];
    let block = textNode.parentElement?.closest("p,li,h1,h2,h3,h4,h5,h6,pre,blockquote,tr");
    while (block && block !== node) {
      blockChain.push(block);
      block = block.parentElement?.closest("p,li,h1,h2,h3,h4,h5,h6,pre,blockquote,tr");
    }
    for (const item of blockChain) {
      if (!item.classList.contains("typing-block")) {
        item.classList.add("typing-block");
        blocks.push(item);
      }
    }
    for (const part of textNode.nodeValue.split(/(\s+)/)) {
      if (!part) continue;
      if (/^\s+$/.test(part)) {
        fragment.append(document.createTextNode(part));
      } else {
        const word = document.createElement("span");
        word.className = "typing-word";
        word.textContent = part;
        word._typingBlocks = blockChain;
        fragment.append(word);
        words.push(word);
      }
    }
    parent.replaceChild(fragment, textNode);
  }

  if (!words.length) {
    for (const block of blocks) block.classList.remove("typing-block");
    onDone?.();
    return;
  }

  const delay = Math.max(8, Math.min(24, 3500 / words.length));
  let index = 0;
  const timer = setInterval(() => {
    const word = words[index];
    for (const block of word._typingBlocks || []) {
      block.classList.remove("typing-block");
    }
    word.classList.add("typing-visible");
    index += 1;
    if (index % 10 === 0) onProgress?.();
    if (index >= words.length) {
      clearInterval(timer);
      onProgress?.();
      onDone?.();
    }
  }, delay);
}

function syncComposerPadding() {
  messages.style.paddingBottom = `${form.offsetHeight + 24}px`;
}

function scrollDown() {
  syncComposerPadding();
  messages.scrollTop = messages.scrollHeight;
}

function hideEmpty() {
  empty.style.display = "none";
}

function resizeInput() {
  const field = input.closest(".composer-field");
  input.classList.remove("multiline");
  if (field) field.classList.remove("has-multiline");
  input.style.height = "auto";
  const lineHeight = parseFloat(getComputedStyle(input).lineHeight);
  if (input.scrollHeight > lineHeight * 1.5) {
    input.classList.add("multiline");
    if (field) field.classList.add("has-multiline");
  }
  input.style.height = "auto";
  const height = Math.min(input.scrollHeight + 2, 160);
  input.style.height = `${height}px`;
  input.style.overflowY = input.scrollHeight + 2 > 160 ? "auto" : "hidden";
  syncComposerPadding();
}

function updateSendState() {
  if (sending) {
    send.disabled = false;
    send.classList.add("stop");
    send.title = "Остановить";
    send.setAttribute("aria-label", "Остановить");
    return;
  }
  send.classList.remove("stop");
  send.title = "Отправить";
  send.setAttribute("aria-label", "Отправить");
  send.disabled = !input.value.trim();
}

function stopAgent() {
  if (!sending || !abortController) return;
  abortController.abort();
}

function formatDate(timestamp) {
  return new Date(timestamp * 1000).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function callErrorKind(data) {
  for (const call of data.calls || []) {
    const result = call && call.result;
    if (result && result.error && result.kind) return result.kind;
  }
  return null;
}

function badge(data) {
  const hasPreview = operationPreviews(data.calls).length > 0;
  if (data.answer && data.answer.requires_confirmation && hasPreview) {
    return ["Нужно подтверждение", "badge-yellow"];
  }
  if (data.verdict && data.verdict.ok && !(data.answer && data.answer.refused)) {
    return ["Проверено", "badge-green"];
  }
  const errorKind = callErrorKind(data);
  if (errorKind === "platform") return ["Ошибка платформы", "badge-red"];
  if (errorKind === "auth") return ["Ошибка доступа", "badge-red"];
  if (errorKind === "timeout") return ["Таймаут", "badge-red"];
  if (data.answer && data.answer.refused) return ["Нужны данные", "badge-yellow"];
  return ["Не подтверждено", "badge-red"];
}

function userRow(text) {
  const row = h("div", "row user");
  const bubble = h("div", "bubble user-bubble");
  bubble.append(h("div", "answer-text", text));
  row.append(bubble);
  return row;
}

function addUser(text) {
  messages.append(userRow(text));
  hideEmpty();
  scrollDown();
}

function addLoading() {
  const row = h("div", "row agent");
  const bubble = h("div", "bubble");
  const line = h("div", "answer-text");
  const status = h("span", "loading-status", "Собираю данные и проверяю ответ…");
  line.append(h("span", "spinner"), status);
  bubble.append(line);
  row.append(bubble);
  messages.append(row);
  hideEmpty();
  scrollDown();
  return { row, status };
}

function addError(text, kind) {
  const row = h("div", "row agent");
  const bubble = h("div", "bubble error-bubble");
  bubble.append(h("span", "badge badge-red", `Ошибка: ${kind || "platform"}`));
  bubble.append(h("div", "answer-text", text));
  row.append(bubble);
  messages.append(row);
  hideEmpty();
  scrollDown();
}

function addStopped() {
  const row = h("div", "row agent");
  const bubble = h("div", "bubble");
  bubble.append(h("div", "answer-text stopped-text", "Выполнение остановлено."));
  row.append(bubble);
  messages.append(row);
  hideEmpty();
  scrollDown();
}

function plural(count, one, few, many) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

function sourceLabel(pointer) {
  const labels = {
    label: "Название",
    id: "ID",
    type: "Тип",
    concept_path: "Путь",
    metaconcept_path: "Путь метапонятия",
    depth: "Глубина",
    meta: "Метапонятие",
    property: "Свойство",
    value: "Значение",
    values: "Значения",
    view: "Режим",
    path: "Путь ИР",
    total: "Всего совпадений",
    total_matches: "Всего совпадений",
    truncated: "Выдача обрезана",
    nodes: "Вершин",
    edges: "Дуг",
    start: "Начальная вершина",
    title: "Название",
    name: "Имя",
    json_type: "Тип JSON",
    ontology: "Онтология",
    creation: "Дата создания",
    date: "Дата изменения",
    source: "Источник",
  };
  const parts = String(pointer || "").split("/").filter(Boolean);
  const key = parts[parts.length - 1] || "";
  return labels[key] || key.replaceAll("_", " ") || "Значение";
}

function sourceGroup(pointer) {
  const value = String(pointer || "");
  if (value.startsWith("/matches/")) return "Вершина";
  if (value.startsWith("/values/")) return "Значения свойств";
  if (value.startsWith("/resources/")) return "Ресурсы";
  if (/^\/(path|total|truncated)$/.test(value)) return "Результат поиска";
  if (/^\/(nodes|edges|start)$/.test(value)) return "Результат подсчёта";
  return "Данные";
}

function sourceValue(value) {
  if (value === true) return "да";
  if (value === false) return "нет";
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    return String(value).replace(".", ",");
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function callIds(call) {
  return [call?.call_id, call?.result?.call_id]
    .filter((value) => value !== undefined && value !== null)
    .map(String);
}

function registerCalls(calls, messageId) {
  for (const call of calls || []) {
    for (const id of callIds(call)) {
      callRegistry.set(id, call);
      if (messageId !== undefined && messageId !== null) {
        callRegistry.set(`m${messageId}_${id}`, call);
      }
    }
  }
}

function findCall(callId, calls) {
  const wanted = String(callId || "");
  const local = (calls || []).find((item) => callIds(item).includes(wanted));
  if (local) return local;
  if (callRegistry.has(wanted)) return callRegistry.get(wanted);
  const rawId = wanted.replace(/^m\d+_/, "");
  return callRegistry.get(rawId) || null;
}

function terminalValue(call, pointer) {
  const match = String(pointer || "").match(/^\/values\/(\d+)/);
  if (!match) return null;
  return call?.result?.values?.[Number(match[1])] || null;
}

function factLabel(claim, source, call) {
  const terminal = terminalValue(call, source.pointer);
  if (terminal?.property) return terminal.property;
  const label = sourceLabel(source.pointer);
  if (label !== "Название" && label !== "name") return label;
  const statement = String(claim.statement || "").trim();
  return statement.length > 90 ? `${statement.slice(0, 87)}…` : statement || label;
}

function argumentValue(value) {
  if (Array.isArray(value)) return value.join(", ");
  if (value === true) return "да";
  if (value === false) return "нет";
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function toolArguments(call) {
  const args = call?.args || {};
  const items = [];
  const add = (label, value) => {
    const text = argumentValue(value);
    if (text) items.push([label, text]);
  };

  switch (call?.tool) {
    case "list_inforesources":
      add("Запрос", args.query);
      break;
    case "get_inforesource":
      if (args.format === "json") add("Формат", "JSON");
      break;
    case "get_subgraph":
    case "count_graph":
      add("Ветка", args.start);
      break;
    case "search_vertices":
      add("Текст", args.text);
      add("Ветка", args.start);
      break;
    case "locate_vertex":
      add("Текст", args.text);
      add("Ветка", args.start);
      break;
    case "get_terminal_values":
      if (Array.isArray(args.starts) && args.starts.length) {
        add("Ветки", args.starts);
      } else {
        add("Ветка", args.start);
      }
      break;
    case "preview_import":
      add("Папка", args.folder);
      add("Название", args.resource?.title || args.resource?.name);
      if (args.clear_if_exists) add("Очистить при наличии", "да");
      break;
    case "preview_update":
      add("Вершина", args.concept_path);
      add("Новая метка", args.mark);
      add("Комментарий", args.comment);
      break;
    case "preview_delete":
      add("Режим", operationLabel(args.mode));
      add("Вершина", args.concept_path);
      break;
  }

  return items;
}

function sourceHead(tool, path = "", summary = false, args = []) {
  const head = h("div", summary ? "source-head source-head-summary" : "source-head");
  head.append(h("div", "source-tool", `Вызов инструмента: ${tool || "тулз"}`));
  if (path) head.append(h("div", "source-path", path));
  if (args.length) {
    const list = h("div", "source-args");
    for (const [label, value] of args) {
      const row = h("div", "source-arg");
      row.append(h("span", "source-arg-label", `${label}:`));
      row.append(h("span", "source-arg-value", value));
      list.append(row);
    }
    head.append(list);
  }
  return head;
}

function aggregateLabel(claim) {
  const text = String(claim.statement || "").replace(/\s+равно\s+.*$/i, "").trim();
  return text || "Итог";
}

function renderClaims(claims, calls) {
  const byCall = new Map();
  const aggregates = [];
  const allCallIds = new Set();

  for (const claim of claims) {
    const sources = claim.sources || [];
    const sourceCalls = new Set(sources.map(source => source.call_id || "?"));
    for (const callId of sourceCalls) allCallIds.add(callId);

    if (sourceCalls.size > 1) {
      aggregates.push(claim);
      continue;
    }
    for (const source of sources) {
      const callId = source.call_id || "?";
      if (!byCall.has(callId)) byCall.set(callId, []);
      byCall.get(callId).push({ claim, source });
    }
  }

  const callCount = allCallIds.size;
  const factCount = claims.length;
  const details = h("details", "sources");
  details.append(
    h(
      "summary",
      null,
      `Источники: ${callCount} ${plural(callCount, "запрос", "запроса", "запросов")}, ` +
        `${factCount} ${plural(factCount, "факт", "факта", "фактов")}`,
    ),
  );

  if (aggregates.length) {
    const aggregateCalls = new Set(
      aggregates.flatMap(claim => (claim.sources || []).map(source => source.call_id || "?")),
    );
    const card = h("div", "source-card");
    card.append(
      sourceHead(
        "Итоги",
        `${aggregateCalls.size} ${plural(aggregateCalls.size, "запрос", "запроса", "запросов")}`,
        true,
      ),
    );
    const group = h("div", "source-group");
    group.append(h("div", "source-group-title", "Сводные значения"));
    for (const claim of aggregates) {
      const fact = h("div", "source-fact");
      fact.append(h("span", "fact-label", aggregateLabel(claim)));
      fact.append(h("span", "fact-value", sourceValue(claim.value)));
      group.append(fact);
    }
    card.append(group);
    details.append(card);
  }

  for (const [callId, entries] of byCall) {
    const call = findCall(callId, calls);
    const path = call?.args?.path || call?.result?.path || "";
    const card = h("div", "source-card");
    card.append(sourceHead(call ? call.tool : "тулз", path, false, toolArguments(call)));

    const groups = new Map();
    for (const entry of entries) {
      const group = sourceGroup(entry.source.pointer);
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(entry);
    }

    for (const [name, items] of groups) {
      const group = h("div", "source-group");
      group.append(h("div", "source-group-title", name));
      for (const { claim, source } of items) {
        const fact = h("div", "source-fact");
        fact.append(h("span", "fact-label", factLabel(claim, source, call)));
        fact.append(h("span", "fact-value", sourceValue(claim.value)));
        group.append(fact);
      }
      card.append(group);
    }
    details.append(card);
  }

  return details;
}

function operationLabel(operation) {
  return {
    import: "Импорт ИР",
    update: "Изменение вершины",
    deleteIR: "Удаление ИР",
    trashIR: "Перемещение ИР в корзину",
    clearIR: "Очистка ИР",
    deleteSubGraph: "Удаление подграфа",
    clearSubGraph: "Очистка подграфа",
  }[operation] || operation || "Операция";
}

function operationPreviews(calls) {
  return (calls || []).filter((call) => {
    const result = call.result;
    return result && typeof result === "object" && "operation" in result && !("success" in result);
  });
}

function operationValue(value) {
  if (value === undefined || value === null || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

function updateAnswerBadge(card, state) {
  const badge = card.closest(".bubble")?.querySelector(".bubble-head .badge");
  if (!badge) return;
  if (state === "confirmed") {
    badge.className = "badge badge-green";
    badge.textContent = "Выполнено";
  } else if (state === "cancelled") {
    badge.className = "badge badge-yellow";
    badge.textContent = "Отменено";
  } else if (state === "failed") {
    badge.className = "badge badge-red";
    badge.textContent = "Ошибка операции";
  }
}

function renderOperationCard(call, data) {
  const preview = call.result || {};
  const details = preview.details || {};
  const card = h("div", "operation-card");
  card.append(h("div", "operation-title", operationLabel(preview.operation)));
  const rows = [
    ["Путь", preview.path],
    ["Вершина", details.concept_path],
    ["Метапуть", details.metaconcept_path],
    ["Папка", details.folder],
    ["Название", details.title],
    ["Метка", details.mark],
    ["Комментарий", details.comment],
    ["Очистить", details.clear_if_exists],
  ].filter(([, value]) => value !== undefined && value !== null && value !== "");
  for (const [name, value] of rows) {
    const row = h("div", "operation-row");
    row.append(h("span", "operation-key", name));
    row.append(h("span", "operation-value", operationValue(value)));
    card.append(row);
  }

  const status = h("div", "operation-status");
  const actions = h("div", "operation-actions");
  card.append(status, actions);
  const dialogueId = data.dialogue_id || currentDialogueId;

  const finish = (state, text, kind) => {
    call.status = state;
    status.className = `operation-status ${kind || ""}`.trim();
    status.textContent = text;
    actions.replaceChildren();
    updateAnswerBadge(card, state);
  };

  if (call.status === "confirmed") {
    const result = call.operation_result || {};
    finish(
      "confirmed",
      result.error ? `Ошибка: ${result.error}` : "Операция выполнена.",
      result.error ? "error" : "success",
    );
    return card;
  }
  if (call.status === "cancelled") {
    finish("cancelled", "Операция отменена.", "muted");
    return card;
  }
  if (call.status === "failed") {
    const result = call.operation_result || {};
    finish("failed", `Ошибка: ${result.error || "операция не выполнена"}`, "error");
    return card;
  }

  const run = async (action) => {
    if (!dialogueId) {
      status.textContent = "Не удалось определить диалог.";
      return;
    }
    const buttons = actions.querySelectorAll("button");
    buttons.forEach((button) => { button.disabled = true; });
    status.className = "operation-status muted";
    status.textContent = action === "confirm" ? "Выполняю…" : "Отменяю…";
    try {
      const response = await fetch(`/api/operations/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dialogue_id: dialogueId, call_id: call.call_id }),
      });
      const payload = await response.json();
      if (!response.ok || (action === "confirm" && !payload.ok)) {
        const message = payload.error || payload.result?.error || "операция не выполнена";
        finish("failed", `Ошибка: ${message}`, "error");
        return;
      }
      if (action === "cancel") {
        finish("cancelled", "Операция отменена.", "muted");
      } else {
        call.operation_result = payload.result;
        finish("confirmed", "Операция выполнена.", "success");
      }
    } catch (error) {
      status.className = "operation-status error";
      status.textContent = `Ошибка сети: ${error.message}`;
      updateAnswerBadge(card, "failed");
      buttons.forEach((button) => { button.disabled = false; });
    }
  };

  const confirm = h("button", "operation-button confirm", "Подтвердить");
  confirm.type = "button";
  confirm.addEventListener("click", () => run("confirm"));
  const cancel = h("button", "operation-button cancel", "Отменить");
  cancel.type = "button";
  cancel.addEventListener("click", () => run("cancel"));
  actions.append(confirm, cancel);
  return card;
}

function renderAnswer(data, animate = false) {
  const row = h("div", "row agent");
  const bubble = h("div", "bubble");
  const head = h("div", "bubble-head");
  const [label, className] = badge(data);
  head.append(h("span", `badge ${className}`, label));
  bubble.append(head);

  const answer = data.answer || {};
  const answerNode = h("div");
  setAnswer(answerNode, data);
  bubble.append(answerNode);

  const appendSources = () => {
    for (const call of operationPreviews(data.calls)) {
      const card = renderOperationCard(call, data);
      bubble.append(card);
      if (call.status && call.status !== "pending") updateAnswerBadge(card, call.status);
    }
    if (answer.claims && answer.claims.length) {
      bubble.append(renderClaims(answer.claims, data.calls));
    }
    const hasPreview = operationPreviews(data.calls).length > 0;
    const hideReasons = answer.requires_confirmation && hasPreview;
    if (data.verdict && !data.verdict.ok && data.verdict.reasons && data.verdict.reasons.length && !hideReasons) {
      const reasons = h("div", "claim");
      reasons.append(h("div", "claim-statement", "Причины отказа валидатора"));
      for (const reason of data.verdict.reasons) {
        reasons.append(h("div", "claim-meta", reason));
      }
      bubble.append(reasons);
    } else if (data.reasons && data.reasons.length && !hideReasons) {
      const reasons = h("div", "claim");
      reasons.append(h("div", "claim-statement", "Почему ответ не подтверждён"));
      for (const reason of data.reasons) {
        reasons.append(h("div", "claim-meta", reason));
      }
      bubble.append(reasons);
    }
    scrollDown();
  };

  row.append(bubble);
  if (animate) revealWords(answerNode, appendSources, scrollDown);
  else appendSources();
  return row;
}

function renderStoredMessage(message) {
  const fragment = document.createDocumentFragment();
  fragment.append(userRow(message.question));
  fragment.append(renderAnswer(message));
  return fragment;
}

async function loadDialogues() {
  try {
    const response = await fetch("/api/dialogues");
    const data = await response.json();
    const items = Array.isArray(data.items) ? data.items : [];
    chatList.replaceChildren();
    if (!items.length) return;

    for (const item of items) {
      const entry = h("div", "chat-item");
      entry.append(h("div", "chat-title", item.title));
      entry.append(h("div", "chat-meta", formatDate(item.updated_at)));
      if (item.id === currentDialogueId) entry.classList.add("active");
      entry.addEventListener("click", () => openDialogue(item.id));
      chatList.append(entry);
    }
  } catch (error) {
    console.error(error);
    chatList.replaceChildren();
  }
}

async function openDialogue(dialogueId) {
  if (sending) return;
  try {
    const response = await fetch(`/api/dialogues/${encodeURIComponent(dialogueId)}`);
    if (!response.ok) throw new Error("диалог не найден");
    const data = await response.json();
    currentDialogueId = dialogueId;
    localStorage.setItem("iacpaas_dialogue", dialogueId);

    const storedMessages = data.messages || [];
    callRegistry.clear();
    for (const message of storedMessages) {
      registerCalls(message.calls, message.message_id);
    }

    if (storedMessages.length) {
      const fragment = document.createDocumentFragment();
      for (const message of storedMessages) fragment.append(renderStoredMessage(message));
      messages.replaceChildren(fragment);
      empty.style.display = "none";
    } else {
      messages.replaceChildren(empty);
      empty.style.display = "";
    }
    scrollDown();
    loadDialogues();
  } catch (error) {
    console.error(error);
    localStorage.removeItem("iacpaas_dialogue");
    currentDialogueId = null;
    messages.replaceChildren(empty);
    empty.style.display = "";
    loadDialogues();
  }
}

function openNewChat() {
  currentDialogueId = null;
  callRegistry.clear();
  localStorage.removeItem("iacpaas_dialogue");
  messages.replaceChildren(empty);
  empty.style.display = "";
  input.value = "";
  resizeInput();
  updateSendState();
  input.focus();
  loadDialogues();
}

async function ask(question) {
  addUser(question);
  input.value = "";
  resizeInput();
  sending = true;
  abortController = new AbortController();
  updateSendState();
  newChat.disabled = true;
  const loading = addLoading();

  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, dialogue_id: currentDialogueId }),
      signal: abortController.signal,
    });
    if (!response.ok || !response.body) {
      const raw = await response.text();
      loading.row.remove();
      addError(`HTTP ${response.status}: ${raw.slice(0, 500) || "пустой ответ"}`, "platform");
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalData = null;
    let streamError = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let match = buffer.match(/\r?\n\r?\n/);
      while (match) {
        const chunk = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        const payload = chunk
          .split(/\r?\n/)
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (payload) {
          let event;
          try {
            event = JSON.parse(payload);
          } catch {
            event = null;
          }
          if (event && event.type === "status") {
            loading.status.textContent = event.message;
          } else if (event && event.type === "final") {
            finalData = event.result;
          } else if (event && event.type === "error") {
            streamError = event;
          }
        }
        match = buffer.match(/\r?\n\r?\n/);
      }
    }

    loading.row.remove();
    if (streamError) {
      addError(streamError.error || "Ошибка сервера", streamError.kind);
    } else if (!finalData) {
      addError("Сервер не вернул итоговый ответ", "platform");
    } else {
      currentDialogueId = finalData.dialogue_id;
      localStorage.setItem("iacpaas_dialogue", currentDialogueId);
      registerCalls(finalData.calls);
      messages.append(renderAnswer(finalData, true));
      loadDialogues();
      scrollDown();
    }
  } catch (error) {
    loading.row.remove();
    if (error.name === "AbortError") {
      addStopped();
    } else {
      addError(`Сеть: ${error.message}`, "platform");
    }
  } finally {
    abortController = null;
    sending = false;
    newChat.disabled = false;
    updateSendState();
    input.focus();
  }
}

async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    const data = await response.json();
    modelPill.textContent = data.model;
  } catch {
    modelPill.textContent = "?";
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (sending) {
    stopAgent();
    return;
  }
  const question = input.value.trim();
  if (!question) return;
  ask(question);
});

input.addEventListener("input", () => {
  resizeInput();
  updateSendState();
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

newChat.addEventListener("click", openNewChat);

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    input.value = chip.dataset.question;
    resizeInput();
    updateSendState();
    input.focus();
  });
});

loadHealth();
resizeInput();
updateSendState();
window.addEventListener("resize", resizeInput);
const savedDialogue = localStorage.getItem("iacpaas_dialogue");
if (savedDialogue) {
  openDialogue(savedDialogue);
} else {
  loadDialogues();
  input.focus();
}
