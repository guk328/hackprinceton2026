// Plant view (centre column): live station state from GET /state, and for the selected
// command the physics model's forecast from GET /events/{id} (the full trajectory).
// Drawn as inline SVG: no chart library, nothing loaded from the internet.
"use strict";
(() => {
  const { el, getJSON, listeners } = window.backflow;
  const NS = "http://www.w3.org/2000/svg";
  const svg = (tag, attrs = {}, text) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const col = document.getElementById("col-plant");
  col.hidden = false;
  document.querySelector("main").classList.add("three");

  // ---------------------------------------------------------------- live tiles
  col.append(el("h2", "", "Plant now"));
  const tiles = el("div", "tiles");
  col.append(tiles);
  const tile = (label) => {
    const t = el("div", "tile");
    const v = el("div", "v", "–"), s = el("div", "s", "");
    t.append(el("div", "l", label), v, s);
    tiles.append(t);
    return { t, v, s };
  };
  const tank = el("div", "tile tank");
  const bar = el("div", "bar"), fill = el("div", "fill"), hi = el("div", "sp"), lo = el("div", "sp");
  bar.append(fill, hi, lo);
  const tankV = el("div", "v", "–"), tankS = el("div", "s", "");
  const tankText = el("div", "");
  tankText.append(el("div", "l", "Tank level"), tankV, tankS);
  tank.append(bar, tankText);
  tiles.append(tank);
  const T = { cl: tile("Chlorine"), pump: tile("Pump"), doser: tile("Doser"), site: tile("On site"),
              mode: tile("Mode") };

  const onoff = (x, v, on, text, sub = "") => {
    x.v.textContent = text; x.s.textContent = sub;
    x.t.classList.toggle("on", !!on); x.t.classList.toggle("warn", v === "warn");
  };

  async function pollState() {
    let s;
    try { s = await getJSON("/state"); } catch (e) { s = { connected: false }; }
    col.classList.toggle("offline", !s.connected);
    if (!s.holding) { tankS.textContent = "station not connected"; return; }
    const h = s.holding, c = s.coils, di = s.discrete_inputs, level = h.TANK_LEVEL_PCT / 10;
    tankV.textContent = `${level.toFixed(1)} %`;
    tankS.textContent = s.connected ? `pump runs between ${h.LEVEL_LOW_SP / 10} and ${h.LEVEL_HIGH_SP / 10} %`
                                    : "station not connected, last known values";
    fill.style.height = `${Math.max(0, Math.min(100, level))}%`;
    hi.style.bottom = `${h.LEVEL_HIGH_SP / 10}%`; lo.style.bottom = `${h.LEVEL_LOW_SP / 10}%`;
    const cl = h.CHLORINE_MGL / 100;
    onoff(T.cl, cl >= 4 ? "warn" : "", false, `${cl.toFixed(2)} mg/L`, "alarm line 4.0 mg/L");
    onoff(T.pump, "", di.PUMP_RUNNING, di.PUMP_RUNNING ? "RUNNING" : "OFF", c.AUTO_MODE ? "automatic" : "manual");
    onoff(T.doser, "", di.DOSER_RUNNING, di.DOSER_RUNNING ? "DOSING" : "OFF", `rate ${h.DOSER_RATE_PCT} %`);
    const site = di.PRESENCE || di.CABINET_OPEN;
    onoff(T.site, "", site, site ? "SOMEONE" : "NOBODY",
          `${di.PRESENCE ? "motion" : "no motion"} · cabinet ${di.CABINET_OPEN ? "open" : "closed"}`);
    onoff(T.mode, c.SAFE_MODE || c.ALARM ? "warn" : "", false, c.SAFE_MODE ? "SAFE MODE" : "NORMAL",
          c.ALARM ? "alarm sounding" : `demand ${(s.input_registers.DEMAND_PCT / 10).toFixed(0)} %`);
  }

  // ---------------------------------------------------------------- forecast chart
  const head = el("h2", "", "What the command would do");
  head.style.marginTop = "18px";
  const title = el("div", "chart-title", ""), sub = el("div", "chart-sub", "Select a command on the left.");
  const box = el("div", "chart");
  const tip = el("div", "tip"); tip.hidden = true;
  col.append(head, title, sub, box, tip);
  const SERIES = {   // which line tells the story for each violation
    CHLORINE_HIGH: 2, CHLORINE_LOW: 2, TANK_OVERFLOW: 1, PRESSURE_LOSS: 1, PUMP_SHORT_CYCLE: 1 };
  const UNIT = { 1: ["Tank level", "%", 1], 2: ["Chlorine", "mg/L", 2] };
  let token = 0;

  async function show(e) {
    const mine = ++token;
    box.replaceChildren(); tip.hidden = true;
    title.textContent = `${e.id}: ${e.name ?? "command"} ${e.old_value ?? ""} → ${e.new_value ?? ""}`;
    sub.textContent = "Loading the forecast…";
    let full;
    try { full = await getJSON(`/events/${encodeURIComponent(e.id)}`); } catch (err) { sub.textContent = "Forecast not available."; return; }
    if (mine !== token) return;
    const p = full.prediction, traj = p && p.trajectory;
    if (!traj || traj.length < 2) { sub.textContent = (full.reasons && full.reasons[0]) || "No forecast was needed for this command."; return; }
    const v = (p.violations || []).find((x) => !x.pre_existing && SERIES[x.code]);
    const idx = v ? SERIES[v.code] : 2, [name, unit, dp] = UNIT[idx];
    sub.textContent = v ? `${v.message}. The gateway held it; this did not happen.`
                        : `Model forecast: stays within limits (${full.decision}).`;
    draw(traj, idx, name, unit, dp, v);
  }

  function draw(traj, idx, name, unit, dp, v) {
    const W = 640, H = 300, L = 54, R = 16, Tp = 18, B = 38;
    const step = Math.max(1, Math.floor(traj.length / 320));
    const pts = traj.filter((_, i) => i % step === 0 || i === traj.length - 1);
    const xs = pts.map((r) => r[0]), ys = pts.map((r) => r[idx]);
    const limit = v && typeof v.limit === "number" ? v.limit : null;
    let y0 = Math.min(...ys, limit ?? Infinity), y1 = Math.max(...ys, limit ?? -Infinity);
    const pad = (y1 - y0 || 1) * 0.15; y0 = Math.max(0, y0 - pad); y1 += pad;
    const x1 = xs[xs.length - 1] || 1;
    const X = (t) => L + (W - L - R) * (t / x1), Y = (y) => Tp + (H - Tp - B) * (1 - (y - y0) / (y1 - y0));
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
                           "aria-label": `${name} forecast over ${Math.round(x1)} minutes` });
    for (let i = 0; i <= 4; i++) {
      const y = y0 + (y1 - y0) * i / 4;
      s.append(svg("line", { x1: L, x2: W - R, y1: Y(y), y2: Y(y), class: "grid" }));
      s.append(svg("text", { x: L - 8, y: Y(y) + 4, class: "tick", "text-anchor": "end" }, y.toFixed(dp === 2 ? 1 : 0)));
    }
    for (let i = 0; i <= 4; i++) {
      const t = x1 * i / 4;
      s.append(svg("text", { x: X(t), y: H - B + 18, class: "tick", "text-anchor": "middle" }, `${Math.round(t)}`));
    }
    s.append(svg("text", { x: (L + W - R) / 2, y: H - 4, class: "tick", "text-anchor": "middle" }, "plant-minutes from the command"));
    s.append(svg("text", { x: L, y: 12, class: "tick" }, `${name} (${unit})`));
    if (limit !== null) {
      s.append(svg("line", { x1: L, x2: W - R, y1: Y(limit), y2: Y(limit), class: "limit" }));
      s.append(svg("text", { x: W - R - 4, y: Y(limit) - 6, class: "limit-label", "text-anchor": "end" },
                   `▲ limit ${limit} ${unit}`));
    }
    s.append(svg("polyline", { points: pts.map((r) => `${X(r[0]).toFixed(1)},${Y(r[idx]).toFixed(1)}`).join(" "), class: "line" }));
    if (v && typeof v.minutes_until === "number" && v.minutes_until <= x1) {
      const cx = X(v.minutes_until), cy = Y(limit ?? v.value);
      s.append(svg("circle", { cx, cy, r: 6, class: "cross" }));
      s.append(svg("text", { x: Math.min(cx + 10, W - R - 150), y: cy + 20, class: "cross-label" },
                   `crosses in ${Math.round(v.minutes_until)} min`));
    }
    const hair = svg("line", { y1: Tp, y2: H - B, class: "hair", visibility: "hidden" });
    const dot = svg("circle", { r: 5, class: "dot", visibility: "hidden" });
    const hit = svg("rect", { x: L, y: Tp, width: W - L - R, height: H - Tp - B, fill: "transparent" });
    s.append(hair, dot, hit);
    hit.addEventListener("mousemove", (ev) => {
      const r = s.getBoundingClientRect(), t = ((ev.clientX - r.left) / r.width * W - L) / (W - L - R) * x1;
      let k = 0;
      for (let i = 1; i < xs.length; i++) if (Math.abs(xs[i] - t) < Math.abs(xs[k] - t)) k = i;
      hair.setAttribute("x1", X(xs[k])); hair.setAttribute("x2", X(xs[k]));
      dot.setAttribute("cx", X(xs[k])); dot.setAttribute("cy", Y(ys[k]));
      hair.setAttribute("visibility", "visible"); dot.setAttribute("visibility", "visible");
      tip.textContent = `${Math.round(xs[k])} min: ${ys[k].toFixed(dp)} ${unit}`;
      tip.hidden = false;
    });
    hit.addEventListener("mouseleave", () => {
      hair.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true;
    });
    box.replaceChildren(s);
  }

  listeners.select.push(show);
  pollState();
  setInterval(pollState, 1000);
})();
