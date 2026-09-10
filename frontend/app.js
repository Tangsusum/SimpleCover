// SimpleCover demo frontend — single page, no build, no framework.
// Talks only to the thin local backend at /api/chat.
import { DatadogProvider } from "@datadog/openfeature-browser";
import { OpenFeature } from "@openfeature/web-sdk";

const _local =
  location.hostname === "localhost" || location.hostname === "127.0.0.1";
const API_BASE =
  new URLSearchParams(location.search).get("api") ||
  (_local ? "http://localhost:8000" : "");

function rumOnReady(fn) {
  if (window.DD_RUM && typeof window.DD_RUM.onReady === "function") {
    window.DD_RUM.onReady(fn);
  }
}

function rumIdentify() {
  rumOnReady(() => {
    window.DD_RUM.setUser({ id: customerId });
    window.DD_RUM.setGlobalContextProperty("customer_id", customerId);
    window.DD_RUM.setGlobalContextProperty("chat_session_id", sessionId);
  });
}

// ── Session identity (in-memory only) ─────────────────────────────
const uuid = () =>
  (crypto.randomUUID && crypto.randomUUID()) ||
  "id-" + Math.random().toString(16).slice(2) + Date.now().toString(16);

let customerId = "cust-" + uuid().slice(0, 8);
let sessionId = "sess-" + uuid().slice(0, 12);

const $ = (id) => document.getElementById(id);
const els = {
  messages: $("messages"),
  input: $("input"),
  send: $("send"),
  customerId: $("customer-id"),
  sessionId: $("session-id"),
  newSession: $("new-session"),
  docToggle: $("doc-toggle"),
  docDrawer: $("doc-drawer"),
  docInput: $("doc-input"),
  docSend: $("doc-send"),
  docCancel: $("doc-cancel"),
  cta: $("cta-chat"),
  banner: $("assistant-banner"),
  bannerChat: $("banner-chat"),
  bannerDismiss: $("banner-dismiss"),
};

// ── Init ──────────────────────────────────────────────────────────
els.customerId.value = customerId;
renderSessionId();
rumIdentify();

// Keep customerId in sync if the user edits it (returning-customer demo).
els.customerId.addEventListener("change", () => {
  customerId = els.customerId.value.trim() || customerId;
  els.customerId.value = customerId;
  rumIdentify();
  syncFeatureFlagContext();
});

els.newSession.addEventListener("click", () => {
  sessionId = "sess-" + uuid().slice(0, 12);
  renderSessionId();
  rumIdentify();
  rumOnReady(() => {
    window.DD_RUM.addAction("chat_new_session", {
      customer_id: customerId,
      chat_session_id: sessionId,
    });
  });
  addSystemNote(`Started a new session (${sessionId}) for ${customerId}.`);
});

els.cta.addEventListener("click", () => {
  $("chat").scrollIntoView({ behavior: "smooth" });
  els.input.focus();
});

if (els.bannerChat) {
  els.bannerChat.addEventListener("click", () => {
    $("chat").scrollIntoView({ behavior: "smooth" });
    els.input.focus();
  });
}
if (els.bannerDismiss) {
  els.bannerDismiss.addEventListener("click", () => {
    sessionStorage.setItem("sc-banner-dismissed", "1");
    if (els.banner) els.banner.hidden = true;
  });
}

// Composer: Enter sends, Shift+Enter = newline; textarea auto-grows.
els.input.addEventListener("input", autoGrow);
els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
els.send.addEventListener("click", () => sendMessage());

// Document drawer
els.docToggle.addEventListener("click", () => {
  els.docDrawer.hidden = !els.docDrawer.hidden;
  if (!els.docDrawer.hidden) els.docInput.focus();
});
els.docCancel.addEventListener("click", () => {
  els.docDrawer.hidden = true;
  els.docInput.value = "";
});
els.docSend.addEventListener("click", () => {
  const text = els.docInput.value.trim();
  if (!text) return;
  els.docDrawer.hidden = true;
  els.docInput.value = "";
  sendMessage(text, { asDocument: true });
});

