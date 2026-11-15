// Backflow operator console. Draws what the data source (data.js) reports: plant tiles, command
// feed, held commands with Approve / Reject, the forecast chart and the agent's steps.
// Everything from the network is written with textContent, never as HTML.
"use strict";
(() => {
  const { on, source } = window.Backflow;
  const $ = (id) => document.getElementById(id);
  const replay = source.mode === "replay";
  const NS = "http://www.w3.org/2000/svg";

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function svg(tag, attrs = {}, text) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text !== undefined) n.textContent = text;
    return n;
  }
  const idNum = (id) => parseInt(String(id).replace(/\D/g, ""), 10) || 0;
  const fmtTime = (ts) => {
    const d = new Date(typeof ts === "number" ? ts * 1000 : ts);
    return isNaN(d) ? String(ts ?? "") : d.toLocaleTimeString([], { hour12: false });
  };
  const mins = (m) => (m < 1 ? "under a minute" : `${Math.round(m)} min`);

  // ------------------------------------------------------------------ words for registers
  const onOff = (v) => (v ? "ON" : "OFF");
  const REG = {
    PUMP_CMD: ["Supply pump", onOff], DOSER_CMD: ["Chlorine doser", onOff], AUTO_MODE: ["Automatic control", onOff],
    SAFE_MODE: ["Safe mode", onOff], ALARM: ["Alarm", onOff],
    DOSER_RATE_PCT: ["Doser rate", (v) => `${v} %`],
    LEVEL_HIGH_SP: ["Pump-stop level", (v) => `${(v / 10).toFixed(1)} %`],
    LEVEL_LOW_SP: ["Pump-start level", (v) => `${(v / 10).toFixed(1)} %`],
    CHLORINE_TARGET: ["Chlorine target", (v) => `${(v / 100).toFixed(2)} mg/L`],
    LOGIC_VERSION: ["Logic version", (v) => `${v}`],
  };
  function describe(e) {
    const [label, f] = REG[e.name] || [e.name || `${e.kind || "function"} ${e.address ?? e.function ?? ""}`, (v) => `${v}`];
    const has = (v) => v !== null && v !== undefined;
    const val = has(e.new_value) ? (has(e.old_value) ? `${f(e.old_value)} → ${f(e.new_value)}` : `→ ${f(e.new_value)}`) : "";
    return { label, val };
  }
  // The physics reason in plain words, from the model's own violation record.
  function physicsReason(v) {
    // The model words some violations itself (a crossing after the forecast window, for one).
    // Only the plain "in about N plant-minutes" cases are rephrased here.
    if (v.message && !/in about/.test(v.message)) return v.message;
    const n = typeof v.minutes_until === "number" ? ` in ${mins(v.minutes_until)} of plant time` : "";
    switch (v.code) {
      case "CHLORINE_HIGH": return `Chlorine reaches ${v.limit} mg/L${n}`;
      case "CHLORINE_LOW": return `Chlorine falls below ${v.limit} mg/L${n}`;
      case "TANK_OVERFLOW": return `Tank passes the ${v.limit} % overflow guard${n}`;
      case "PRESSURE_LOSS": return `Tank falls below the ${v.limit} % low-pressure guard${n}`;
      default: return v.message || v.code;
    }
  }
  const violations = (e) => ((e.prediction && e.prediction.violations) || []).filter((v) => !v.pre_existing);
  const isTamper = (e) => String((e.reasons && e.reasons[0]) || "").startsWith("LOGIC_TAMPER");
  function reasons(e) {
    const phys = violations(e).map(physicsReason);
    const codes = violations(e).map((v) => v.code);
    const rest = (e.reasons || []).filter((r) => !codes.some((c) => String(r).startsWith(c)));
    let main = phys[0] || rest.shift() || (e.decision === "ALLOW" ? "Within limits" : "");
    if (phys.length > 1) rest.unshift(...phys.slice(1));
    return { main, rest, physics: phys.length > 0 };
  }

  const ICON = {
    ALLOW: "M2 8.5 6 12.5 14 3.5", HOLD: "M5 2.5v11M11 2.5v11", DENY: "M3 3l10 10M13 3 3 13" };
  function badge(decision, word) {
    const d = ICON[decision] ? decision : "DENY";
    const b = el("span", `badge ${d}`);
    const s = svg("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" });
    s.append(svg("path", { d: ICON[d], fill: "none", stroke: "currentColor", "stroke-width": d === "HOLD" ? 3.2 : 2.6, "stroke-linecap": "square" }));
    b.append(s, document.createTextNode(word || d));
    return b;
  }

  // ------------------------------------------------------------------ mode bar and session
  const modebar = $("modebar");
  function setMode(cls, tag, text) {
    modebar.className = `modebar ${cls}`;
    $("mode-tag").textContent = tag;
    $("mode-text").textContent = text;
  }
  if (replay) {
    document.title = "REPLAY · Backflow operator console";
    setMode("replay", "REPLAY", "Loading the recording…");
    $("mode-switch").textContent = "Switch to live";
    $("mode-switch").href = "console.html";
    $("player").hidden = false;
    $("pl-toggle").addEventListener("click", () => source.toggle());
    $("pl-restart").addEventListener("click", () => source.restart());
    document.querySelectorAll(".pl-speed").forEach((b) => b.addEventListener("click", () => source.setSpeed(+b.dataset.x)));
  }
  on("player", (p) => {
    if (p.missing) { setMode("down", "REPLAY", "No recording found (dashboard/replay/scenario_0_1.js). Record one with tools/record_replay.py."); return; }
    const m = p.meta;
    const when = new Date(m.t0 * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short", hour12: false });
    setMode("replay", "REPLAY", p.waiting
      ? `Recorded run of ${when}. Paused: press ${p.waiting.reply === "BLOCK" ? "Reject" : "Approve"} under Held commands, as the operator did.`
      : `Recorded run of ${when} against ${m.station}. Not live: nothing here reaches a station.`);
    $("pl-toggle").textContent = p.done ? "Play again" : p.playing ? "Pause" : "Play";
    $("pl-bar").value = p.total ? Math.min(1, p.t / p.total) : 0;
    document.querySelectorAll(".pl-speed").forEach((b) => b.setAttribute("aria-pressed", String(+b.dataset.x === p.speed)));
  });
  on("conn", (c) => {
    if (c === "live") setMode("live", "LIVE", "Connected to the gateway. Commands and plant values are real-time.");
    else if (c === "reconnecting") setMode("down", "LIVE", "Lost the gateway stream. Reconnecting…");
  });
  on("auth", (ok) => {
    if (ok === null) { $("overlay").hidden = false; return; }
    if (!ok) { location.replace("login.html?next=console.html"); return; }
    for (const id of ["who", "reset", "logout"]) $(id).hidden = false;
  });
  $("logout").addEventListener("click", async () => { await source.logout(); location.href = "index.html"; });
  $("reset").addEventListener("click", async () => {
    if (!confirm("Clear all events, messages and blocks, and put the station back to its defaults?")) return;
    if (await source.reset()) location.reload(); else alert("Reset failed.");
  });

  // ------------------------------------------------------------------ plant tiles
  const tile = (id) => { const t = $(id); return { t, v: t.querySelector(".v"), s: t.querySelector(".s") }; };
  const T = { cl: tile("t-cl"), pump: tile("t-pump"), doser: tile("t-doser"), mode: tile("t-mode"),
              demand: tile("t-demand"), site: tile("t-site"), cab: tile("t-cab"), health: tile("t-health") };
  function put(x, text, sub, opts = {}) {
    x.v.replaceChildren();
    if (opts.lamp) { const s = el("span", "state"); s.append(el("i"), document.createTextNode(text)); x.v.append(s); }
    else if (opts.unit) { x.v.append(document.createTextNode(text + " "), el("small", "", opts.unit)); }
    else x.v.textContent = text;
    x.s.textContent = sub || "";
    x.t.classList.toggle("on", !!opts.on);
    x.t.classList.toggle("warn", !!opts.warn);
    x.t.classList.toggle("nosensor", !!opts.nosensor);
  }
  let lastState = null;
  function renderHealth() {
    const s = lastState;
    if (!s) return;
    const bad = (text, sub) => { put(T.health, text, sub, { lamp: true }); T.health.t.classList.add("bad"); $("plant").classList.add("stale"); };
    if (s.unreachable) return bad("NO GATEWAY", "the gateway API is not answering");
    const age = s.ts ? Math.max(0, source.now() - s.ts) : null;
    const ageText = age === null ? "no reading yet" : age < 2 ? "updated just now" : `last update ${age < 90 ? `${Math.round(age)} s` : `${Math.round(age / 60)} min`} ago`;
    if (!s.connected) return bad("DISCONNECTED", s.ts ? `${ageText}; values shown are the last known` : "no reading yet");
    if (age > 5) return bad("STALE", ageText);
    T.health.t.classList.remove("bad"); $("plant").classList.remove("stale");
    const up = s.input_registers && s.input_registers.UPTIME_S;
    put(T.health, "CONNECTED", `${ageText}${up !== undefined ? ` · up ${up < 120 ? `${up} s` : `${Math.round(up / 60)} min`}` : ""}`, { lamp: true, on: true });
  }
  on("state", (s) => {
    if (s.unreachable) { lastState = { ...(lastState || {}), unreachable: true }; renderHealth(); return; }
    lastState = s;
    renderHealth();
    if (!s.holding || s.holding.TANK_LEVEL_PCT === undefined) return;
    const h = s.holding, c = s.coils, di = s.discrete_inputs, ir = s.input_registers;
    const level = h.TANK_LEVEL_PCT / 10;
    $("tank-v").replaceChildren(document.createTextNode(level.toFixed(1) + " "), el("small", "", "%"));
    $("tank-s").textContent = `pump starts at ${h.LEVEL_LOW_SP / 10}, stops at ${h.LEVEL_HIGH_SP / 10} %`;
    $("tank-fill").style.height = `${Math.max(0, Math.min(100, level))}%`;
    $("tank-hi").style.bottom = `${Math.min(100, h.LEVEL_HIGH_SP / 10)}%`;
    $("tank-lo").style.bottom = `${Math.min(100, h.LEVEL_LOW_SP / 10)}%`;
    const cl = h.CHLORINE_MGL / 100;
    put(T.cl, cl.toFixed(2), `target ${(h.CHLORINE_TARGET / 100).toFixed(2)} · alarm line 4.0`, { unit: "mg/L", warn: cl >= 4 });
    put(T.pump, di.PUMP_RUNNING ? "RUNNING" : "OFF", `command ${onOff(c.PUMP_CMD)}`, { lamp: true, on: di.PUMP_RUNNING });
    put(T.doser, di.DOSER_RUNNING ? "DOSING" : "OFF", `rate ${h.DOSER_RATE_PCT} % · command ${onOff(c.DOSER_CMD)}`, { lamp: true, on: di.DOSER_RUNNING });
    put(T.mode, c.SAFE_MODE ? "SAFE MODE" : c.AUTO_MODE ? "AUTO" : "MANUAL",
        c.ALARM ? "ALARM sounding" : c.SAFE_MODE ? "doser off, remote writes ignored" : c.AUTO_MODE ? "station runs the pump" : "remote pump control",
        { warn: !!(c.SAFE_MODE || c.ALARM) });
    put(T.demand, (ir.DEMAND_PCT / 10).toFixed(0), "of maximum town demand", { unit: "%" });
    const site = di.PRESENCE || di.CABINET_OPEN;
    put(T.site, site ? "SOMEONE" : "NOBODY", `${di.PRESENCE ? "motion" : "no motion"} · cabinet ${di.CABINET_OPEN ? "open" : "closed"}`, { lamp: true, on: site });
    // DHT11: the register reads 0 until the sensor has answered once, and 0 % humidity is outside
    // what a DHT11 can report, so 0/0 (or a missing register, or 0xFFFF) means there is no sensor.
    const tC = ir.CABINET_TEMP_C, rh = ir.CABINET_HUMIDITY;
    const dead = tC === undefined || rh === undefined || tC === 0xFFFF || rh === 0xFFFF || (tC === 0 && rh === 0);
    if (dead) put(T.cab, "no sensor", "temperature and humidity not reported", { nosensor: true });
    else put(T.cab, (tC / 10).toFixed(1), `humidity ${(rh / 10).toFixed(0)} %`, { unit: "°C" });
  });
  setInterval(renderHealth, 1000);

  // ------------------------------------------------------------------ command feed
  const events = new Map(), rows = new Map(), feed = $("feed");
  let selected = null;
  function renderRow(e) {
    const li = el("li", `ev ${e.decision}`);
    li.dataset.id = e.id;
    li.tabIndex = 0;
    const d = describe(e), r = reasons(e);
    li.append(badge(e.decision, isTamper(e) ? "TAMPER" : undefined));
    const what = el("div", "what", d.label + " ");
    what.append(el("span", "val", d.val));
    li.append(what);
    li.append(el("div", "why", r.rest.length ? `${r.main} (+${r.rest.length} more ${r.rest.length > 1 ? "reasons" : "reason"})` : r.main));
    li.append(el("div", "meta", `${fmtTime(e.ts)} · ${e.src_ip ?? "?"} · ${e.name ?? ""} · ${e.id}`));
    li.addEventListener("click", () => select(e.id));
    li.addEventListener("keydown", (k) => { if (k.key === "Enter" || k.key === " ") { k.preventDefault(); select(e.id); } });
    return li;
  }
  function select(id) {
    selected = id;
    for (const [k, li] of rows) li.classList.toggle("sel", k === id);
    showForecast(events.get(id));
  }
  on("event", (e, isNew) => {
    if (!e || !e.id || events.has(e.id)) return;
    events.set(e.id, e);
    const li = renderRow(e);
    if (isNew) li.classList.add("fresh");
    const n = idNum(e.id);
    let before = null;
    for (const child of feed.children) if (idNum(child.dataset.id) < n) { before = child; break; }
    feed.insertBefore(li, before);
    rows.set(e.id, li);
    $("feed-empty").hidden = true;
    $("feed-count").textContent = `${events.size} shown, newest first`;
    if (e.decision !== "ALLOW" && (isNew || !selected || idNum(selected) < n)) select(e.id);
    else if (!selected) select(e.id);
    renderHeld();
  });

  // ------------------------------------------------------------------ held commands
  let inbox = [];
  const outcomes = new Map();     // event id -> { kind, text } from the agent log (reply / released / refused)
  const replyErr = new Map();
  let heldSig = "";
  on("inbox", (list) => { inbox = list || []; renderHeld(); });

  function heldCard(e, q) {
    const out = outcomes.get(e.id) || {};
    const answered = (q && q.reply) || out.reply;
    const open = !answered && (!q || q.open);
    const card = el("div", `hold-card ${open ? "open" : "done"}`);
    const d = describe(e), r = reasons(e);
    const top = el("div", "top");
    top.append(badge("HOLD", isTamper(e) ? "TAMPER" : undefined), el("span", "what", `${d.label} ${d.val}`),
               el("span", "id", `${fmtTime(e.ts)} · ${e.id}`));
    card.append(top);
    if (!open) {
      let text = answered === "BLOCK" ? "You rejected it. The command was never sent to the station."
        : answered === "ALLOW" ? (out.refused ? `You approved it, but the release was refused: ${out.refused}`
          : out.released ? "You approved it. The gateway re-checked it and sent it to the station."
          : out.closed ? "You approved it, but it was not released: it did not pass the safety re-check. The agent's assessment says why."
          : "You approved it. It is being re-checked against the physics model before any release.")
        : "The question expired without a reply. Nothing was sent.";
      card.append(el("div", "outcome", text));
      return card;
    }
    card.append(el("div", "physics", r.main));
    const actions = el("div", "actions");
    const reject = el("button", "reject", "Reject"), approve = el("button", "approve", "Approve");
    if (!q) { reject.disabled = approve.disabled = true; }
    reject.addEventListener("click", () => answer(q, "BLOCK", card));
    approve.addEventListener("click", () => answer(q, "ALLOW", card));
    actions.append(reject, approve);
    actions.append(el("span", "note", !q ? "Held and not sent. Waiting for the agent's message before you can decide."
      : `Not sent to the station. An approval is re-checked against physics before release. ${q.code ? `Code ${q.code}, ` : ""}valid ${Math.max(1, Math.ceil((q.expires_in_s || 0) / 60))} min.`));
    card.append(actions);
    if (r.rest.length) card.append(el("div", "also", `Also: ${r.rest.join("; ")}`));
    if (q) {
      const msg = el("div", "msg");
      msg.append(el("div", "from", "Message to the operator"), el("div", "", q.text));
      card.append(msg);
      if (q.covers && q.covers.length) card.append(el("div", "covers", `Approving would send: ${q.covers.join("; ")}`));
    }
    if (replyErr.get(e.id)) card.append(el("div", "err", replyErr.get(e.id)));
    return card;
  }
  async function answer(q, reply, card) {
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    const err = await source.reply(q, reply);
    if (err) replyErr.set(q.event_id, err); else replyErr.delete(q.event_id);
    renderHeld();
  }
  function renderHeld() {
    const box = $("held");
    const byId = new Map(inbox.map((q) => [q.event_id, q]));
    const held = [...events.values()].filter((e) => e.decision === "HOLD" || byId.has(e.id)).sort((a, b) => idNum(b.id) - idNum(a.id));
    const isOpen = (e) => { const q = byId.get(e.id), o = outcomes.get(e.id) || {}; return !((q && q.reply) || o.reply) && (!q || q.open); };
    const openOnes = held.filter(isOpen);
    // Redraw only when something changed, so a button is never replaced under the pointer.
    const sig = JSON.stringify([held.map((e) => e.id), inbox.map((q) => [q.event_id, q.reply, q.open, Math.ceil((q.expires_in_s || 0) / 60)]),
                                [...outcomes], [...replyErr]]);
    if (sig === heldSig) return;
    heldSig = sig;
    box.replaceChildren(...openOnes.map((e) => heldCard(e, byId.get(e.id))),
                        ...held.filter((e) => !isOpen(e)).slice(0, 6).map((e) => heldCard(e, byId.get(e.id))));
    $("held-empty").hidden = held.length > 0;
    $("held-count").textContent = openOnes.length ? `${openOnes.length} waiting for you` : held.length ? "none waiting" : "";
  }

  // ------------------------------------------------------------------ agent activity
  const LABEL = { tool: "Looked up", result: "Found", note: "Note", verdict: "Assessment", text: "Told operator",
                  reply: "Operator", action: "Action", status: "Status", cost: "Model cost", waiting: "Waiting",
                  released: "Released", refused: "Refused", report: "Report" };
  const stepIds = new Set();
  let lastStepEvent;
  on("agent", (s) => {
    if (!s || stepIds.has(s.id)) return;
    stepIds.add(s.id);
    const steps = $("steps");
    if (s.event_id !== lastStepEvent) {
      const e = events.get(s.event_id);
      steps.append(el("li", "inc", `Incident ${s.event_id ?? ""}${e ? `: ${describe(e).label} ${describe(e).val}` : ""}`));
      lastStepEvent = s.event_id;
    }
    const li = el("li", `step ${LABEL[s.kind] ? s.kind : "status"}`);
    const text = String(s.text ?? "").replace(/\*\*/g, "");
    const t = el("span", "t", text);
    li.append(el("span", "k", `${LABEL[s.kind] ?? s.kind}`), t);
    li.title = fmtTime(s.ts);
    if (s.kind === "report" && s.event_id) {
      const a = el("a", "", "Open the incident report");
      a.href = `report.html?id=${encodeURIComponent(s.event_id)}${replay ? "&mode=replay" : ""}`;
      a.target = "_blank";
      t.append(document.createTextNode(" "), a);
    }
    steps.append(li);
    $("steps-empty").hidden = true;
    const box = $("agent-scroll");
    box.scrollTop = box.scrollHeight;
    if (s.event_id && ["reply", "released", "refused", "report", "verdict"].includes(s.kind)) {
      const o = outcomes.get(s.event_id) || {};
      if ((s.kind === "report" || s.kind === "verdict") && o.reply) o.closed = true;   // the incident is finished
      if (s.kind === "reply") o.reply = /ALLOW/.test(text) ? "ALLOW" : "BLOCK";
      if (s.kind === "released") o.released = true;
      if (s.kind === "refused") o.refused = text;
      outcomes.set(s.event_id, o);
      renderHeld();
    }
  });

  on("reset", () => {
    events.clear(); rows.clear(); stepIds.clear(); outcomes.clear(); replyErr.clear();
    inbox = []; selected = null; lastStepEvent = undefined; lastState = null; heldSig = "";
    feed.replaceChildren(); $("steps").replaceChildren(); $("held").replaceChildren();
    $("feed-empty").hidden = $("steps-empty").hidden = $("held-empty").hidden = false;
    $("feed-count").textContent = "newest first"; $("held-count").textContent = "";
    $("fc-title").textContent = "physics model forecast"; $("fc-sub").textContent = "Select a command to see its forecast.";
    clearChart();
  });

  // ------------------------------------------------------------------ forecast chart
  const chartBox = $("chart"), tip = $("tip");
  const SERIES = { CHLORINE_HIGH: 2, CHLORINE_LOW: 2, TANK_OVERFLOW: 1, PRESSURE_LOSS: 1, PUMP_SHORT_CYCLE: 1 };
  const UNIT = { 1: ["Tank level", "%", 1], 2: ["Chlorine", "mg/L", 2] };
  const CL_REGS = ["DOSER_RATE_PCT", "DOSER_CMD", "CHLORINE_TARGET"];
  let token = 0;
  function clearChart() { chartBox.querySelectorAll("svg").forEach((n) => n.remove()); tip.hidden = true; }

  async function showForecast(e) {
    if (!e) return;
    const mine = ++token;
    clearChart();
    const d = describe(e);
    $("fc-title").textContent = `${e.id} · ${d.label} ${d.val}`;
    $("fc-sub").textContent = "Loading the forecast…";
    let full;
    try { full = await source.getEvent(e.id); } catch (err) { if (mine === token) $("fc-sub").textContent = "The forecast for this command is not available."; return; }
    if (mine !== token) return;
    const p = full.prediction, traj = p && p.trajectory;
    if (!traj || traj.length < 2) {
      $("fc-sub").textContent = `${reasons(full).main || "No forecast"}. No physics forecast was run for this command.`;
      return;
    }
    const v = (p.violations || []).find((x) => !x.pre_existing && SERIES[x.code]);
    const idx = v ? SERIES[v.code] : CL_REGS.includes(e.name) ? 2 : 1;
    const [name, unit, dp] = UNIT[idx];
    $("fc-sub").textContent = v
      ? `Forecast if it had been sent. The gateway ${full.decision === "DENY" ? "denied" : "held"} it, so this did not happen.`
      : `${name} stays inside the limits for the next ${Math.round(traj[traj.length - 1][0])} min of plant time (${full.decision}).`;
    draw(traj, p.baseline_trajectory, idx, name, unit, dp, v);
  }

  function draw(traj, base, idx, name, unit, dp, v) {
    const r = chartBox.getBoundingClientRect();
    const W = Math.max(420, Math.round(r.width - 16)), H = Math.max(140, Math.round(r.height - 14));
    const L = 52, R = 18, Tp = 26, B = 44;
    const thin = (a) => { const step = Math.max(1, Math.floor(a.length / 300)); return a.filter((_, i) => i % step === 0 || i === a.length - 1); };
    const pts = thin(traj), bpts = base && base.length > 1 ? thin(base) : null;
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[idx]);
    const limit = v && typeof v.limit === "number" ? v.limit : null;
    const all = ys.concat(bpts ? bpts.map((p) => p[idx]) : [], limit === null ? [] : [limit]);
    let y0 = Math.min(...all), y1 = Math.max(...all);
    const pad = (y1 - y0 || 1) * 0.18; y0 = Math.max(0, y0 - pad); y1 += pad;
    const x1 = xs[xs.length - 1] || 1;
    const X = (t) => L + (W - L - R) * (t / x1), Y = (y) => Tp + (H - Tp - B) * (1 - (y - y0) / (y1 - y0));
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": `${name} forecast over ${Math.round(x1)} minutes${v ? `, crossing the limit of ${limit} ${unit}` : ""}` });
    for (let i = 0; i <= 4; i++) {
      const y = y0 + (y1 - y0) * i / 4;
      s.append(svg("line", { x1: L, x2: W - R, y1: Y(y), y2: Y(y), class: "grid" }));
      s.append(svg("text", { x: L - 8, y: Y(y) + 5, class: "tick", "text-anchor": "end" }, y.toFixed(dp === 2 ? 1 : 0)));
    }
    for (let i = 0; i <= 4; i++) {
      const t = x1 * i / 4;
      s.append(svg("text", { x: X(t), y: H - B + 18, class: "tick", "text-anchor": i === 4 ? "end" : i ? "middle" : "start" }, `${Math.round(t)}`));
    }
    s.append(svg("line", { x1: L, x2: W - R, y1: H - B, y2: H - B, class: "axis" }));
    s.append(svg("text", { x: (L + W - R) / 2, y: H - 8, class: "tick", "text-anchor": "middle" }, "minutes of plant time after the command"));
    s.append(svg("text", { x: L, y: 15, class: "tick" }, `${name} (${unit})`));
    if (limit !== null) {
      const high = v.code !== "CHLORINE_LOW" && v.code !== "PRESSURE_LOSS";
      const top = high ? Tp : Y(limit), hgt = high ? Y(limit) - Tp : H - B - Y(limit);
      if (hgt > 0) s.append(svg("rect", { x: L, y: top, width: W - L - R, height: hgt, class: "over" }));
      s.append(svg("line", { x1: L, x2: W - R, y1: Y(limit), y2: Y(limit), class: "limit" }));
      s.append(svg("text", { x: L + 8, y: Y(limit) + (high ? -7 : 18), class: "lbl red" }, `Safe limit ${limit} ${unit}`));
    }
    const path = (a) => a.map((p) => `${X(Math.min(p[0], x1)).toFixed(1)},${Y(p[idx]).toFixed(1)}`).join(" ");
    if (bpts) {
      s.append(svg("polyline", { points: path(bpts), class: "base" }));
      const last = bpts[bpts.length - 1];
      s.append(svg("text", { x: W - R - 4, y: Y(last[idx]) + 18, class: "lbl grey", "text-anchor": "end" }, "if nothing changes"));
    }
    s.append(svg("polyline", { points: path(pts), class: "line" }));
    const end = pts[pts.length - 1];
    s.append(svg("text", { x: W - R - 4, y: Y(end[idx]) < (Tp + H - B) / 2 ? Y(end[idx]) + 22 : Y(end[idx]) - 10, class: "lbl", "text-anchor": "end" }, "with this command"));
    const lowSide = v && (v.code === "CHLORINE_LOW" || v.code === "PRESSURE_LOSS");
    const crossed = limit !== null && ys.some((y) => (lowSide ? y <= limit : y >= limit));   // inside this window
    if (v && crossed && typeof v.minutes_until === "number" && v.minutes_until <= x1) {
      const cx = X(v.minutes_until), cy = Y(limit);
      s.append(svg("line", { x1: cx, x2: cx, y1: cy, y2: H - B, class: "limit" }));
      s.append(svg("circle", { cx, cy, r: 7, class: "cross" }));
      s.append(svg("text", { x: Math.min(cx + 12, W - R - 170), y: H - B - 10, class: "lbl red" }, `crosses at ${mins(v.minutes_until)}`));
    }
    const hair = svg("line", { y1: Tp, y2: H - B, class: "hair", visibility: "hidden" });
    const dot = svg("circle", { r: 5, class: "dot", visibility: "hidden" });
    const hit = svg("rect", { x: L, y: Tp, width: W - L - R, height: H - Tp - B, fill: "transparent" });
    s.append(hair, dot, hit);
    hit.addEventListener("mousemove", (ev) => {
      const b = s.getBoundingClientRect(), t = ((ev.clientX - b.left) / b.width * W - L) / (W - L - R) * x1;
      let k = 0;
      for (let i = 1; i < xs.length; i++) if (Math.abs(xs[i] - t) < Math.abs(xs[k] - t)) k = i;
      hair.setAttribute("x1", X(xs[k])); hair.setAttribute("x2", X(xs[k]));
      dot.setAttribute("cx", X(xs[k])); dot.setAttribute("cy", Y(ys[k]));
      hair.setAttribute("visibility", "visible"); dot.setAttribute("visibility", "visible");
      tip.textContent = `at ${Math.round(xs[k])} min: ${ys[k].toFixed(dp)} ${unit}`;
      tip.hidden = false;
    });
    hit.addEventListener("mouseleave", () => { hair.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true; });
    clearChart();
    chartBox.append(s);
  }
  // The chart is drawn to the size of its box; redraw when the box changes (window resize, or
  // the held-commands panel above it growing or shrinking).
  let resizeTimer, drawnFor = "";
  new ResizeObserver(() => {
    const r = chartBox.getBoundingClientRect(), key = `${Math.round(r.width)}x${Math.round(r.height)}`;
    if (key === drawnFor) return;
    drawnFor = key;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => selected && showForecast(events.get(selected)), 150);
  }).observe(chartBox);

  source.start();
})();
