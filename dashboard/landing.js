// Landing page motion: water flowing behind the hero. Plain canvas, no library.
// Stops when the hero is off screen or the tab is hidden, and does not run at all under prefers-reduced-motion.
"use strict";
(() => {
  const canvas = document.getElementById("flow"), ctx = canvas.getContext("2d");
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let w = 0, h = 0, parts = [], visible = true, raf = 0, last = 0;

  function size() {
    const r = canvas.getBoundingClientRect(), dpr = Math.min(devicePixelRatio || 1, 2);
    w = r.width; h = r.height;
    canvas.width = w * dpr; canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const n = Math.round(Math.min(130, w / 11));
    parts = Array.from({ length: n }, () => ({
      x: Math.random() * w, lane: Math.random(), speed: 22 + Math.random() * 60,
      amp: 8 + Math.random() * 26, phase: Math.random() * 6.28, r: 1 + Math.random() * 2.2, a: .12 + Math.random() * .38 }));
  }
  function frame(t) {
    const dt = Math.min(.05, (t - last) / 1000 || 0);
    last = t;
    ctx.clearRect(0, 0, w, h);
    for (const p of parts) {
      p.x += p.speed * dt;
      if (p.x > w + 20) { p.x = -20; p.lane = Math.random(); }
      // streams bend towards the middle of the page, like water drawn into one pipe
      const pull = Math.sin((p.x / w) * Math.PI);
      const y = h * (0.12 + 0.76 * (p.lane + (0.5 - p.lane) * 0.45 * pull)) + Math.sin(p.x / 140 + p.phase) * p.amp;
      ctx.globalAlpha = p.a;
      ctx.fillStyle = "#fff";
      ctx.beginPath();
      ctx.ellipse(p.x, y, p.r * 5, p.r * .8, 0, 0, 6.2832);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
    if (visible && !still) raf = requestAnimationFrame(frame);
  }
  const run = () => { cancelAnimationFrame(raf); last = performance.now(); raf = requestAnimationFrame(frame); };
  size();
  addEventListener("resize", () => { size(); if (still) frame(0); });
  if (still) { frame(0); return; }
  new IntersectionObserver(([e]) => { visible = e.isIntersecting && !document.hidden; if (visible) run(); }).observe(canvas);
  document.addEventListener("visibilitychange", () => { visible = !document.hidden; if (visible) run(); });
  run();
})();
