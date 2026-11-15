// Landing page motion: a braille dot field behind the hero. Plain canvas, no library.
// Dots sit in 2 x 4 braille cells. A slow flow field decides which dots are raised, inside a ring that
// leaves the headline clear, so patterns drift across the sides like water passing. The pointer lifts
// the dots near it. Stops when the hero is off screen or the tab is hidden; one still frame under
// prefers-reduced-motion.
"use strict";

// On the public site (backflow.design, GitHub Pages) there is no gateway: point sign-in at the recording.
(() => {
  if (!/(^|\.)backflow\.design$|\.github\.io$/.test(location.hostname)) return;
  const cta = document.getElementById("nav-cta");
  if (cta) { cta.textContent = "Watch the demo"; cta.href = "console.html?mode=replay"; }
  document.querySelectorAll(".live-only").forEach((el) => el.remove());
})();

(() => {
  const canvas = document.getElementById("dots");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const P = 6, GAP = 6, CW = 2 * P + GAP, CH = 4 * P + GAP;   // dot pitch, gap between cells, cell size (CSS px)
  const LEVELS = 7, SIZE = 1.7;
  let w = 0, h = 0, dots = new Float32Array(0), n = 0, visible = true, raf = 0, last = 0, t0 = performance.now();
  let px = -1e4, py = -1e4, lift = 0;

  // Distance from the middle of the headline, stretched sideways so the rings are wide ellipses.
  const cxOf = () => w / 2, cyOf = () => h * 0.4;
  const dist = (x, y) => Math.hypot((x - cxOf()) * 0.62, (y - cyOf()) * 0.95);
  // Where dots may appear: clear behind the headline, rising towards the sides, gone above the white floor.
  function mask(x, y) {
    const d = dist(x, y) / Math.max(220, Math.min(w, 1800) * 0.32);
    const inner = Math.min(1, Math.max(0, (d - 0.45) / 0.4));
    const floor = Math.min(1, Math.max(0, (h * 0.78 - y) / (h * 0.3)));
    return inner * inner * floor;
  }
  // Ripples spreading out from the headline, bent by a slow swirl, so they read as arcs of water.
  const flow = (x, y, d, t) => {
    const a = Math.atan2(y - cyOf(), x - cxOf());
    const ripple = Math.sin(d * 0.034 - t * 0.9 + 1.6 * Math.sin(a * 3 + t * 0.15) + Math.sin(y * 0.01 + t * 0.3));
    const swell = Math.sin(x * 0.006 - t * 0.35) * Math.sin(y * 0.009 + t * 0.25);
    return 0.5 + 0.38 * ripple + 0.12 * swell;
  };

  function size() {
    const r = canvas.getBoundingClientRect(), dpr = Math.min(devicePixelRatio || 1, 2);
    w = r.width; h = r.height;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const cols = Math.ceil(w / CW) + 1, rows = Math.ceil(h / CH) + 1;
    const ox = (w - (cols * CW - GAP)) / 2, oy = 4;
    const out = [];
    for (let cy = 0; cy < rows; cy++) for (let cx = 0; cx < cols; cx++) for (let k = 0; k < 8; k++) {
      const x = ox + cx * CW + (k & 1) * P, y = oy + cy * CH + (k >> 1) * P;
      const m = mask(x, y);
      if (m > 0.03) out.push(x, y, m, Math.random(), dist(x, y));
    }
    dots = new Float32Array(out); n = dots.length;
  }

  function draw(now) {
    const t = (now - t0) / 1000;
    lift *= 0.96;
    ctx.clearRect(0, 0, w, h);
    const paths = Array.from({ length: LEVELS }, () => new Path2D());
    for (let i = 0; i < n; i += 5) {
      const x = dots[i], y = dots[i + 1], m = dots[i + 2], r = dots[i + 3];
      const f = flow(x, y, dots[i + 4], t);
      let s = m * f * f * 1.25;
      if (lift > 0.01) { const dx = x - px, dy = y - py; s += lift * 0.8 * Math.exp(-(dx * dx + dy * dy) / 7000); }
      const a = (s - (0.12 + 0.75 * r)) * 3.5;              // each dot has its own threshold, so cells fill unevenly
      if (a <= 0) continue;
      paths[Math.min(LEVELS - 1, Math.floor(a * LEVELS))].rect(x - SIZE / 2, y - SIZE / 2, SIZE, SIZE);
    }
    ctx.fillStyle = "#d8f4ff";
    for (let l = 0; l < LEVELS; l++) { ctx.globalAlpha = 0.18 + 0.75 * ((l + 1) / LEVELS); ctx.fill(paths[l]); }
    ctx.globalAlpha = 1;
  }

  function frame(now) {
    if (now - last >= 33) { last = now; draw(now); }       // 30 fps is plenty for drifting dots
    if (visible) raf = requestAnimationFrame(frame);
  }
  const run = () => { cancelAnimationFrame(raf); if (visible) raf = requestAnimationFrame(frame); };

  size();
  addEventListener("resize", () => { size(); draw(performance.now()); });
  if (still) { draw(t0 + 4000); return; }
  addEventListener("pointermove", (e) => {
    const r = canvas.getBoundingClientRect();
    px = e.clientX - r.left; py = e.clientY - r.top; lift = 1;
  }, { passive: true });
  new IntersectionObserver(([e]) => { visible = e.isIntersecting && !document.hidden; run(); }).observe(canvas);
  document.addEventListener("visibilitychange", () => { visible = !document.hidden; run(); });
  run();
})();
