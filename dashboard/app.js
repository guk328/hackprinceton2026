// Live event feed: backfill from GET /events, then append from /stream (SSE, event "event").
// Rows are keyed by event id, so an event delivered by both paths is shown once.
"use strict";

const feed = document.getElementById("feed");
const empty = document.getElementById("empty");
const status = document.getElementById("status");
const rows = new Map();               // id -> <li>
const MAX_ROWS = 500;

const idNum = (id) => parseInt(String(id).replace(/\D/g, ""), 10) || 0;

function fmtTime(ts) {
  const d = new Date(ts);
  return isNaN(d) ? String(ts ?? "") : d.toLocaleTimeString();
}

function fmtValue(e) {
  const v = e.new_value;
  if (e.old_value === undefined || e.old_value === null) return String(v);
  return `${e.old_value} → ${v}`;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function render(e) {
  const li = el("li", "ev");
  li.dataset.id = e.id;
  const decision = ["ALLOW", "HOLD", "DENY"].includes(e.decision) ? e.decision : "DENY";
  li.append(el("div", `badge ${decision}`, decision));

  const what = el("div", "what");
  what.append(document.createTextNode(`${e.name ?? `${e.kind} ${e.address}`} `));
  what.append(el("span", "val", fmtValue(e)));
  li.append(what);

  li.append(el("div", "meta", `${e.src_ip ?? "?"} · ${fmtTime(e.ts)} · ${e.id}`));
  const reason = (e.reasons && e.reasons[0]) || (decision === "ALLOW" ? "within envelope" : "");
  li.append(el("div", "reason", reason));
  return li;
}

function add(e, isNew) {
  if (!e || !e.id || rows.has(e.id)) return;
  const li = render(e);
  if (isNew) li.classList.add("new");
  // keep newest first by numeric id, whatever order the two paths deliver in
  const n = idNum(e.id);
  let before = null;
  for (const child of feed.children) {
    if (idNum(child.dataset.id) < n) { before = child; break; }
  }
  feed.insertBefore(li, before);
  rows.set(e.id, li);
  while (rows.size > MAX_ROWS) {
    const last = feed.lastElementChild;
    rows.delete(last.dataset.id);
    last.remove();
  }
  empty.hidden = rows.size > 0;
}

async function backfill() {
  try {
    const r = await fetch("/events?limit=200");
    if (!r.ok) throw new Error(r.status);
    for (const e of await r.json()) add(e, false);
  } catch (err) {
    console.warn("backfill failed", err);
  }
}

function connect() {
  // Subscribe first, then backfill: anything saved in between arrives on the stream
  // and the id check drops the copy.
  const es = new EventSource("/stream");
  es.addEventListener("event", (m) => {
    try { add(JSON.parse(m.data), true); } catch (err) { console.warn("bad event", err); }
  });
  es.onopen = () => {
    status.textContent = "live";
    status.className = "live";
    backfill();                         // also refills after a reconnect
  };
  es.onerror = () => {
    status.textContent = "reconnecting…";
    status.className = "down";
  };
}

connect();
backfill();                           // in case the stream never opens
