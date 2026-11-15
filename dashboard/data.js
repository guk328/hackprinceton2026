// Backflow console data layer. The console never talks to the network itself; it asks a source.
//   live   : the gateway's HTTP API (GET /events, /state, /agent/log, /operator/inbox, SSE /stream)
//   replay : a recorded run (replay/scenario_0_1.js, made by tools/record_replay.py) played on a timer
// Both emit the same messages, so the console draws them the same way:
//   "event" (event, isNew)   "agent" (step)   "state" (snapshot | {unreachable:true})
//   "inbox" (questions)      "conn" ("live"|"reconnecting")   "reset" ()   "player" (status)
// The only thing this file ever POSTs is /operator/reply, /reset and /logout. It has no way to
// send a command to the station.
"use strict";
(() => {
  const handlers = {};
  const on = (k, f) => { (handlers[k] = handlers[k] || []).push(f); };
  const emit = (k, ...a) => (handlers[k] || []).forEach((f) => { try { f(...a); } catch (e) { console.error(e); } });

  async function getJSON(url) {
    const r = await fetch(url, { credentials: "same-origin" });
    if (!r.ok) { const e = new Error(`${url}: ${r.status}`); e.status = r.status; throw e; }
    return r.json();
  }

  // ------------------------------------------------------------------ live
  function liveSource() {
    let admin = false, inboxBusy = false;
    async function loadInbox() {
      if (inboxBusy || !admin) return;
      inboxBusy = true;
      try { emit("inbox", await getJSON("/operator/inbox")); }
      catch (e) { if (e.status === 401) { admin = false; emit("auth", false); } }
      finally { inboxBusy = false; }
    }
    async function pollState() {
      try { emit("state", await getJSON("/state")); } catch (e) { emit("state", { unreachable: true }); }
    }
    async function backfill() {
      try { for (const e of (await getJSON("/events?limit=200")).reverse()) emit("event", e, false); } catch (e) { console.warn(e); }
      try { for (const s of await getJSON("/agent/log?limit=300")) emit("agent", s); } catch (e) { console.warn(e); }
      loadInbox();
    }
    return {
      mode: "live",
      now: () => Date.now() / 1000,
      async start() {
        try { admin = (await getJSON("/me")).admin === true; }
        catch (e) { emit("auth", null); return; }          // gateway not reachable at all
        emit("auth", admin);
        if (!admin) return;
        const es = new EventSource("/stream");
        es.addEventListener("event", (m) => { try { emit("event", JSON.parse(m.data), true); } catch (e) { console.warn(e); } });
        es.addEventListener("agent", (m) => {
          try {
            const s = JSON.parse(m.data);
            emit("agent", s);
            if (["text", "reply", "released", "refused", "action"].includes(s.kind)) loadInbox();
          } catch (e) { console.warn(e); }
        });
        es.onopen = () => { emit("conn", "live"); backfill(); };
        es.onerror = () => emit("conn", "reconnecting");
        pollState();
        setInterval(pollState, 1000);
        setInterval(loadInbox, 3000);
      },
      getEvent: (id) => getJSON(`/events/${encodeURIComponent(id)}`),
      async getReport(id) {
        const r = await fetch(`/reports/${encodeURIComponent(id)}`);
        if (!r.ok) throw new Error(`no report (${r.status})`);
        return r.text();
      },
      async reply(q, answer) {
        try {
          const r = await fetch("/operator/reply", {
            method: "POST", credentials: "same-origin", headers: { "content-type": "application/json" },
            body: JSON.stringify({ reply: answer, event_id: q.event_id, code: q.code }) });
          if (r.status === 401) return "Sign in again to reply.";
          if (!r.ok) return `Not accepted: ${(await r.json().catch(() => ({}))).detail || r.status}`;
          return "";
        } catch (e) { return "Could not reach the gateway."; }
        finally { loadInbox(); }
      },
      async reset() { return (await fetch("/reset", { method: "POST", credentials: "same-origin" })).ok; },
      async logout() { try { await fetch("/logout", { method: "POST", credentials: "same-origin" }); } catch (e) { /* ignore */ } },
    };
  }

  // ------------------------------------------------------------------ replay
  // On the public site, or with ?loop=1, the replay runs unattended: at the operator's question it waits
  // AUTO_REPLY_MS for someone to press the recorded answer, then continues by itself, and it starts over
  // RESTART_MS after the end. Locally, ?mode=replay waits for the presenter as before.
  const AUTO_REPLY_MS = 8000, RESTART_MS = 10000;
  function replaySource(auto) {
    let rec = null, idx = 0, t = 0, speed = 1, playing = false, waiting = null, last = 0, restartAt = 0;
    const secs = (at) => Math.max(0, Math.ceil((at - performance.now()) / 1000));
    const status = () => ({ t, total: rec ? rec.meta.duration_s : 0, speed, playing, waiting,
                            autoIn: waiting && waiting.autoAt ? secs(waiting.autoAt) : null,
                            restartIn: restartAt ? secs(restartAt) : null,
                            done: !!rec && idx >= rec.timeline.length, meta: rec && rec.meta });
    const recordedReply = (eventId) => {
      for (const item of rec.timeline) {
        if (item.k !== "inbox") continue;
        const q = item.d.find((x) => x.event_id === eventId && x.reply);
        if (q) return q.reply;
      }
      return null;
    };
    function dispatch(item) {
      if (item.k === "event") emit("event", item.d, true);
      else if (item.k === "inbox") {
        emit("inbox", item.d);
        // Stop at an open question and let the presenter press the button the recorded operator pressed.
        const open = item.d.find((q) => q.open && !q.reply && recordedReply(q.event_id));
        if (open) waiting = { event_id: open.event_id, reply: recordedReply(open.event_id) };
      } else emit(item.k, item.d);
    }
    function tick() {
      const now = performance.now(), dt = (now - last) / 1000;
      last = now;
      if (!rec) return;
      if (auto && waiting && playing) {
        if (!waiting.autoAt) waiting.autoAt = now + AUTO_REPLY_MS;
        if (now >= waiting.autoAt) waiting = null;          // nobody pressed it: carry on as the recorded operator did
        emit("player", status());
        if (waiting) return;
      }
      if (auto && !playing && idx >= rec.timeline.length) {
        if (!restartAt) restartAt = now + RESTART_MS;
        if (now >= restartAt) src.restart(); else emit("player", status());
        return;
      }
      if (!playing || waiting) return;
      t += dt * speed;
      while (idx < rec.timeline.length && rec.timeline[idx].t <= t && !waiting) dispatch(rec.timeline[idx++]);
      if (waiting) t = rec.timeline[idx - 1].t;
      if (idx >= rec.timeline.length) playing = false;
      emit("player", status());
    }
    const src = {
      mode: "replay",
      now: () => (rec ? rec.meta.t0 + t : 0),
      start() {
        const s = document.createElement("script");      // a script, not fetch: also works from file://
        s.src = "replay/scenario_0_1.js";
        s.onload = () => {
          rec = window.BACKFLOW_REPLAY;
          if (!rec) { emit("auth", null); return; }
          emit("conn", "replay");
          src.restart();
          setInterval(tick, 100);
        };
        s.onerror = () => emit("player", { missing: true });
        document.head.append(s);
      },
      restart() { idx = 0; t = 0; waiting = null; restartAt = 0; playing = true; last = performance.now(); emit("reset"); emit("player", status()); },
      toggle() { if (idx >= rec.timeline.length) return src.restart(); playing = !playing; emit("player", status()); },
      setSpeed(x) { speed = x; emit("player", status()); },
      getEvent: async (id) => { if (rec.events[id]) return rec.events[id]; throw new Error("not in the recording"); },
      getReport: async (id) => { if (rec.reports[id]) return rec.reports[id]; throw new Error("no report in the recording"); },
      async reply(q, answer) {
        if (!waiting || waiting.event_id !== q.event_id) return "The recording has already moved on.";
        if (answer !== waiting.reply) return `The recorded operator chose ${waiting.reply === "BLOCK" ? "Reject" : "Approve"}. A replay can only follow the recording.`;
        waiting = null; last = performance.now();
        emit("player", status());
        return "";
      },
      recordedReply: (id) => (rec ? recordedReply(id) : null),
      async reset() { src.restart(); return true; },
      async logout() {},
    };
    return src;
  }

  // The public site (backflow.design on GitHub Pages) has no gateway behind it: it always plays the recording.
  const hosted = /(^|\.)backflow\.design$|\.github\.io$/.test(location.hostname);
  const params = new URLSearchParams(location.search);
  const mode = hosted || params.get("mode") === "replay" || location.protocol === "file:" ? "replay" : "live";
  const source = mode === "replay" ? replaySource(hosted || params.get("loop") === "1") : liveSource();
  window.Backflow = { on, source, getJSON, replaySource, hosted };
})();
