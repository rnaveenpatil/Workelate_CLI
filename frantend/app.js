/* Workelate_CLI — tiny chat frontend for the FastAPI agent. */

const $ = (id) => document.getElementById(id);

const chat = $("chat");
const empty = $("empty");
const emptyTip = $("emptyTip");
const messages = [];
const CONVERSATION_KEY = "workelate.conversation.v1";
const conversation = loadConversation();

function loadConversation() {
  try {
    const saved = JSON.parse(localStorage.getItem(CONVERSATION_KEY) || "[]");
    return Array.isArray(saved)
      ? saved.filter((m) => m && ["user", "assistant"].includes(m.role) &&
          typeof m.content === "string").slice(-16)
      : [];
  } catch (_) { return []; }
}

function saveConversation() {
  try { localStorage.setItem(CONVERSATION_KEY, JSON.stringify(conversation.slice(-16))); }
  catch (_) { /* In-memory context remains available if storage is unavailable. */ }
}

function restoreConversation() {
  if (!conversation.length) return;
  hideEmpty();
  for (const message of conversation) {
    if (message.role === "user") addUserMessage(message.content);
    else {
      const wrap = el("div", "msg assistant");
      wrap.append(el("div", "msg-role", "agent"));
      wrap.append(el("div", "bubble", message.content));
      chat.append(wrap);
      messages.push(wrap);
    }
  }
  scrollToBottom();
}
const form = $("form");
const input = $("input");
const sendBtn = $("send");
const newChatBtn = $("newChat");
const statusEl = $("status");
const statusText = $("statusText");
const maxStepsEl = $("maxSteps");
const apiBtn = $("apiBtn");

let busy = false;

/* ------------------------------- API base -------------------------------- */

const API_BASE = (() => {
  const query = new URLSearchParams(location.search).get("api");
  if (query) return normalizeBase(query);

  try {
    const saved = localStorage.getItem("workelate.api");
    if (saved) return normalizeBase(saved);
  } catch (_) {
    /* localStorage can be unavailable on file:// in some browsers */
  }

  if (/^https?:$/.test(location.protocol)) {
    if (location.port === "8000" || location.pathname.startsWith("/ui")) return "";
  }
  return "http://localhost:8000";
})();

function normalizeBase(url) {
  return String(url).trim().replace(/\/+$/, "");
}

function apiLabel() {
  return API_BASE || location.origin;
}

/* -------------------------------- helpers -------------------------------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function scrollToBottom() {
  requestAnimationFrame(() => {
    chat.scrollTop = chat.scrollHeight;
  });
}

function hideEmpty() {
  empty.hidden = true;
}

function showEmpty() {
  empty.hidden = false;
  for (const node of chat.querySelectorAll(".msg")) node.remove();
  messages.length = 0;
  conversation.length = 0;
  chat.scrollTop = 0;
}

function autosize() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 168)}px`;
}

function updateSend() {
  sendBtn.disabled = busy || !input.value.trim();
}

function setStatus(state, text) {
  statusEl.classList.remove("online", "offline");
  if (state) statusEl.classList.add(state);
  statusText.textContent = text;
}

async function fetchJSON(path, options = {}, timeoutMs = 0) {
  const controller = new AbortController();
  const timer = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const res = await fetch(API_BASE + path, { ...options, signal: controller.signal });
    const raw = await res.text();
    let data = null;
    if (raw) {
      try {
        data = JSON.parse(raw);
      } catch (_) {
        data = { detail: raw };
      }
    }
    if (!res.ok) {
      let detail = data && data.detail;
      if (Array.isArray(detail)) {
        detail = detail.map((item) => item.msg || JSON.stringify(item)).join("; ");
      }
      throw new Error(detail || `HTTP ${res.status}`);
    }
    return data;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/* -------------------------------- health --------------------------------- */

