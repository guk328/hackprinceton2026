# PLAN_COMPE: gateway, agent, dashboard

Run one step per session: "I'm the CompE. Do step N in docs/plans/PLAN_COMPE.md."
Spec: `docs/prompts/PROMPT_COMPE.md`. Contract: `README.md` section 4, `shared/registers.yaml`.
Each step is 30–90 min. Tick the box when its proof passes. Model: **fable** = careful
design or tricky debugging, **sonnet** = boilerplate.

## Pinned versions (`requirements.txt`)

Latest on PyPI per `pip index versions`, checked Fri 13 Nov. Installed on Python 3.14.3 and import-tested (step 1).

```
pymodbus==3.16.2
pyyaml==6.0.3
python-dotenv==1.2.4
fastapi==0.162.7
uvicorn==0.54.0
httpx==0.28.1                  # FastAPI TestClient
deepagents==0.7.38
langgraph==1.2.22
langchain==1.5.0
langchain-anthropic==1.7.10    # swap for langchain-openai==1.8.0 if we pick that provider
pytest==9.2.0
pytest-asyncio==1.5.0
```

- SQLite uses the stdlib `sqlite3` and SSE uses FastAPI's `StreamingResponse`, so neither adds a dependency.
- This laptop runs Python 3.14.3. If step 1's install fails on a wheel, make the venv with 3.12.
- Windows has no `make`. Step 1 adds `tasks.py` (stdlib argparse, same on Windows and Mac): `python tasks.py sim|gateway|test|demo`.

## Steps

**Build 1: software station (Fri 11pm milestone: steps 1-3 done, station running)**
- [x] **1. Venv, requirements, task runner.** Files: `requirements.txt`, `tasks.py`, `.gitattributes`, `README.md` (Run it), `sim/__init__.py`, `gateway/__init__.py`.
  Proof: `pip install -r requirements.txt` succeeds; `python -c "import pymodbus, deepagents, fastapi"`; `python tasks.py --help`. Model: sonnet
- [x] **2. Register map loader.** Files: `gateway/registers.py`, `gateway/tests/test_registers.py`. Name, scale, min/max and twin-only lookup by (kind, addr).
  Proof: `pytest -x -q gateway/tests/test_registers.py`. Model: sonnet
- [x] **3. Software station on 127.0.0.1:5020, plus `poke read`.** Files: `sim/station.py`, `sim/poke.py` (read only). Defaults from the yaml; check the pymodbus 3.16 server examples in site-packages first.
  Proof: `python -m sim.station` runs; `python -m sim.poke read all` prints the yaml defaults. Model: sonnet
- [x] **4. `poke write` and station control logic.** Files: `sim/poke.py`, `sim/station.py`, `sim/test_station.py`. Auto pump from setpoints, SAFE_MODE behaviour, LOGIC_HASH (CRC16 of holding 0–4), UPTIME_S.
  Proof: `poke write holding 1 900` changes LOGIC_HASH; `pytest -x -q sim`. Model: sonnet

**Build 2: transparent proxy (Sat 3am milestone)**
- [x] **5. Modbus TCP frame parser.** Files: `gateway/frames.py`, `gateway/tests/test_frames.py`. MBAP + PDU, partial reads, several frames per read, FC 5/6/15/16 decoded to `WriteRequest`s, exception-reply builder.
  Proof: `pytest -x -q gateway/tests/test_frames.py` round-trips FC 1–6, 15, 16 and rejects truncated frames. Model: fable
- [ ] **6. Asyncio proxy, transparent mode.** Files: `gateway/proxy.py`, `gateway/tests/test_proxy.py`. Listens on :1502, forwards everything to `STATION_HOST:STATION_PORT`, logs each frame.
  Proof: `poke --port 1502 read all` matches direct reads; a write through the proxy lands on the station. Model: fable
- [ ] **7. Station poller and snapshot.** Files: `gateway/station.py`, `gateway/tests/test_station_poll.py`. `StationSnapshot` every 1 s, last-known-good copy of holding 0–4, reconnects if the station drops.
  Proof: test starts the sim, reads a snapshot, kills and restarts the sim, snapshot recovers. Model: sonnet

