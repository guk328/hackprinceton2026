# Backflow

**A physics-aware safety gateway for small water utilities.**
HackPrinceton Fall '26 · Business & Enterprise track · working name, change it if you like.

> Everyone on the team (and every AI assistant you use) reads this file first.
> The section called **The contract** is the only thing all three of us depend on.
> Change it only after telling the other two.

---

## 1. The pitch in four sentences

In July 2026, attackers hit more than 100 US water systems, mostly small towns whose pump
controllers (PLCs) were reachable from the internet. CISA's advice was to put a gateway between
the internet and the PLC. Backflow is that gateway: it reads every command headed for the PLC,
runs it through a physics model of the plant, and refuses anything that would overflow a tank,
drain the town's pressure, or overdose chlorine. An AI agent then investigates what happened,
puts the station back in a safe state, texts the one operator on call in plain English, and
drafts the incident report.

- **Who it helps:** residents on small water systems with no security staff.
- **Who pays:** the integrators who install and maintain these PLCs (monthly fee per station);
  cyber insurers and state grant programs second.
- **Why it is different:** ordinary firewalls ask "is this packet allowed?" Backflow asks
  "what would this command do to the water?"

## 2. How it works

```
 attacker / remote operator
          |
          v   Modbus TCP :1502
 +--------------------+        +---------------------------+
 |  GATEWAY (laptop)  |<------>|  PHYSICS MODEL (python)   |
 |  1 parse command   |        |  predict(state, command)  |
 |  2 policy check    |        |  -> safe? what breaks,    |
 |  3 physics check   |        |     and in how many min   |
 |  4 allow / hold    |        +---------------------------+
 +--------------------+
      | allowed only          held or odd events
      v  Modbus TCP :502            |
 +--------------------+             v
 |  STATION (ESP32)   |     +---------------------------+
 |  relay 1 = pump    |     |  AGENT (deepagents)       |
 |  relay 2 = doser   |     |  investigate -> act ->    |
 |  PIR + light =     |     |  text operator -> report  |
 |  "someone on site" |     +---------------------------+
 |  OLED, buzzer, LEDs|             |
 +--------------------+             v
      ^                      operator's phone, dashboard,
      |  reads relays,       incident report (markdown)
      |  writes tank level
 +--------------------+
 |  PLANT TWIN        |  the "real world": same physics model,
 |  (laptop)          |  stepped forward in time, sped up for the demo
 +--------------------+
```

**Two rules that keep this honest and safe**

1. **The AI is never in the hot path.** Allow/hold is decided by plain code (policy + physics) in
   milliseconds. The agent only works on what has already been held or flagged.
2. **The agent cannot send arbitrary commands to the station.** Its only write tools are
   "revert to last known good", "enter safe mode" and "block a source". Releasing a held
   command needs the operator's approval.

**What is real and what is simulated (say this to judges; they respect it)**

| Real | Simulated |
|---|---|
| Modbus TCP traffic, the gateway, the block | Tank level and chlorine (computed by the twin) |
| Relays switching a pump/fan, buzzer, OLED | The "town" (a potentiometer sets water demand) |
| On-site presence sensing (PIR, cabinet light) | The PLC itself (an ESP32 stands in for it) |

## 3. Repo layout and owners

```
backflow/
├── README.md              <- this file (shared)
├── shared/
│   └── registers.yaml     <- the register map, single source of truth
├── firmware/              <- EE   · ESP32 station (PlatformIO)
├── physics/               <- MechE· plant model, limits, tests
├── twin/                  <- MechE· runs the model as the "real world"
├── sim/                   <- CompE· software station (fallback if ESP32 dies)
├── gateway/               <- CompE· proxy, policy, agent, API
│   ├── proxy.py
│   ├── policy.py
│   ├── events.py
│   ├── agent/
│   └── api.py
├── dashboard/             <- CompE (MechE helps with the plant view)
├── attacks/               <- MechE· demo scripts, run only against our own rig
├── docs/
│   ├── prompts/           <- the three role prompts
│   ├── plans/             <- PLAN_COMPE.md, PLAN_EE.md, PLAN_ME.md (one per person)
│   ├── research.md        <- sources, market numbers, operator quotes
│   └── demo.md            <- demo script and judge Q&A
├── CLAUDE.md              <- rules every AI assistant loads automatically
├── .env.example
└── .gitignore
```

One directory per owner means almost no merge conflicts.

## 4. The contract

### 4.1 Network

