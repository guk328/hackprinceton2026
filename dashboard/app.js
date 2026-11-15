// Backflow dashboard. One page, no build step, no external files.
//   feed   : GET /events, then "event" messages on /stream
//   agent  : GET /agent/log, then "agent" messages on /stream
//   phone  : GET /operator/inbox (texts + one-time codes); buttons POST /operator/reply
// Everything from the network is written with textContent, never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
const feed = $("feed"), status = $("status");
const rows = new Map();               // event id -> <li>
const stepIds = new Set();
const MAX_ROWS = 300, MAX_STEPS = 400;
const listeners = { select: [], event: [] };   // other scripts (plant.js) hook in here
let selected = null, lastStepEvent = null;

const idNum = (id) => parseInt(String(id).replace(/\D/g, ""), 10) || 0;
const fmtTime = (ts) => { const d = new Date(ts); return isNaN(d) ? String(ts ?? "") : d.toLocaleTimeString(); };

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

async function getJSON(url) {
  const r = await fetch(url, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

// ------------------------------------------------------------------ command feed

function renderEvent(e) {
  const li = el("li", "ev");
  li.dataset.id = e.id;
  const decision = ["ALLOW", "HOLD", "DENY"].includes(e.decision) ? e.decision : "DENY";
  li.append(el("div", `badge ${decision}`, decision));
  const what = el("div", "what");
  const old = e.old_value === undefined || e.old_value === null ? "" : `${e.old_value} → `;
  what.append(document.createTextNode(`${e.name ?? `${e.kind ?? "function"} ${e.address ?? e.function}`} `));
  what.append(el("span", "val", e.new_value === null || e.new_value === undefined ? "" : `${old}${e.new_value}`));
  li.append(what);
  li.append(el("div", "reason", (e.reasons && e.reasons[0]) || (decision === "ALLOW" ? "safe: within limits" : "")));
  li.append(el("div", "meta", `${e.src_ip ?? "?"} · ${fmtTime(e.ts)} · ${e.id}`));
  li.addEventListener("click", () => select(e));
  return li;
}

function select(e) {
  selected = e.id;
  for (const [id, li] of rows) li.classList.toggle("sel", id === e.id);
  listeners.select.forEach((f) => f(e));
}

function addEvent(e, isNew) {
  if (!e || !e.id || rows.has(e.id)) return;
  const li = renderEvent(e);
  if (isNew) li.classList.add("new");
  const n = idNum(e.id);
  let before = null;
  for (const child of feed.children) if (idNum(child.dataset.id) < n) { before = child; break; }
  feed.insertBefore(li, before);
  rows.set(e.id, li);
  while (rows.size > MAX_ROWS) { const last = feed.lastElementChild; rows.delete(last.dataset.id); last.remove(); }
  $("empty").hidden = rows.size > 0;
  listeners.event.forEach((f) => f(e, isNew));
  if (isNew && e.decision !== "ALLOW") select(e);
}

// ------------------------------------------------------------------ agent steps

const LABEL = { tool: "tool", result: "result", note: "note", verdict: "verdict", text: "text sent",
                reply: "reply", action: "action", status: "status", cost: "cost", waiting: "waiting",
                released: "released", refused: "refused", report: "report" };

function addStep(s) {
  if (!s || stepIds.has(s.id)) return;
  stepIds.add(s.id);
  const steps = $("steps");
  if (s.event_id !== lastStepEvent) {
    steps.append(el("li", "step head", `Incident ${s.event_id ?? ""} · ${fmtTime(s.ts)}`));
    lastStepEvent = s.event_id;
  }
  const li = el("li", `step ${LABEL[s.kind] ? s.kind : "status"}`);
  const text = ["verdict", "note"].includes(s.kind) ? String(s.text ?? "").replace(/\*\*/g, "") : s.text ?? "";
  li.append(el("span", "k", LABEL[s.kind] ?? s.kind), el("span", "t", text));
  if (s.kind === "report" && s.event_id) {
    const a = el("a", "", " open report");
    a.href = `/reports/${encodeURIComponent(s.event_id)}`; a.target = "_blank"; a.style.color = "var(--accent)";
    li.lastChild.append(a);
  }
  steps.append(li);
  while (steps.children.length > MAX_STEPS) steps.firstElementChild.remove();
  $("no-steps").hidden = true;
  const box = $("agent");
  box.scrollTop = box.scrollHeight;
  if (["text", "reply", "released", "refused", "action"].includes(s.kind)) loadInbox();
}

// ------------------------------------------------------------------ operator's phone

async function sendReply(msg, reply, box) {
  box.querySelectorAll("button").forEach((b) => (b.disabled = true));
  let err = "";
  try {
    const r = await fetch("/operator/reply", {
      method: "POST", credentials: "same-origin", headers: { "content-type": "application/json" },
      body: JSON.stringify({ reply, event_id: msg.event_id, code: msg.code }) });
    if (r.status === 401) err = "Log in to reply.";
    else if (!r.ok) err = `Not accepted: ${(await r.json().catch(() => ({}))).detail ?? r.status}`;
  } catch (e) { err = "Could not reach the gateway."; }
  await loadInbox();
  if (err) { const b = document.querySelector(`.sms[data-id="${msg.event_id}"]`); if (b) b.append(el("div", "err", err)); }
}

function renderInbox(list) {
  const inbox = $("inbox");
  inbox.replaceChildren();
  $("no-texts").hidden = list.length > 0;
  for (const m of [...list].reverse()) {
    const box = el("div", "sms");
    box.dataset.id = m.event_id;
    box.append(el("p", "", m.text));
    const meta = el("div", "meta");
    meta.append(el("span", "", `${fmtTime(m.ts * 1000)} · ${m.event_id}`));
    if (m.open) meta.append(el("span", "", `reply code ${m.code} · good for ${Math.ceil(m.expires_in_s / 60)} min`));
    box.append(meta);
    if (m.reply) box.append(el("div", "answer", `You replied ${m.reply}`));
    else if (m.open) {
      const actions = el("div", "actions");
      const block = el("button", "btn-block", "BLOCK"), allow = el("button", "btn-allow", "ALLOW");
      block.addEventListener("click", () => sendReply(m, "BLOCK", box));
      allow.addEventListener("click", () => sendReply(m, "ALLOW", box));
      actions.append(block, allow);
      box.append(actions);
    } else box.append(el("div", "answer", "Expired, no reply taken"));
    inbox.append(box);
  }
}

let inboxBusy = false, admin = false;
async function loadInbox() {
  if (inboxBusy || !admin) return;
  inboxBusy = true;
  try { renderInbox(await getJSON("/operator/inbox")); }
  catch (e) { if (String(e).includes("401")) checkAdmin(); }
  finally { inboxBusy = false; }
}

// ------------------------------------------------------------------ admin login

function showAdmin(is) {
  admin = is;
  $("login").hidden = is;
  for (const id of ["who", "logout", "admin-tools"]) $(id).hidden = !is;
  $("need-login").hidden = is;
  if (!is) { $("inbox").replaceChildren(); $("no-texts").hidden = true; }
  else loadInbox();
}

async function checkAdmin() {
  try { showAdmin((await getJSON("/me")).admin === true); } catch (e) { showAdmin(false); }
}

$("login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("login-err").textContent = "";
  try {
    const r = await fetch("/login", { method: "POST", credentials: "same-origin",
      headers: { "content-type": "application/json" }, body: JSON.stringify({ password: $("pw").value }) });
    $("pw").value = "";
    if (r.ok) showAdmin(true);
    else $("login-err").textContent = r.status === 429 ? "Too many tries, wait a minute" : "Wrong password";
  } catch (e) { $("login-err").textContent = "Gateway not reachable"; }
});

const resetBtn = el("button", "ghost", "Reset demo");
$("admin-tools").append(resetBtn);
resetBtn.addEventListener("click", async () => {
  if (!confirm("Clear all events, texts and blocks, and put the station back to defaults?")) return;
  resetBtn.disabled = true;
  try {
    const r = await fetch("/reset", { method: "POST", credentials: "same-origin" });
    if (r.ok) location.reload(); else alert(`Reset failed (${r.status})`);
  } catch (e) { alert("Gateway not reachable"); }
  resetBtn.disabled = false;
});

$("logout").addEventListener("click", async () => {
  try { await fetch("/logout", { method: "POST", credentials: "same-origin" }); } catch (e) { /* ignore */ }
  showAdmin(false);
});

// ------------------------------------------------------------------ wiring

async function backfill() {
  try {
    const list = await getJSON("/events?limit=200");
    for (const e of list) addEvent(e, false);
    const latest = list.find((e) => e.decision !== "ALLOW");     // newest first
    if (!selected && latest) select(latest);
  } catch (e) { console.warn(e); }
  try { for (const s of await getJSON("/agent/log?limit=300")) addStep(s); } catch (e) { console.warn(e); }
  loadInbox();
}

function connect() {
  const es = new EventSource("/stream");
  es.addEventListener("event", (m) => { try { addEvent(JSON.parse(m.data), true); } catch (e) { console.warn(e); } });
  es.addEventListener("agent", (m) => { try { addStep(JSON.parse(m.data)); } catch (e) { console.warn(e); } });
  es.onopen = () => { status.textContent = "live"; status.className = "live"; backfill(); };
  es.onerror = () => { status.textContent = "reconnecting…"; status.className = "down"; };
}

window.backflow = { el, getJSON, fmtTime, listeners, rows, loadInbox, selected: () => selected,
                    isAdmin: () => admin };
checkAdmin();
connect();
backfill();
setInterval(loadInbox, 5000);     // expiry countdowns, and a safety net if a stream message is lost
