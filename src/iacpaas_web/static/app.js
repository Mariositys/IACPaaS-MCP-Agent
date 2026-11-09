const messages = document.getElementById("messages");
const empty = document.getElementById("empty");
const form = document.getElementById("composer");
const input = document.getElementById("question");
const send = document.getElementById("send");
const chatList = document.getElementById("chat-list");
const modelPill = document.getElementById("model");
const promptPill = document.getElementById("prompt");
const newChat = document.getElementById("new-chat");
const callRegistry = new Map();
let currentDialogueId = null;

function h(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function scrollDown() {
  messages.scrollTop = messages.scrollHeight;
}

function hideEmpty() {
  empty.style.display = "none";
}

function resizeInput() {
  input.classList.remove("multiline");
  input.style.height = "auto";
  const lineHeight = parseFloat(getComputedStyle(input).lineHeight);
  if (input.scrollHeight > lineHeight * 1.5) {
    input.classList.add("multiline");
  }
  input.style.height = "auto";
  const height = Math.min(input.scrollHeight + 2, 160);
  input.style.height = `${height}px`;
  input.style.overflowY = input.scrollHeight + 2 > 160 ? "auto" : "hidden";
}

function formatDate(timestamp) {
  return new Date(timestamp * 1000).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function badge(data) {
  if (data.answer && data.answer.refused) return ["Нужны данные", "badge-gray"];
  if (data.verdict && data.verdict.ok && data.attempts > 1) return ["Исправлено", "badge-yellow"];
  if (data.verdict && data.verdict.ok) return ["Проверено", "badge-green"];
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
  line.append(h("span", "spinner"));
  line.append(document.createTextNode("Собираю данные и проверяю ответ…"));
  bubble.append(line);
  row.append(bubble);
  messages.append(row);
  hideEmpty();
  scrollDown();
  return row;
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
    return Number.isInteger(value)
      ? String(value)
      : value.toLocaleString("ru-RU", { maximumFractionDigits: 2 });
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function registerCalls(calls, messageId) {
  for (const call of calls || []) {
    if (!call?.call_id) continue;
    callRegistry.set(call.call_id, call);
    if (messageId !== undefined && messageId !== null) {
      callRegistry.set(`m${messageId}_${call.call_id}`, call);
    }
  }
}

function findCall(callId, calls) {
  const local = (calls || []).find(item => item.call_id === callId);
  if (local) return local;
  if (callRegistry.has(callId)) return callRegistry.get(callId);
  const rawId = String(callId || "").replace(/^m\d+_/, "");
  return callRegistry.get(rawId) || null;
}

function sourceHead(tool, path = "", summary = false) {
  const head = h("div", summary ? "source-head source-head-summary" : "source-head");
  head.append(h("div", "source-tool", tool || "тулз"));
  if (path) head.append(h("div", "source-path", path));
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
    card.append(sourceHead(call ? call.tool : "тулз", path));

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
        fact.append(h("span", "fact-label", sourceLabel(source.pointer)));
        fact.append(h("span", "fact-value", sourceValue(claim.value)));
        group.append(fact);
      }
      card.append(group);
    }
    details.append(card);
  }

  const technical = h("details", "technical");
  technical.append(h("summary", null, "Технические детали"));
  for (const claim of claims) {
    for (const source of claim.sources || []) {
      const call = findCall(source.call_id, calls);
      const tool = call ? call.tool : "?";
      const pointer = source.pointer || "''";
      technical.append(h("div", "technical-line", `${source.call_id} · ${tool} · ${pointer}`));
    }
  }
  details.append(technical);
  return details;
}

function renderAnswer(data) {
  const row = h("div", "row agent");
  const bubble = h("div", "bubble");
  const head = h("div", "bubble-head");
  const [label, className] = badge(data);
  head.append(h("span", `badge ${className}`, label));
  bubble.append(head);

  const answer = data.answer || {};
  bubble.append(h("div", "answer-text", answer.answer || data.final || "Нет ответа."));

  if (data.attempts !== undefined) {
    bubble.append(h("div", "meta", `попыток: ${data.attempts} · шагов: ${data.steps ?? "?"}`));
  }

  if (answer.claims && answer.claims.length) {
    bubble.append(renderClaims(answer.claims, data.calls));
  }
  if (data.verdict && !data.verdict.ok && data.verdict.reasons && data.verdict.reasons.length) {
    const reasons = h("div", "claim");
    reasons.append(h("div", "claim-statement", "Причины отказа валидатора"));
    for (const reason of data.verdict.reasons) {
      reasons.append(h("div", "claim-meta", reason));
    }
    bubble.append(reasons);
  } else if (data.reasons && data.reasons.length) {
    const reasons = h("div", "claim");
    reasons.append(h("div", "claim-statement", "Почему ответ не подтверждён"));
    for (const reason of data.reasons) {
      reasons.append(h("div", "claim-meta", reason));
    }
    bubble.append(reasons);
  }

  row.append(bubble);
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
  if (send.disabled) return;
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
  input.focus();
  loadDialogues();
}

async function ask(question) {
  addUser(question);
  input.value = "";
  resizeInput();
  send.disabled = true;
  newChat.disabled = true;
  const loading = addLoading();

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, dialogue_id: currentDialogueId }),
    });
    const raw = await response.text();
    let data;
    try {
      data = raw ? JSON.parse(raw) : {};
    } catch {
      loading.remove();
      addError(`HTTP ${response.status}: ${raw.slice(0, 500) || "пустой ответ"}`, "platform");
      return;
    }
    loading.remove();
    if (!response.ok || data.error) {
      addError(data.error || `HTTP ${response.status}`, data.kind);
    } else {
      currentDialogueId = data.dialogue_id;
      localStorage.setItem("iacpaas_dialogue", currentDialogueId);
      registerCalls(data.calls);
      messages.append(renderAnswer(data));
      loadDialogues();
      scrollDown();
    }
  } catch (error) {
    loading.remove();
    addError(`Сеть: ${error.message}`, "platform");
  } finally {
    send.disabled = false;
    newChat.disabled = false;
    input.focus();
  }
}

async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    const data = await response.json();
    modelPill.textContent = `модель: ${data.model}`;
    promptPill.textContent = `контракт: v${data.prompt_version}`;
  } catch {
    modelPill.textContent = "модель: ?";
    promptPill.textContent = "контракт: ?";
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const question = input.value.trim();
  if (!question || send.disabled) return;
  ask(question);
});

input.addEventListener("input", () => {
  resizeInput();
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
    input.focus();
  });
});

loadHealth();
resizeInput();
const savedDialogue = localStorage.getItem("iacpaas_dialogue");
if (savedDialogue) {
  openDialogue(savedDialogue);
} else {
  loadDialogues();
  input.focus();
}