| Thing | Address | Notes |
|---|---|---|
| Station (ESP32) | `STATION_HOST:502` | Modbus TCP, unit id 1 |
| Software station | `127.0.0.1:5020` | same register map, used when no hardware |
| Gateway | `0.0.0.0:1502` | what "the internet" talks to |
| Dashboard/API | `localhost:8000` | |

Campus Wi-Fi (`puvisitor`) uses a sign-in page the ESP32 cannot pass, and may block
device-to-device traffic. **Run everything on a phone hotspot.**

### 4.2 Register map (full version in `shared/registers.yaml`)

Scaling: level and temperature ×10, chlorine ×100. All values unsigned 16-bit.

**Coils (read/write)**

| Addr | Name | Meaning |
|---|---|---|
| 0 | PUMP_CMD | 1 = run the supply pump (relay 1) |
| 1 | DOSER_CMD | 1 = run the chlorine doser (relay 2) |
| 2 | AUTO_MODE | 1 = station controls the pump from level setpoints |
| 3 | SAFE_MODE | 1 = doser off, pump on local level control, remote writes ignored |
| 4 | ALARM | 1 = buzzer + red LED |

**Discrete inputs (read only)**

| Addr | Name | Source |
|---|---|---|
| 0 | PRESENCE | PIR motion in the last 60 s |
| 1 | CABINET_OPEN | photoresistor sees light |
| 2 | LOCAL_BUTTON | button held (local acknowledge / manual) |
| 3 | HIGH_LEVEL_SWITCH | IR obstacle module, stands in for a float switch |

**Input registers (read only)**

| Addr | Name | Scale | Source |
|---|---|---|---|
| 0 | DEMAND_PCT | ×10 | potentiometer, 0–100 % of max town demand |
| 1 | CABINET_TEMP_C | ×10 | DHT11 |
| 2 | CABINET_HUMIDITY | ×10 | DHT11 |
| 10 | LOGIC_HASH | – | CRC16 of holding registers 0–4 |
| 11 | UPTIME_S | – | seconds since boot (wraps) |

**Holding registers (read/write)**

| Addr | Name | Scale | Default | Meaning |
|---|---|---|---|---|
| 0 | DOSER_RATE_PCT | ×1 | 10 | doser output, 0–100 % |
| 1 | LEVEL_HIGH_SP | ×10 | 850 | pump stops above this level |
| 2 | LEVEL_LOW_SP | ×10 | 400 | pump starts below this level |
| 3 | CHLORINE_TARGET | ×100 | 100 | 1.00 mg/L |
| 4 | LOGIC_VERSION | – | 1 | bumped when control settings change |
| 100 | TANK_LEVEL_PCT | ×10 | 600 | **written by the twin only** |
| 101 | CHLORINE_MGL | ×100 | 100 | **written by the twin only** |

The twin talks to the station directly (it is "physics", not a network user).
Everything else goes through the gateway.

### 4.3 Physics API (`physics/`)

```python
from physics import PlantState, Command, predict, step

state = PlantState(level_pct=60.0, chlorine_mgl=1.0, demand_pct=40.0,
                   pump_on=True, doser_on=True, doser_rate_pct=10,
                   level_high_sp=85.0, level_low_sp=40.0, auto_mode=True)

cmd = Command(kind="register", address=0, value=100)   # doser to max

p = predict(state, cmd, horizon_min=120)
p.safe            # False
p.violations      # [Violation(code="CHLORINE_HIGH", minutes_until=11.2,
                  #            value=4.0, limit=4.0,
                  #            message="Chlorine passes 4.0 mg/L in about 11 minutes")]
p.trajectory      # [(t_min, level_pct, chlorine_mgl), ...] for the dashboard plot

next_state = step(state, dt_s=1.0)   # used by the twin
```

Violation codes: `TANK_OVERFLOW`, `PRESSURE_LOSS` (tank too low), `CHLORINE_HIGH`,
`CHLORINE_LOW`, `PUMP_SHORT_CYCLE`, `SETPOINT_OUT_OF_RANGE`.
`predict` must be pure, fast (<20 ms) and must never raise on odd input.

### 4.4 Event schema (gateway -> agent, dashboard, database)

```json
{
  "id": "evt_0042",
  "ts": "2026-11-14T03:12:09Z",
  "src_ip": "203.0.113.7",
  "function": 6,
  "kind": "register",
  "address": 0,
  "name": "DOSER_RATE_PCT",
  "old_value": 10,
  "new_value": 100,
  "decision": "HOLD",
  "reasons": ["CHLORINE_HIGH in 11 min", "source not seen before", "nobody on site"],
  "prediction": { "safe": false, "violations": [], "trajectory": [] },
  "station": { "presence": false, "cabinet_open": false, "level_pct": 61.2, "chlorine_mgl": 1.02 }
}
```