// Greeting
addAgent(
  "Hi! I'm the SimpleCover assistant. Tell me a bit about what you're after — " +
    "your state, whether you want hospital, extras or both, and anything that " +
    "matters to you. You can also paste a policy document for me to review.",
  []
);

initFeatureFlags();

// ── Datadog feature flags ─────────────────────────────────────────
// https://docs.datadoghq.com/feature_flags/client/javascript/?tab=npm
async function initFeatureFlags() {
  const provider = new DatadogProvider({
    applicationId: "982ab0ef-608a-405d-89be-3d3033d68e3f",
    clientToken: "pub6f8a1340522470ddd064a690663e1996",
    site: "datadoghq.com",
    env: "dev",
  });
  try {
    await OpenFeature.setProviderAndWait(provider, flagContext());
    applyAssistantBanner();
  } catch (err) {
    console.warn("Datadog feature flags unavailable; banner stays off.", err);
  }
}

function flagContext() {
  return {
    targetingKey: customerId,
    user_id: customerId,
  };
}

async function syncFeatureFlagContext() {
  try {
    await OpenFeature.setContext(flagContext());
    applyAssistantBanner();
  } catch (err) {
    console.warn("Could not refresh feature-flag context.", err);
  }
}

function applyAssistantBanner() {
  const value = OpenFeature.getClient().getBooleanValue("simplecover-assistant-banner", false);
  if (!els.banner) return;
  const dismissed = sessionStorage.getItem("sc-banner-dismissed") === "1";
  els.banner.hidden = !(value && !dismissed);
}

// ── Core send ─────────────────────────────────────────────────────
async function sendMessage(overrideText, opts = {}) {
  const text = (overrideText ?? els.input.value).trim();
  if (!text) return;
  if (!overrideText) {
    els.input.value = "";
    autoGrow();
  }

  if (opts.asDocument) addDocument(text);
  else addUser(text);

  rumOnReady(() => {
    window.DD_RUM.addAction("chat_message_sent", {
      as_document: !!opts.asDocument,
      message_length: text.length,
    });
  });

  const typing = addTyping();
  setBusy(true);

  try {
    const res = await fetch(`${API_BASE}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        customer_id: customerId,
        session_id: sessionId,
        message: text,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    typing.remove();
    addAgent(data.response || "(no response)", data.trace || []);
  } catch (err) {
    typing.remove();
    rumOnReady(() => {
      window.DD_RUM.addError(err, { api_base: API_BASE, source: "chat_fetch" });
    });
    addAgent(
      `⚠️ Couldn't reach the backend at ${API_BASE}. Is it running?\n\n` +
        `Start it with:  uvicorn server:app --port 8000  (in backend/)\n\n${err}`,
      [{ kind: "error", message: String(err) }]
    );
  } finally {
    setBusy(false);
  }
}

// ── Rendering ─────────────────────────────────────────────────────
function addUser(text) {
  const el = document.createElement("div");
  el.className = "msg user";
  el.textContent = text;
  els.messages.appendChild(el);
  scroll();
}

function addDocument(text) {
  const el = document.createElement("div");
  el.className = "msg doc";
  const tag = document.createElement("span");
  tag.className = "doc-tag";
  tag.textContent = "📄 Uploaded document";
  el.appendChild(tag);
  el.appendChild(document.createTextNode(text));
  els.messages.appendChild(el);
  scroll();
}

function addAgent(text, trace) {
  const el = document.createElement("div");
  el.className = "msg agent";
  el.textContent = text;
  els.messages.appendChild(el);
  if (trace && trace.length) els.messages.appendChild(renderTrace(trace));
  scroll();
}

function addSystemNote(text) {
  const el = document.createElement("div");
  el.className = "msg agent";
  el.style.opacity = ".7";
  el.style.fontStyle = "italic";
  el.textContent = text;
  els.messages.appendChild(el);
  scroll();
}

function addTyping() {
  const el = document.createElement("div");
  el.className = "typing";
  el.innerHTML = "<span></span><span></span><span></span>";
  els.messages.appendChild(el);
  scroll();
  return el;
}

