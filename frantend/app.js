/* Workelate — minimal chat frontend for the FastAPI agent. */

const $ = (id) => document.getElementById(id);

const chat = $("chat");
const empty = $("empty");
const form = $("form");
const input = $("input");
const sendBtn = $("send");
const newChatBtn = $("newChat");
const themeBtn = $("theme");
const messages = [];
const conversation = [];
const SESSION_HISTORY_LIMIT = 1000;
const DEFAULT_MAX_STEPS = 8;

let busy = false;

/* ------------------------------- theme ----------------------------------- */

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("workelate.theme", theme);
  } catch (_) {
    /* storage may be unavailable */
  }
}

themeBtn.addEventListener("click", () => {
  applyTheme(currentTheme() === "dark" ? "light" : "dark");
  input.focus();
});

/* ------------------------------- API base -------------------------------- */

const DEFAULT_API_BASE = "https://workelate-cli-1.onrender.com";

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
  return DEFAULT_API_BASE;
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

function remember(role, content) {
  conversation.push({ role, content: String(content || "") });
  if (conversation.length > SESSION_HISTORY_LIMIT) {
    conversation.splice(0, conversation.length - SESSION_HISTORY_LIMIT);
  }
}

function errorText(err) {
  return err && err.name === "AbortError"
    ? "The request timed out before the agent finished."
    : String((err && err.message) || err);
}

function autosize() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 168)}px`;
}

function updateSend() {
  sendBtn.disabled = busy || !input.value.trim();
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

  details.append(
    el(
      "summary",
      "",
      `Execution log \u00b7 ${steps.length} step${steps.length === 1 ? "" : "s"}`
    )
  );

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

    box.append(el("div", "step-obs-label", "output"));
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

  const outcome = String(data.outcome || "(no outcome returned)");
  if (!data.ok) meta.append(badge("stopped"));
  meta.append(copyButton(outcome));
  bubble.append(meta);

  bubble.append(el("div", "conclusion", outcome));

  if (
    data.stopped_because &&
    !["done", "answered_directly", "clarification_required"].includes(data.stopped_because)
  ) {
    bubble.append(el("div", "stop-reason", `stopped: ${data.stopped_because}`));
  }

  if (Array.isArray(data.steps) && data.steps.length) {
    // Show the executed commands and their terminal output by default so the
    // execution log is visible at the bottom of the message widget.
    bubble.append(buildSteps(data.steps, true));
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
  bubble.append(el("div", "err-title", "Could not reach the agent"));
  bubble.append(el("div", "", errorText(err)));

  const help = el("div", "err-help");
  help.append(document.createTextNode("Start the backend with "));
  help.append(el("code", "", "cd backend && ./run.sh"));
  help.append(document.createTextNode(` (expected at ${apiLabel()}).`));
  bubble.append(help);

  if (goal) bubble.append(el("div", "err-help", `Goal was: ${goal}`));

  const retry = el("button", "copy", "Retry");
  retry.type = "button";
  retry.addEventListener("click", () => {
    const index = messages.indexOf(wrap);
    if (index >= 0) messages.splice(index, 1);
    wrap.remove();
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

  try {
    const data = await fetchJSON(
      "/task",
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          goal: text,
          max_steps: DEFAULT_MAX_STEPS,
          conversation: conversation.slice(-SESSION_HISTORY_LIMIT),
        }),
      },
      10 * 60 * 1000
    );
    pending.remove();
    addAssistantResult(data);
    remember("user", text);
    remember("assistant", data.outcome || "");
  } catch (err) {
    pending.remove();
    remember("user", text);
    remember("assistant", `Request failed: ${errorText(err)}`);
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

newChatBtn.addEventListener("click", () => {
  if (busy) return;
  showEmpty();
  input.value = "";
  autosize();
  updateSend();
  input.focus();
});

autosize();
updateSend();
input.focus();