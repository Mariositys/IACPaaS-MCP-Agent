const messages = document.getElementById("messages");
const empty = document.getElementById("empty");
const form = document.getElementById("composer");
const input = document.getElementById("question");
const send = document.getElementById("send");
const catalogList = document.getElementById("catalog-list");
const catalogSearch = document.getElementById("catalog-search");
const catalogError = document.getElementById("catalog-error");
const modelPill = document.getElementById("model");
const promptPill = document.getElementById("prompt");
const newChat = document.getElementById("new-chat");
let turns = [];

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

function badge(data) {
  if (data.answer && data.answer.refused) return ["Нужны данные", "badge-gray"];
  if (data.verdict && data.verdict.ok && data.attempts > 1) return ["Исправлено", "badge-yellow"];
  if (data.verdict && data.verdict.ok) return ["Проверено", "badge-green"];
  return ["Не подтверждено", "badge-red"];
}

function addUser(text) {
  const row = h("div", "row user");
  const bubble = h("div", "bubble user-bubble");
  bubble.append(h("div", "answer-text", text));
  row.append(bubble);
  messages.append(row);
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

function renderClaims(claims, calls) {
  const details = h("details", "sources");
  details.append(h("summary", null, `Источники (${claims.length})`));
  for (const claim of claims) {
    const item = h("div", "claim");
    item.append(h("div", "claim-statement", claim.statement));
    const meta = [];
    if (claim.value !== null && claim.value !== undefined) meta.push(`value: ${JSON.stringify(claim.value)}`);
    if (claim.quote) meta.push(`quote: «${claim.quote}»`);
    if (meta.length) item.append(h("div", "claim-meta", meta.join(" · ")));
    for (const source of claim.sources || []) {
      const call = (calls || []).find(item => item.call_id === source.call_id);
      const tool = call ? call.tool : "?";
      const pointer = source.pointer || "''";
      const expr = source.expr ? ` · expr: ${source.expr}` : "";
      item.append(h("div", "source", `${source.call_id} · ${tool} · ${pointer}${expr}`));
    }
    details.append(item);
  }
  return details;
}

function downloadTrace(data) {
  const blob = new Blob([JSON.stringify(data) + "\n"], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `trace-${Date.now()}.jsonl`;
  link.click();
  URL.revokeObjectURL(link.href);
}

function renderTrace(data) {
  const details = h("details", "trace");
  details.append(h("summary", null, `Трасса (${data.calls.length} вызовов)`));
  for (const call of data.calls) {
    const block = h("div", "trace-call");
    block.append(h("div", "trace-title", `${call.tool} · ${call.call_id}`));
    block.append(h("pre", null, JSON.stringify({ args: call.args, result: call.result }, null, 2)));
    details.append(block);
  }
  const download = h("button", "ghost small", "Скачать JSONL");
  download.type = "button";
  download.addEventListener("click", () => downloadTrace(data));
  details.append(download);
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
  if (data.calls && data.calls.length) {
    bubble.append(renderTrace(data));
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

async function ask(question) {
  addUser(question);
  input.value = "";
  input.style.height = "auto";
  send.disabled = true;
  newChat.disabled = true;
  const loading = addLoading();

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history: turns.slice(-3) }),
    });
    const data = await response.json();
    loading.remove();
    if (!response.ok || data.error) {
      addError(data.error || `HTTP ${response.status}`, data.kind);
    } else {
      messages.append(renderAnswer(data));
      turns.push({ question, answer: data.answer, calls: data.calls });
      if (turns.length > 10) turns.shift();
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

async function loadCatalog() {
  catalogError.textContent = "";
  catalogList.replaceChildren();
  try {
    const response = await fetch(`/api/catalog?query=${encodeURIComponent(catalogSearch.value)}`);
    const data = await response.json();
    if (data.error) {
      catalogError.textContent = data.error;
      return;
    }
    if (!data.items.length) {
      catalogError.textContent = "Ничего не найдено";
      return;
    }
    for (const item of data.items) {
      const entry = h("li", "catalog-item");
      entry.append(h("div", "catalog-title", item.title));
      entry.append(h("div", "catalog-path", item.path));
      entry.addEventListener("click", () => {
        input.value = item.path;
        input.focus();
      });
      catalogList.append(entry);
    }
  } catch (error) {
    catalogError.textContent = `Ошибка каталога: ${error.message}`;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const question = input.value.trim();
  if (!question || send.disabled) return;
  ask(question);
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

let catalogTimer;
catalogSearch.addEventListener("input", () => {
  clearTimeout(catalogTimer);
  catalogTimer = setTimeout(loadCatalog, 250);
});

newChat.addEventListener("click", () => {
  messages.replaceChildren(empty);
  empty.style.display = "";
  turns = [];
  input.value = "";
  input.style.height = "auto";
  input.focus();
});

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    input.value = chip.dataset.question;
    input.focus();
  });
});

loadHealth();
loadCatalog();
input.focus();