// Summarise + expand the structured trace returned from /api/chat.
function renderTrace(trace) {
  const tools = trace.filter((t) => t.kind === "tool_call" && t.phase === "input");
  const kbs = trace.filter((t) => t.kind === "kb_lookup" && t.phase === "input");
  const guards = trace.filter((t) => t.kind === "guardrail");
  const bits = [];
  if (tools.length) bits.push(`${tools.length} tool call${tools.length > 1 ? "s" : ""}`);
  if (kbs.length) bits.push(`${kbs.length} KB lookup${kbs.length > 1 ? "s" : ""}`);
  if (guards.some((g) => g.blocked)) bits.push("guardrail blocked");
  const summaryText = bits.length ? bits.join(" · ") : "trace";

  const details = document.createElement("details");
  details.className = "trace";
  const summary = document.createElement("summary");
  summary.textContent = `Trace (${summaryText})`;
  details.appendChild(summary);

  const body = document.createElement("div");
  body.className = "trace-body";
  trace.forEach((e) => body.appendChild(traceItem(e)));
  details.appendChild(body);
  return details;
}

function traceItem(e) {
  const box = document.createElement("div");
  box.className = "trace-item";
  const badge = document.createElement("span");
  badge.className = "trace-kind ";

  if (e.kind === "tool_call") {
    badge.className += "k-tool";
    badge.textContent = e.phase === "input" ? "tool →" : "tool ←";
    box.appendChild(badge);
    if (e.phase === "input") {
      box.appendChild(inline(` ${e.tool || "(tool)"}`, "lbl"));
      box.appendChild(pre(e.input));
    } else {
      box.appendChild(pre(e.output));
    }
  } else if (e.kind === "kb_lookup") {
    badge.className += "k-kb";
    badge.textContent = e.phase === "input" ? "KB query" : "KB result";
    box.appendChild(badge);
    if (e.phase === "input") {
      box.appendChild(inline(` ${e.query || ""}`));
    } else {
      const chunks = e.chunks || [];
      box.appendChild(inline(` ${chunks.length} chunk(s) retrieved`));
      chunks.slice(0, 3).forEach((c) => {
        const doc = c.document || "?";
        const score = c.score == null ? "?" : c.score;
        box.appendChild(pre(`[${doc}] score=${score}\n${(c.text || "").slice(0, 240)}`));
      });
    }
  } else if (e.kind === "guardrail") {
    badge.className += e.blocked ? "k-guard blocked" : "k-guard";
    badge.textContent = e.blocked ? "guardrail: BLOCKED" : "guardrail: passed";
    box.appendChild(badge);
    box.appendChild(inline(` action=${e.action || "?"}${e.reason ? " — " + e.reason : ""}`));
  } else if (e.kind === "rationale") {
    badge.className += "k-rationale";
    badge.textContent = "rationale";
    box.appendChild(badge);
    box.appendChild(inline(` ${(e.text || "").slice(0, 220)}`));
  } else if (e.kind === "error") {
    badge.className += "k-error";
    badge.textContent = "error";
    box.appendChild(badge);
    box.appendChild(pre(e.message));
  } else {
    badge.className += "k-rationale";
    badge.textContent = e.kind || "event";
    box.appendChild(badge);
    box.appendChild(pre(e));
  }
  return box;
}

function inline(text, cls) {
  const s = document.createElement("span");
  if (cls) s.className = cls;
  s.textContent = text;
  return s;
}
function pre(obj) {
  const p = document.createElement("pre");
  p.textContent = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
  return p;
}

// ── Helpers ───────────────────────────────────────────────────────
function renderSessionId() {
  els.sessionId.textContent = "session: " + sessionId;
}
function setBusy(b) {
  els.send.disabled = b;
  els.docSend.disabled = b;
}
function autoGrow() {
  els.input.style.height = "auto";
  els.input.style.height = Math.min(els.input.scrollHeight, 140) + "px";
}
function scroll() {
  els.messages.scrollTop = els.messages.scrollHeight;
}