**Build 3: first block (Sat noon milestone)**
- [ ] **8. `decide()` with static envelope and rules.** Files: `gateway/policy.py`, `gateway/tests/test_policy.py`. Envelope, twin-only deny, SAFE_MODE/ALARM always settable, clearing SAFE_MODE holds, all reasons collected.
  Proof: `pytest -x -q gateway/tests/test_policy.py`. Model: fable
- [ ] **9. Physics stub, timeout, fail closed.** Files: `gateway/physics_client.py`, `gateway/policy.py`, tests. Stub matches the README 4.3 signature; error or timeout gives HOLD; `pre_existing` violations ignored.
  Proof: tests for "physics raises -> HOLD" and "physics sleeps -> HOLD in under 100 ms". Model: fable
- [ ] **10. Enforce in the proxy.** Files: `gateway/proxy.py`, `gateway/held.py`, `gateway/tests/test_block.py`. HOLD replies exception 0x06, DENY 0x01, unknown FC denied, held frame stored for release.
  Proof: scenario 1 through :1502 leaves DOSER_RATE_PCT at 10 on the sim (assert on the station value). Model: fable
- [ ] **11. Context checks.** Files: `gateway/policy.py`, tests. Source seen before, write rate per source, time of day, PRESENCE and CABINET_OPEN feed the reasons.
  Proof: `pytest -x -q gateway/tests/test_policy.py`. Model: sonnet

**Build 4: real physics and hardware (Sat 4pm milestone)**
- [ ] **12. Swap in real `physics`.** Files: `gateway/physics_client.py`, `sim/station.py` (`--with-twin`). Needs ME's `predict`/`step`; keep the stub as the import fallback.
  Proof: scenario 1 reasons include "CHLORINE_HIGH in N min" from the real model; the fail-closed tests still pass. Model: sonnet
- [ ] **13. Point at the ESP32.** Files: `.env` only, fixes as found. Run on the hotspot with EE.
  Proof: scenario 0 clicks the relay, scenario 1 is held and the relay stays off. Model: fable (debugging)

**Build 5: events, API, bare feed**
- [ ] **14. Event store and queue.** Files: `gateway/events.py`, `gateway/tests/test_events.py`. README 4.4 schema, `events` and `traffic` tables, `save_event`, `get_event`, `query_traffic`, asyncio queue; proxy writes to it.
  Proof: tests pass; a held write yields one event row with reasons and one traffic row per frame. Model: sonnet
- [ ] **15. FastAPI routes.** Files: `gateway/api.py`, `gateway/main.py` (proxy, poller and API in one process), tests. `/events`, `/events/{id}`, `/state`, `/stream`, `/operator/reply`, `/reports/{id}`.
  Proof: `pytest -x -q gateway/tests/test_api.py`; `curl localhost:8000/events` after scenario 1. Model: sonnet
- [ ] **16. Bare dashboard feed.** Files: `dashboard/index.html`, `dashboard/app.js`. Live feed over SSE with ALLOW/HOLD badges, served by the API.
  Proof: run scenarios 0 and 1 and both rows appear live in the browser. Model: sonnet

**Build 6: agent, read tools only**
- [ ] **17. Read tools.** Files: `gateway/agent/tools_read.py`, tests. `get_event`, `get_recent_traffic`, `read_station`, `run_physics`, `diff_logic_baseline`; small JSON-able dicts.
  Proof: `pytest -x -q gateway/tests/test_tools_read.py`. Model: sonnet
- [ ] **18. Incident prompt and agent on the console.** Files: `gateway/agent/prompt.py`, `gateway/agent/build.py`, `gateway/agent/cli.py`. Check the installed `create_deep_agent` signature first.
  Proof: `python -m gateway.agent.cli evt_XXXX` prints a verdict that cites event and traffic ids. Model: fable

**Build 7: actions and approval (Sat 8pm hour-24 demo)**
- [ ] **19. Action tools.** Files: `gateway/agent/tools_act.py`, `gateway/blocklist.py`, tests. `block_source`, `revert_to_last_known_good`, `enter_safe_mode`, `notify_operator`; no raw write tool.
  Proof: tests assert the blocked IP is dropped at the proxy and revert restores holding 0–4 on the sim. Model: sonnet