async function checkHealth() {
  setStatus("", "checking…");
  try {
    const health = await fetchJSON("/health", {}, 6000);
    const terminal = health.terminal || {};
    const llm = health.llm || {};
    const mode = terminal.mode || "local";
    const terminalLabel = mode === "modal" ? "Modal" : "Local";
    const bits = [terminalLabel];

    if (mode !== "local") {
      bits.push(terminal.configured ? "sandbox configured" : "sandbox not configured");
    }
    bits.push(llm.configured ? `LLM ${llm.model || "on"}` : "LLM off · agent disabled");

    setStatus("online", bits.join(" · "));
    statusEl.title = `Backend: ${apiLabel()}`;

    emptyTip.textContent = llm.configured
      ? `Connected to ${apiLabel()}. Ask in plain English or type a command.`
      : `Connected to ${apiLabel()} · configure an LLM key to enable conversation and workspace actions. No action will run without it.`;
  } catch (err) {
    setStatus("offline", "backend offline");
    statusEl.title = String((err && err.message) || err);
    emptyTip.textContent =
      `Cannot reach ${apiLabel()}. Start it with: cd backend && ./run.sh`;
  }
}

/* ------------------------------- messages -------------------------------- */

function addUserMessage(text) {
  const wrap = el("div", "msg user");
  wrap.append(el("div", "msg-role", "you"));
  wrap.append(el("div", "bubble", text));
  chat.append(wrap);
  messages.push(wrap);
  scrollToBottom();
}

function addPending() {
  const wrap = el("div", "msg assistant pending");
  wrap.append(el("div", "msg-role", "agent"));

  const bubble = el("div", "bubble");
  const dots = el("span", "dots");
  dots.append(el("i"), el("i"), el("i"));
  bubble.append(dots);
  wrap.append(bubble);

  chat.append(wrap);
  messages.push(wrap);
  scrollToBottom();

  return {
    remove() {
      const index = messages.indexOf(wrap);
      if (index >= 0) messages.splice(index, 1);
      wrap.remove();
    },
  };
}

function badge(text, tone = "") {
  return el("span", `badge ${tone}`.trim(), text);
}

function inputSummary(input) {
  if (!input || !Object.keys(input).length) return "";
  if (input.command) return `$ ${input.command}`;
  if (input.url) return `${input.method || "GET"} ${input.url}`;
  if (input.path) return input.path;
  if (input.prefix) return `prefix: ${input.prefix}`;
  return "";
}

function inputBody(input) {
  if (!input || !Object.keys(input).length) return "";
  if (input.command) return `$ ${input.command}`;
  if (input.url) return `${input.method || "GET"} ${input.url}`;
  if (input.path) return input.path;
  if (input.prefix) return `prefix: ${input.prefix}`;
  return JSON.stringify(input, null, 2);
}

function buildSteps(steps, open) {
  const details = el("details", "steps");
  if (open) details.open = true;

  const summary = el(
    "summary",
    "",
    `Execution log · ${steps.length} step${steps.length === 1 ? "" : "s"}`
  );
  details.append(summary);

  for (const step of steps) {
    const box = el("div", "step");
    const head = el("div", "step-head");
    head.append(el("span", "step-index", `#${step.index}`));
    head.append(el("span", "step-tool", step.tool || "unknown"));

    const summaryText = inputSummary(step.input);
    if (summaryText) head.append(el("span", "step-summary", summaryText));
    box.append(head);

    const body = inputBody(step.input);
    if (body) box.append(el("pre", "step-input", body));

    box.append(el("div", "step-obs-label", "observation"));
    box.append(el("pre", "step-obs", step.observation || "(no output)"));
    details.append(box);
  }
  return details;
}

function copyButton(text) {
  const button = el("button", "copy", "Copy");
  button.type = "button";
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
    } catch (_) {
      button.textContent = "Copy failed";
    }
    setTimeout(() => {
      button.textContent = "Copy";
    }, 1300);
  });
  return button;
}

function addAssistantResult(data) {
  const wrap = el("div", "msg assistant");
  wrap.append(el("div", "msg-role", "agent"));

  const bubble = el("div", "bubble");
  const meta = el("div", "meta");
  meta.append(badge(data.ok ? "completed" : "stopped", data.ok ? "ok" : "warn"));
  meta.append(badge(data.mode === "llm" ? "LLM" : "fallback"));

  const outcome = String(data.outcome || "(no outcome returned)");
  meta.append(copyButton(outcome));
  bubble.append(meta);

  bubble.append(el("div", "conclusion", outcome));

  if (data.stopped_because && !["done", "answered_directly", "clarification_required"].includes(data.stopped_because)) {
    bubble.append(el("div", "stop-reason", `stopped: ${data.stopped_because}`));
  }

  if (Array.isArray(data.steps) && data.steps.length) {
    bubble.append(buildSteps(data.steps, !data.ok));
  }

  wrap.append(bubble);
  chat.append(wrap);
  messages.push(wrap);
  scrollToBottom();
}