`decision` is one of `ALLOW`, `HOLD`, `DENY`. Held commands are **not** forwarded;
the gateway answers the sender with a Modbus exception.

## 5. Demo scenarios (`attacks/`)

Run these only against our own ESP32 or the software station.

| # | Name | What the script sends | What should happen |
|---|---|---|---|
| 0 | Normal day | operator turns pump on, small setpoint change | allowed, relay clicks |
| 1 | Chlorine overdose | DOSER_RATE_PCT -> 100, DOSER_CMD -> 1 | held, "chlorine > 4 mg/L in N min" |
| 2 | Drain the town | AUTO_MODE -> 0, PUMP_CMD -> 0 at high demand | held, "pressure loss in N min" |
| 3 | Overflow | LEVEL_HIGH_SP -> 1000, PUMP_CMD -> 1 | held, "tank overflows in N min" |
| 4 | Logic tamper | setpoints changed behind the gateway's back | LOGIC_HASH mismatch, agent reverts |
| 5 | Operator on site | same change as #1 but smaller, with PRESENCE + CABINET_OPEN | agent asks operator, approval releases it |

Scenario 1 is the one we show in the two-minute demo. The rest are for judge questions.

## 6. Working together

- **Repo:** create it after 8pm Friday (rules: repo created Nov 13–15, public, all work done in
  the hacking period). No code copied from older projects.
- **Branches:** `main` always runs. Work on `ee/...`, `me/...`, `ce/...`; merge small and often.
- **Commits:** small and frequent, plain messages.
- **Secrets:** API keys live in `.env` (git-ignored). `.env.example` lists the names only.
- **Using AI assistants:** open the assistant in the repo root so it loads `CLAUDE.md`, say
  your role (CompE, EE or ME), and have it write your plan to `docs/plans/PLAN_<ROLE>.md`
  before any code. Then work one plan step per session.
- **Check-ins:** five minutes at each milestone below. Ask: does the end-to-end path still work?
- **ChatOverflow:** whoever uses a coding agent connects it once (free prize entry).

## 7. Milestones

| When | Milestone | Proof |
|---|---|---|
| Fri 11pm | Skeleton | repo up, software station answers Modbus, `predict()` returns something |
| Sat 3am | Parts work alone | relay toggles over Wi-Fi; physics tests pass; proxy forwards and logs |
| Sat noon | First block | scenario 1 is held by the gateway against the software station |
| Sat 4pm | On hardware | scenario 1 held against the ESP32; relay stays off; twin drives the OLED level |
| **Sat 8pm** | **Hour-24 demo** | attack -> hold -> agent explains -> operator text -> reply -> report |
| Sun 2am | Polish | dashboard, scenarios 2–5, logic-hash check, advisory lookup |
| Sun 5am | Freeze | no new features; record 2-min video; write Devpost |
| Sun 7:30am | Submit | deadline is 8:00am; do not cut it close |

**If hardware fails:** switch `STATION_HOST` to the software station. Nothing else changes.

## 8. Prizes we are entering

- Main track: **Business & Enterprise** (it names cybersecurity).
- Special: Amazon Practical AI · Best Physical AI · LangChain deepagents · Best Hardware Hack ·
  ChatOverflow · Photon (only if operator texting runs on Photon) · Arlequin forensics (only if
  the report has an evidence-cited timeline) · Peri (only if we talk to a real operator or
  integrator and keep proof).
- MLH (offered at this event): Tiger Data (time-series log) · Vultr (hosting) · GoDaddy domain ·
  Gemini (only if it is our model anyway).

Add an integration only if it makes the product better.

## 9. Two-minute demo

1. **0:00–0:20** "In July, attackers hit over 100 US water systems, mostly small towns with one
   operator and no security team. This is a pump station."
2. **0:20–0:45** Normal command: relay clicks, pump runs, dashboard shows ALLOW.
3. **0:45–1:15** Attack: doser to max. No click. Dashboard shows the predicted chlorine curve
   crossing 4 mg/L and the reasons.
4. **1:15–1:40** Operator's phone buzzes with a plain-English text. Reply "block". The incident
   report appears with a timeline.
5. **1:40–2:00** "One box per station, sold through the integrators who install the PLCs.
   81% of US water systems are small. We start with one."

## 10. Open decisions

- [ ] Final name and domain
- [ ] Which model the agent runs on
- [ ] Operator channel: Photon, or a simpler fallback
- [ ] Pump or fan on relay 1 (if we can buy one Saturday)
- [ ] Who presents