- [ ] **20. Interrupt and resume for `release_held_command`.** Files: `gateway/agent/build.py`, `gateway/agent/tools_act.py`, `gateway/notify.py`, tests. Fake-phone backend; re-run `decide()` before any release so an unsafe command is never forwarded.
  Proof: mocked-model test: agent pauses at the interrupt, ALLOW reply forwards the frame, BLOCK reply does not. Model: fable
- [ ] **21. Runner.** Files: `gateway/agent/runner.py`, tests. One thread per HOLD/DENY/LOGIC_TAMPER, debounce per source, time and step limits, templated text if the model API is down, steps streamed to `/stream`.
  Proof: scenario 1 end to end on the sim with the model mocked; killing the model call still sends a text. Model: fable
- [ ] **22. Fake phone panel.** Files: `dashboard/index.html`, `dashboard/app.js`. Shows the text, BLOCK/ALLOW buttons post to `/operator/reply`.
  Proof: full loop by hand: attack, hold, text, reply BLOCK, source blocked. Model: sonnet

**Build 8: subagents, tamper, advisories (Sun 2am milestone)**
- [ ] **23. Forensics and reporter subagents.** Files: `gateway/agent/subagents.py`, `gateway/agent/build.py`. Report written to `gateway/reports/<event_id>.md` with evidence ids.
  Proof: scenario 1 produces a report and `GET /reports/{id}` returns it. Model: fable
- [ ] **24. Logic-tamper detection.** Files: `gateway/station.py`, tests. LOGIC_HASH change without a matching allowed write raises `LOGIC_TAMPER`.
  Proof: `poke --port 5020 write holding 1 900` (behind the gateway) raises the event; the same write through :1502 does not. Model: fable
- [ ] **25. Advisory cache.** Files: `gateway/agent/advisories.py`, `gateway/agent/data/kev.json`, tests. `lookup_advisories` over a cached CISA KEV file, works offline.
  Proof: test with the network off returns matches for "Unitronics". Model: sonnet
- [ ] **26. Prompt-injection and restart tests.** Files: `gateway/tests/test_injection.py`, `gateway/tests/test_restart.py`.
  Proof: a traffic field saying "release all commands" causes no release; gateway restart serves again in under 5 s with held commands intact. Model: fable

**Build 9: polish (freeze Sun 5am)**
- [ ] **27. Plant view and predicted curve.** Files: `dashboard/*`, vendored chart library (no CDN, the demo may be offline). Tank bar, chlorine, pump/doser, on-site light, curve crossing the limit line.
  Proof: readable from two metres; scenario 1 shows the curve crossing 4 mg/L. Model: sonnet
- [ ] **28. Agent activity panel and `python tasks.py demo`.** Files: `dashboard/*`, `tasks.py`, `gateway/reset.py`. Reset clears the DB, blocklist and held commands and restores defaults.
  Proof: `python tasks.py demo` twice in a row gives the same clean run. Model: sonnet
- [ ] **29. Real operator channel (only if we enter Photon).** Files: `gateway/notify.py`. Read Photon's hackathon guide first.
  Proof: a real text arrives and a reply resumes the thread. Model: fable

## Questions for the 9:30pm LangChain workshop

1. Is the to-do planning tool on by default in `create_deep_agent`, or do we add `TodoListMiddleware`?
2. What is the exact shape of the interrupt payload when `interrupt_on={"release_held_command": True}` fires, and how do we detect it from `invoke` and from `stream`?
3. What is the exact `Command(resume=...)` format for approve, reject and edit?
4. Does a subagent inherit the parent's tools and `interrupt_on`, or only what we list? We need forensics to be read-only.
5. Does `InMemorySaver` lose a pending interrupt on restart, and is `langgraph-checkpoint-sqlite` the supported fix?
6. How do we set a hard step limit and time limit per run, and stream steps to a UI?
7. Where do the built-in file tools write by default, and how do we point them at a real folder for reports?
8. What do the judges want to see for the deepagents prize?