function addError(err, goal) {
  const wrap = el("div", "msg assistant");
  wrap.append(el("div", "msg-role", "agent"));

  const bubble = el("div", "bubble error-box");
  bubble.append(el("div", "err-title", "Could not reach the backend agent"));

  const message =
    err && err.name === "AbortError"
      ? "The request timed out before the agent finished."
      : String((err && err.message) || err);
  bubble.append(el("div", "", message));

  const help = el("div", "err-help");
  help.append(document.createTextNode("Check that uvicorn is running on "));
  help.append(el("code", "", apiLabel()));
  help.append(document.createTextNode(" and that the Modal sandbox is reachable."));
  bubble.append(help);

  if (goal) {
    bubble.append(el("div", "err-help", `Goal was: ${goal}`));
  }

  const retry = el("button", "copy", "Retry");
  retry.type = "button";
  retry.addEventListener("click", () => {
    wrap.remove();
    const index = messages.indexOf(wrap);
    if (index >= 0) messages.splice(index, 1);
    sendGoal(goal);
  });
  bubble.append(retry);

  wrap.append(bubble);
  chat.append(wrap);
  messages.push(wrap);
  scrollToBottom();
}

/* --------------------------------- send ---------------------------------- */

async function sendGoal(goal) {
  const text = String(goal || "").trim();
  if (!text || busy) return;

  hideEmpty();
  addUserMessage(text);

  input.value = "";
  autosize();
  updateSend();

  busy = true;
  form.classList.add("busy");
  sendBtn.disabled = true;
  newChatBtn.disabled = true;

  const pending = addPending();
  const maxSteps = Math.max(1, Math.min(25, Number.parseInt(maxStepsEl.value, 10) || 8));
  maxStepsEl.value = String(maxSteps);

  try {
    const priorConversation = conversation.slice(-16);
    const data = await fetchJSON(
      "/task",
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ goal: text, max_steps: maxSteps, conversation: priorConversation }),
      },
      10 * 60 * 1000
    );
    pending.remove();
    addAssistantResult(data);
    conversation.push({ role: "user", content: text });
    conversation.push({ role: "assistant", content: String(data.outcome || "") });
    if (conversation.length > 16) conversation.splice(0, conversation.length - 16);\n    saveConversation();
  } catch (err) {
    pending.remove();
    addError(err, text);
  } finally {
    busy = false;
    form.classList.remove("busy");
    newChatBtn.disabled = false;
    updateSend();
    input.focus();
  }
}

/* -------------------------------- wiring --------------------------------- */

form.addEventListener("submit", (event) => {
  event.preventDefault();
  sendGoal(input.value);
});

input.addEventListener("input", () => {
  autosize();
  updateSend();
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendGoal(input.value);
  }
});

maxStepsEl.addEventListener("change", () => {
  const value = Math.max(1, Math.min(25, Number.parseInt(maxStepsEl.value, 10) || 8));
  maxStepsEl.value = String(value);
});

newChatBtn.addEventListener("click", () => {
  if (busy) return;
  showEmpty();
  input.value = "";
  autosize();
  updateSend();
  input.focus();
});

statusEl.addEventListener("click", checkHealth);

apiBtn.textContent = `API: ${apiLabel()}`;
apiBtn.addEventListener("click", () => {
  const next = window.prompt("Backend API base URL", apiLabel());
  if (next === null) return;
  const value = normalizeBase(next);
  try {
    if (value) localStorage.setItem("workelate.api", value);
    else localStorage.removeItem("workelate.api");
  } catch (_) {
    /* ignore storage failures */
  }
  location.reload();
});

for (const chip of document.querySelectorAll(".chip")) {
  chip.addEventListener("click", () => {
    input.value = chip.dataset.prompt || chip.textContent || "";
    autosize();
    updateSend();
    input.focus();
  });
}

autosize();
updateSend();
restoreConversation();
checkHealth();
setInterval(checkHealth, 45000);
input.focus();
