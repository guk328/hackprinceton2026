// Backflow operator console. Draws what the data source (data.js) reports:
//   left   : the station as a schematic of the rig (tank, pump K1, doser K2, alarm LED) and the command log
//   centre : the selected command; a held one becomes the hero, with its forecast, the agent's
//            briefing and Approve / Reject (which post the operator's reply through data.js)
//   right  : the agent's work as a plain-language timeline, raw tool calls behind an expand
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

  // ------------------------------------------------------------------ words for registers and reasons
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
    return { label, val, f, text: `${label} ${val}`.trim() };
  }
  // The physics reason in plain words, from the model's own violation record. The model words some
  // violations itself (a crossing after the forecast window); only the "in about N" cases are rephrased.
  function physicsReason(v) {
    if (v.message && !/in about/.test(v.message)) return v.message;
    const n = typeof v.minutes_until === "number" ? ` in ${mins(v.minutes_until)}` : "";
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
    const main = phys[0] || rest.shift() || (e.decision === "ALLOW" ? "Within limits" : "");
    if (phys.length > 1) rest.unshift(...phys.slice(1));
    return { main, rest, physics: phys.length > 0 };
  }
  const ICON = { ALLOW: "M2 8.5 6 12.5 14 3.5", HOLD: "M5 2.5v11M11 2.5v11", DENY: "M3 3l10 10M13 3 3 13" };
  function badge(decision, word, size) {
    const d = ICON[decision] ? decision : "DENY";
    const b = el("span", `badge ${d}${size ? ` ${size}` : ""}`);
    const s = svg("svg", { viewBox: "0 0 16 16", "aria-hidden": "true" });
    s.append(svg("path", { d: ICON[d], fill: "none", stroke: "currentColor", "stroke-width": d === "HOLD" ? 3.2 : 2.6, "stroke-linecap": "square" }));
    b.append(s, document.createTextNode(word || d));
    return b;
  }

  // ------------------------------------------------------------------ top bar: mode, session, station link
  const setModeText = (t) => { $("mode-text").textContent = t; $("mode-text").title = t; };
  if (replay) {
    document.body.classList.add("replay");
    document.title = "REPLAY · Backflow console";
    $("mode-tag").textContent = "REPLAY";
    setModeText("Loading the recording…");
    $("mode-switch").textContent = "Switch to live";
    $("mode-switch").href = "console.html";
    $("player").hidden = false;
    $("pl-toggle").addEventListener("click", () => source.toggle());
    $("pl-restart").addEventListener("click", () => source.restart());
    document.querySelectorAll(".pl-speed").forEach((b) => b.addEventListener("click", () => source.setSpeed(+b.dataset.x)));
  }
  on("player", (p) => {
    if (p.missing) { setModeText("No recording found. Record one with dashboard/tools/record_replay.py."); return; }
    const when = new Date(p.meta.t0 * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
    setModeText(p.waiting ? `Paused. Press ${p.waiting.reply === "BLOCK" ? "Reject" : "Approve"}, as the recorded operator did.`
      : `Recorded ${when} on ${p.meta.station}. Nothing here reaches a station.`);
    $("pl-toggle").textContent = p.done ? "Play again" : p.playing ? "Pause" : "Play";
    $("pl-bar").value = p.total ? Math.min(1, p.t / p.total) : 0;
    document.querySelectorAll(".pl-speed").forEach((b) => b.setAttribute("aria-pressed", String(+b.dataset.x === p.speed)));
  });
  on("conn", (c) => {
    document.body.classList.toggle("down", c === "reconnecting");
    if (c === "live") setModeText("Connected to the gateway");
    else if (c === "reconnecting") setModeText("Lost the gateway stream. Reconnecting…");
  });
  on("auth", (ok) => {
    if (ok === null) { $("overlay").hidden = false; return; }
    if (!ok) { location.replace("login.html"); return; }
    for (const id of ["reset", "logout"]) $(id).hidden = false;
  });
  $("logout").addEventListener("click", async () => { await source.logout(); location.href = "index.html"; });
  $("reset").addEventListener("click", async () => {
    if (!confirm("Clear all events, messages and blocks, and put the station back to its defaults?")) return;
    if (await source.reset()) location.reload(); else alert("Reset failed.");
  });

  // ------------------------------------------------------------------ station: live values drive the schematic
  let state = null;
  const num = (node, text, unit) => node.replaceChildren(document.createTextNode(text + " "), el("small", "", unit));
  function renderLink() {
    const link = $("link"), s = state;
    const bad = (t) => { link.classList.add("bad"); $("link-text").textContent = t; $("station").classList.add("stale"); };
    if (!s) return;
    if (s.unreachable) return bad("Gateway not answering");
    const age = s.ts ? Math.max(0, source.now() - s.ts) : null;
    if (!s.connected) return bad(`Station disconnected${age !== null ? `, last seen ${age < 90 ? `${Math.round(age)} s` : `${Math.round(age / 60)} min`} ago` : ""}`);
    if (age > 5) return bad(`Station data is ${Math.round(age)} s old`);
    link.classList.remove("bad"); $("station").classList.remove("stale");
    $("link-text").textContent = "Station connected";
  }
  on("state", (s) => {
    if (s.unreachable) { state = { ...(state || {}), unreachable: true }; renderLink(); return; }
    state = s;
    renderLink();
    if (!s.holding || s.holding.TANK_LEVEL_PCT === undefined) return;
    const h = s.holding, c = s.coils, di = s.discrete_inputs, ir = s.input_registers;
    const level = h.TANK_LEVEL_PCT / 10, y = (pct) => 166 - 1.4 * Math.max(0, Math.min(100, pct));
    $("water").setAttribute("transform", `translate(0,${y(level).toFixed(1)})`);
    for (const [id, sp] of [["sp-hi", h.LEVEL_HIGH_SP / 10], ["sp-lo", h.LEVEL_LOW_SP / 10]]) { $(id).setAttribute("y1", y(sp)); $(id).setAttribute("y2", y(sp)); }
    $("g-pump").classList.toggle("on", !!di.PUMP_RUNNING);
    $("g-doser").classList.toggle("on", !!di.DOSER_RUNNING);
    $("g-alarm").classList.toggle("on", !!c.ALARM);
    $("flow-in").classList.toggle("on", !!di.PUMP_RUNNING);
    $("flow-dose").classList.toggle("on", !!di.DOSER_RUNNING);
    $("flow-out").classList.toggle("on", ir.DEMAND_PCT > 0 && level > 0);
    num($("r-level"), level.toFixed(1), "%");
    const cl = h.CHLORINE_MGL / 100;
    num($("r-cl"), cl.toFixed(2), "mg/L");
    $("read-cl").classList.toggle("warn", cl >= 4);
    num($("r-demand"), (ir.DEMAND_PCT / 10).toFixed(0), "%");
    // Mode and alarm are separate states: the station can be in AUTO with the alarm on.
    const mode = $("chip-mode");
    mode.textContent = c.SAFE_MODE ? "SAFE MODE" : c.AUTO_MODE ? "AUTO" : "MANUAL";
    mode.classList.toggle("safe", !!c.SAFE_MODE);
    $("chip-alarm").textContent = c.ALARM ? "ALARM ON" : "ALARM OFF";
    $("chip-alarm").classList.toggle("on", !!c.ALARM);
    $("f-pump").textContent = di.PUMP_RUNNING ? "running" : "off";
    $("f-doser").textContent = di.DOSER_RUNNING ? `dosing ${h.DOSER_RATE_PCT} %` : `off, set ${h.DOSER_RATE_PCT} %`;
    $("f-site").textContent = di.PRESENCE ? "motion" : di.CABINET_OPEN ? "cabinet open" : "nobody";
    // DHT11: the register stays 0 until the sensor has answered once, and 0 % humidity is outside
    // what a DHT11 reports, so 0/0 (or a missing register, or 0xFFFF) means there is no sensor.
    const tC = ir.CABINET_TEMP_C, rh = ir.CABINET_HUMIDITY;
    const dead = tC === undefined || rh === undefined || tC === 0xFFFF || rh === 0xFFFF || (tC === 0 && rh === 0);
    $("f-cab").textContent = dead ? "no sensor" : `${(tC / 10).toFixed(1)} °C, ${(rh / 10).toFixed(0)} %`;
    updateCard();
  });
  setInterval(renderLink, 1000);

  // ------------------------------------------------------------------ shared incident facts
  const events = new Map(), rows = new Map(), feed = $("feed");
  const stepsBy = new Map();          // event id -> agent steps, in order
  let inbox = [], selected = null;
  const replyErr = new Map();
  const question = (id) => inbox.find((q) => q.event_id === id);
  const stepsOf = (id) => stepsBy.get(id) || [];
  // Where one held command stands, from the operator's inbox and the agent's steps.
  function incident(e) {
    const st = stepsOf(e.id), q = question(e.id);
    const has = (k) => st.some((s) => s.kind === k);
    const replyStep = st.find((s) => s.kind === "reply");
    const reply = (q && q.reply) || (replyStep ? (/ALLOW/.test(replyStep.text) ? "ALLOW" : "BLOCK") : null);
    const afterReply = replyStep ? st.slice(st.indexOf(replyStep) + 1) : [];
    const briefed = !!q || has("text");
    const expired = !!q && !q.open && !reply;
    const finished = reply ? afterReply.some((s) => s.kind === "cost" || s.kind === "report") && afterReply.some((s) => s.kind === "cost" || s.kind === "verdict")
      : !briefed && st.some((s) => s.kind === "cost");
    const refused = st.find((s) => s.kind === "refused");
    return { q, reply, briefed, expired, finished, report: has("report"), released: has("released"), refused: refused && refused.text,
      blocked: st.some((s) => /blocked for/.test(s.text || "")),
      text: (q && q.text) || (st.find((s) => s.kind === "text") || {}).text,
      phase: reply ? (finished ? "done" : "followup") : expired || finished ? "done" : briefed ? "you" : st.length || e.decision === "HOLD" ? "investigating" : "idle" };
  }
  const isOpenHold = (e) => e.decision === "HOLD" && ["investigating", "you"].includes(incident(e).phase);

  // ------------------------------------------------------------------ command log
  function renderRow(e) {
    const li = el("li", `ev ${e.decision}`);
    li.dataset.id = e.id;
    li.tabIndex = 0;
    const r = reasons(e);
    li.append(badge(e.decision, isTamper(e) ? "TAMPER" : undefined, "mini"), el("span", "what", describe(e).text), el("span", "time", fmtTime(e.ts)));
    li.append(el("span", "why", e.decision === "ALLOW" ? "Forwarded to the station" : r.main));
    li.addEventListener("click", () => select(e.id));
    li.addEventListener("keydown", (k) => { if (k.key === "Enter" || k.key === " ") { k.preventDefault(); select(e.id); } });
    return li;
  }
  function select(id, arrive) {
    if (selected === id) return updateCard();
    selected = id;
    for (const [k, li] of rows) li.classList.toggle("sel", k === id);
    showEvent(events.get(id), arrive);
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
    $("feed-count").textContent = `${events.size}, newest first`;
    // A held or denied command takes the centre. An allowed one does too, unless a held command is waiting there.
    const cur = selected && events.get(selected);
    if (!cur || (idNum(cur.id) < n && (e.decision !== "ALLOW" || !isOpenHold(cur)))) select(e.id, isNew && e.decision !== "ALLOW");
  });
  on("inbox", (list) => { inbox = list || []; updateCard(); renderPhase(); });

  // ------------------------------------------------------------------ the selected command
  const sig = {};
  const changed = (slot, value) => { const s = JSON.stringify(value); if (sig[slot] === s) return false; sig[slot] = s; return true; };
  function showEvent(e, arrive) {
    if (!e) return;
    for (const k of Object.keys(sig)) delete sig[k];
    $("idle").hidden = true; $("dc").hidden = false;
    $("dc").className = `dc ${e.decision}`;
    const r = reasons(e);
    $("dc-badge").replaceChildren(badge(e.decision, isTamper(e) ? "TAMPER" : undefined, "big"));
    $("dc-what").textContent = describe(e).text;
    $("dc-reason").textContent = e.decision === "ALLOW" ? "Checked and forwarded to the station." : r.main;
    const also = `From ${e.src_ip ?? "?"} at ${fmtTime(e.ts)} · ${e.id}${r.rest.length && e.decision !== "ALLOW" ? ` · Also: ${r.rest.join("; ")}` : ""}`;
    $("dc-also").textContent = also; $("dc-also").title = also;
    $("decision").classList.remove("arrive");
    if (arrive) { void $("decision").offsetWidth; $("decision").classList.add("arrive"); }
    updateCard();
    showForecast(e, arrive);
  }
  const STAGES = [["Held by gateway", "no AI"], ["Agent investigates", ""], ["Your decision", ""], ["Report", ""]];
  function updateCard() {
    const e = selected && events.get(selected);
    if (!e) return;
    const hold = e.decision === "HOLD", inc = incident(e), open = hold && isOpenHold(e);
    $("decision").classList.toggle("held", open);
    $("stages").hidden = !hold;
    if (hold && changed("stages", [inc.phase, inc.report, inc.reply])) {
      const at = { investigating: 1, you: 2, followup: 3, done: inc.report ? 4 : 3 }[inc.phase] ?? 1;
      $("stages").replaceChildren(...STAGES.map(([t, sub], i) => {
        const done = i < at || (i === 3 && inc.report), skipped = i === 3 && inc.phase === "done" && !inc.report;
        const li = el("li", done ? "done" : i === at && !skipped ? `active${i === 2 ? " you" : ""}` : "");
        const txt = el("span"); txt.append(el("b", "", t)); if (sub) txt.append(el("small", "", sub));
        li.append(el("span", "n", done ? "✓" : String(i + 1)), txt);
        return li;
      }));
    }
    // What the rig is doing right now, so the screen and the board tell the same story.
    const bank = state && (e.kind === "coil" ? state.coils : state.holding);
    const cur = bank ? bank[e.name] : undefined;
    const alarm = !!(state && state.coils && state.coils.ALARM);
    if (changed("rig", [e.id, e.decision, cur, alarm, inc.released])) {
      const d = describe(e), items = [el("li", "h", "On the rig")];
      const delivered = e.decision === "ALLOW" || inc.released;
      items.push(el("li", delivered ? "ok" : "", delivered ? "Command delivered" : "Command not delivered"));
      if (cur !== undefined) items.push(el("li", "", `${d.label} ${delivered ? "is" : "still"} ${d.f(cur)}`));
      if (e.decision !== "ALLOW") items.push(el("li", alarm ? "alarm" : "", alarm ? "Red LED and buzzer on" : "Alarm off"));
      $("rigline").replaceChildren(...items);
    }
    const brief = $("brief"), actions = $("actions");
    if (!hold) { brief.className = "brief"; brief.replaceChildren(); actions.replaceChildren(); return; }
    const last = stepsOf(e.id).map(plain).filter((p) => p && p.who === "agent").pop();
    if (changed("brief", [inc.phase, inc.text, !!inc.q, inc.phase === "investigating" && last && last.t])) {
      if (inc.phase === "investigating") {
        brief.className = "brief wait";
        const t = el("div"); t.append(el("b", "", "The agent is investigating"), el("span", "", `${last ? `${last.t}. ` : ""}Approve and Reject appear with its briefing. The command is already blocked.`));
        brief.replaceChildren(el("div", "spin"), t);
      } else if (inc.text) {
        brief.className = "brief";
        brief.replaceChildren(el("div", "lbl", "Agent briefing"), el("div", "txt", inc.text));
        if (inc.phase === "you" && inc.q && inc.q.covers && inc.q.covers.length) brief.append(el("div", "covers", `Approving would send: ${inc.q.covers.join("; ")}`));
      } else { brief.className = "brief"; brief.replaceChildren(); }
    }
    const err = replyErr.get(e.id) || "";
    if (changed("actions", [inc.phase, inc.reply, inc.report, inc.released, inc.refused, inc.expired, inc.blocked, err, inc.q && Math.ceil((inc.q.expires_in_s || 0) / 60)])) {
      actions.replaceChildren();
      if (inc.phase === "you" && !inc.q) actions.append(el("span", "note", "Loading the operator's question…"));
      else if (inc.phase === "you") {
        const reject = el("button", "reject", "Reject and block source"), approve = el("button", "approve", "Approve release");
        reject.addEventListener("click", () => answer(inc.q, "BLOCK"));
        approve.addEventListener("click", () => answer(inc.q, "ALLOW"));
        const note = el("span", "note", `Approval is re-checked against physics.${inc.q.code ? ` Code ${inc.q.code}.` : ""}`);
        if (err) note.replaceChildren(el("span", "err", err));
        actions.append(reject, approve, note);
      } else if (inc.phase !== "investigating") {
        const text = inc.reply === "BLOCK" ? `You rejected it. The command was never sent${inc.blocked ? ", and the source is blocked" : ""}.`
          : inc.reply === "ALLOW" ? (inc.refused ? `You approved it, but the release was refused: ${inc.refused}`
            : inc.released ? "You approved it. It passed the safety re-check and was sent to the station."
            : inc.finished ? "You approved it, but it was not released: it did not pass the safety re-check."
            : "You approved it. It is being re-checked against the physics model before any release.")
          : inc.expired ? "The question expired without a reply. Nothing was sent." : "";
        if (text) actions.append(el("div", "outcome", text));
        if (inc.report) { const a = el("a", "btn", "Open incident report"); a.href = reportHref(e.id); a.target = "_blank"; actions.append(a); }
      }
    }
  }
  const reportHref = (id) => `report.html?id=${encodeURIComponent(id)}${replay ? "&mode=replay" : ""}`;
  async function answer(q, reply) {
    $("actions").querySelectorAll("button").forEach((b) => (b.disabled = true));
    const err = await source.reply(q, reply);
    if (err) replyErr.set(q.event_id, err); else replyErr.delete(q.event_id);
    delete sig.actions;
    updateCard();
  }

  // ------------------------------------------------------------------ agent timeline
  const TOOL = {
    get_event: () => "Read the held command",
    get_recent_traffic: (a) => `Checked recent traffic from ${a.src_ip || "the source"}`,
    read_station: () => "Read the station's live state",
    run_physics: () => "Ran the physics model on the command",
    diff_logic_baseline: () => "Compared settings with the last known good copy",
    block_source: (a) => `Blocked source ${a.src_ip || ""}`.trim(),
    revert_to_last_known_good: () => "Reverted settings to last known good",
    enter_safe_mode: () => "Put the station in safe mode",
    release_held_command: () => "Asked the gateway to release the held command",
    task: () => "Handed its findings to the report writer",
    notify_operator: () => null,
  };
  const ACT = { block_source: "block", revert_to_last_known_good: "revert", enter_safe_mode: "safe" };
  // One agent step in plain words: { who, t, sub, quote, body, raw, rig, act } or null to leave it out.
  function plain(s) {
    const text = String(s.text ?? "").replace(/\*\*/g, ""), d = s.data || {};
    switch (s.kind) {
      case "status": return /^Incident opened/.test(text) ? { who: "gateway", t: "Held the command", sub: "Decided by policy and physics code, before any AI ran" } : { who: "agent", t: text };
      case "action":
        if (/^Station alarm raised/.test(text)) return { who: "gateway", t: "Raised the station alarm", sub: "Red LED and buzzer on the rig", rig: true };
        if (/blocked for/.test(text)) return { who: "agent", t: text.replace(/\s*\(.*\)$/, ""), act: "block" };
        if (/revert|restored/i.test(text)) return { who: "agent", t: text, act: "revert", rig: true };
        if (/safe mode/i.test(text)) return { who: "agent", t: text, act: "safe", rig: true };
        return { who: "agent", t: text };
      case "tool": {
        const f = TOOL[d.name];
        const t = f ? f(d.args || {}) : `Used ${d.name || "a tool"}`;
        return t ? { who: "agent", t, raw: text, tool: d.name, act: ACT[d.name] } : null;
      }
      case "result": return { result: true, tool: d.name, raw: text };
      case "text": return { who: "agent", t: "Briefed the operator", quote: text };
      case "verdict": return { who: "agent", t: "Assessment", body: text };
      case "reply": return { who: "you", t: /ALLOW/.test(text) ? "You approved the release" : "You rejected the command" };
      case "released": return { who: "gateway", t: "Released the command to the station", sub: "It passed the safety re-check", rig: true };
      case "refused": return { who: "gateway", t: "Refused the release", sub: text };
      case "report": return { who: "agent", t: "Wrote the incident report", report: true, act: "report" };
      case "cost": return null;
      default: return text ? { who: "agent", t: text } : null;
    }
  }
  const WHO = { gateway: "Gateway", agent: "Agent", you: "Operator" };
  const stepIds = new Set(), pending = [];   // pending: tool items still waiting for their result
  let lastStepEvent, lastItem = null;
  function physicsFinding(raw) {
    const code = /"code":\s*"(\w+)"/.exec(raw), safe = /"safe":\s*(true|false)/.exec(raw);
    if (safe && safe[1] === "true") return "Result: stays within limits";
    const words = { CHLORINE_HIGH: "chlorine would pass the 4 mg/L limit", TANK_OVERFLOW: "the tank would overflow", PRESSURE_LOSS: "the town would lose pressure", CHLORINE_LOW: "chlorine would fall too low" };
    return code ? `Result: unsafe, ${words[code[1]] || code[1]}` : null;
  }
  on("agent", (s) => {
    if (!s || stepIds.has(s.id)) return;
    stepIds.add(s.id);
    if (s.event_id) { if (!stepsBy.has(s.event_id)) stepsBy.set(s.event_id, []); stepsBy.get(s.event_id).push(s); }
    const p = plain(s), tl = $("tl");
    if (p && p.result) {                   // fold a tool's result into the step that called it
      const i = pending.findIndex((x) => x.tool === p.tool);
      if (i >= 0) {
        const { li } = pending.splice(i, 1)[0];
        li.querySelector("pre").textContent += `\n→ ${p.raw}`;
        const f = p.tool === "run_physics" && physicsFinding(p.raw);
        if (f) li.querySelector(".t").after(el("div", "sub", f));
      }
    } else if (p) {
      if (s.event_id !== lastStepEvent) {
        const e = events.get(s.event_id);
        tl.append(el("li", "inc", `${s.event_id ?? "Incident"}${e ? ` · ${describe(e).text}` : ""}`));
        lastStepEvent = s.event_id;
        document.querySelectorAll(".act").forEach((a) => a.classList.remove("used"));
      }
      const li = el("li", `it ${p.who} new`);
      const who = el("div", "who", WHO[p.who]);
      who.append(document.createTextNode(` · ${fmtTime(s.ts)}`));
      if (p.rig) who.append(el("span", "tag", "ON THE RIG"));
      li.append(who, el("div", "t", p.t));
      if (p.sub) li.append(el("div", "sub", p.sub));
      if (p.quote) { const det = el("details"); det.append(el("summary", "", "Read the briefing"), el("div", "quote", p.quote)); li.append(det); }
      if (p.body) {
        const det = el("details"); det.append(el("summary", "", "Read the assessment"), el("div", "body", p.body)); li.append(det);
      }
      if (p.raw) {
        const det = el("details"); det.append(el("summary", "", "Raw tool call"), el("pre", "", p.raw)); li.append(det);
        pending.push({ tool: p.tool, li });
      }
      if (p.report && s.event_id) { const a = el("a", "rep", "Open incident report"); a.href = reportHref(s.event_id); a.target = "_blank"; li.append(a); }
      if (p.act) { const chip = document.querySelector(`.act[data-act="${p.act}"]`); if (chip) chip.classList.add("used"); }
      tl.append(li);
      if (lastItem) lastItem.classList.remove("live");
      lastItem = li;
      $("tl-empty").hidden = true;
      const box = $("agent-scroll");
      box.scrollTop = box.scrollHeight;
    }
    renderPhase();
    if (s.event_id === selected) updateCard();
  });
  function renderPhase() {
    const e = lastStepEvent && events.get(lastStepEvent), ph = $("phase");
    const phase = e ? incident(e).phase : lastStepEvent ? "investigating" : "idle";
    const [text, cls] = { idle: ["Idle", ""], investigating: ["Investigating", "busy"], you: ["Waiting for your decision", "you"], followup: ["Following up", "busy"], done: ["Done", ""] }[phase];
    ph.textContent = text; ph.className = `phase ${cls}`;
    if (lastItem) lastItem.classList.toggle("live", cls === "busy");
  }

  on("reset", () => {
    events.clear(); rows.clear(); stepIds.clear(); stepsBy.clear(); replyErr.clear(); pending.length = 0;
    inbox = []; selected = null; lastStepEvent = undefined; lastItem = null; state = null;
    for (const k of Object.keys(sig)) delete sig[k];
    feed.replaceChildren(); $("tl").replaceChildren();
    $("feed-empty").hidden = $("tl-empty").hidden = false;
    $("feed-count").textContent = "newest first";
    $("idle").hidden = false; $("dc").hidden = true; $("decision").classList.remove("held", "arrive");
    document.querySelectorAll(".act").forEach((a) => a.classList.remove("used"));
    renderPhase(); clearChart();
  });

  // ------------------------------------------------------------------ forecast chart
  const chartBox = $("chart"), tip = $("tip");
  const SERIES = { CHLORINE_HIGH: 2, CHLORINE_LOW: 2, TANK_OVERFLOW: 1, PRESSURE_LOSS: 1, PUMP_SHORT_CYCLE: 1 };
  const UNIT = { 1: ["Tank level", "%", 1], 2: ["Chlorine", "mg/L", 2] };
  const CL_REGS = ["DOSER_RATE_PCT", "DOSER_CMD", "CHLORINE_TARGET"];
  let token = 0, drawn = null;
  function clearChart() { chartBox.querySelectorAll("svg").forEach((n) => n.remove()); tip.hidden = true; drawn = null; }
  async function showForecast(e, animate) {
    const mine = ++token;
    clearChart();
    let full;
    try { full = await source.getEvent(e.id); } catch (err) { return; }
    if (mine !== token) return;
    const p = full.prediction, traj = p && p.trajectory;
    if (!traj || traj.length < 2) return;
    const v = (p.violations || []).find((x) => !x.pre_existing && SERIES[x.code]);
    drawn = { traj, idx: v ? SERIES[v.code] : CL_REGS.includes(e.name) ? 2 : 1, v };
    draw(animate);
  }
  function draw(animate) {
    if (!drawn) return;
    const { traj, idx, v } = drawn, [name, unit, dp] = UNIT[idx];
    const r = chartBox.getBoundingClientRect();
    const W = Math.max(380, Math.round(r.width)), H = Math.max(100, Math.round(r.height));
    const L = 46, R = 14, Tp = 22, B = 36;
    const step = Math.max(1, Math.floor(traj.length / 300));
    const pts = traj.filter((_, i) => i % step === 0 || i === traj.length - 1);
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[idx]);
    const limit = v && typeof v.limit === "number" ? v.limit : null;
    const all = ys.concat(limit === null ? [] : [limit]);
    let y0 = Math.min(...all), y1 = Math.max(...all);
    const pad = (y1 - y0 || 1) * 0.2; y0 = Math.max(0, y0 - pad); y1 += pad;
    const x1 = xs[xs.length - 1] || 1;
    const X = (t) => L + (W - L - R) * (t / x1), Y = (y) => Tp + (H - Tp - B) * (1 - (y - y0) / (y1 - y0));
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": `${name} forecast over ${Math.round(x1)} minutes${v ? `, crossing the limit of ${limit} ${unit}` : ", inside the limits"}` });
    for (let i = 0; i <= 3; i++) {
      const y = y0 + (y1 - y0) * i / 3;
      s.append(svg("line", { x1: L, x2: W - R, y1: Y(y), y2: Y(y), class: "gridl" }));
      s.append(svg("text", { x: L - 8, y: Y(y) + 4, class: "tick", "text-anchor": "end" }, y.toFixed(dp === 2 ? 1 : 0)));
    }
    for (let i = 0; i <= 4; i++) {
      const t = x1 * i / 4;
      s.append(svg("text", { x: X(t), y: H - B + 16, class: "tick", "text-anchor": i === 4 ? "end" : i ? "middle" : "start" }, `${Math.round(t)}`));
    }
    s.append(svg("line", { x1: L, x2: W - R, y1: H - B, y2: H - B, class: "axis" }));
    s.append(svg("text", { x: (L + W - R) / 2, y: H - 4, class: "tick", "text-anchor": "middle" }, "minutes of plant time after the command"));
    s.append(svg("text", { x: L, y: 13, class: "tick" }, `Forecast: ${name.toLowerCase()} (${unit})${v ? " if the command had been sent" : ""}`));
    const lowSide = v && (v.code === "CHLORINE_LOW" || v.code === "PRESSURE_LOSS");
    if (limit !== null) {
      const top = lowSide ? Y(limit) : Tp, hgt = lowSide ? H - B - Y(limit) : Y(limit) - Tp;
      if (hgt > 0) s.append(svg("rect", { x: L, y: top, width: W - L - R, height: hgt, class: "over" }));
      s.append(svg("line", { x1: L, x2: W - R, y1: Y(limit), y2: Y(limit), class: "limit" }));
      s.append(svg("text", { x: L + 8, y: Y(limit) + (lowSide ? 17 : -6), class: "lbl red" }, `Safe limit ${limit} ${unit}`));
    }
    s.append(svg("polyline", { points: pts.map((p) => `${X(Math.min(p[0], x1)).toFixed(1)},${Y(p[idx]).toFixed(1)}`).join(" "), class: `line${animate ? " draw" : ""}` }));
    const crossed = limit !== null && ys.some((y) => (lowSide ? y <= limit : y >= limit));
    if (v && crossed && typeof v.minutes_until === "number" && v.minutes_until <= x1) {
      const cx = X(v.minutes_until), cy = Y(limit);
      s.append(svg("line", { x1: cx, x2: cx, y1: cy, y2: H - B, class: "limit" }));
      s.append(svg("circle", { cx, cy, r: 7, class: "cross" }));
      s.append(svg("text", { x: Math.min(cx + 11, W - R - 150), y: H - B - 9, class: "lbl red" }, `crosses at ${mins(v.minutes_until)}`));
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
    chartBox.querySelectorAll("svg").forEach((n) => n.remove());
    chartBox.append(s);
  }
  // The chart is drawn to the size of its box; redraw when the box changes (window, or the briefing arriving).
  let resizeTimer, drawnFor = "";
  new ResizeObserver(() => {
    const r = chartBox.getBoundingClientRect(), key = `${Math.round(r.width)}x${Math.round(r.height)}`;
    if (key === drawnFor) return;
    drawnFor = key;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => draw(false), 120);
  }).observe(chartBox);

  window.Backflow.chart = { SERIES, UNIT };
  source.start();
})();
