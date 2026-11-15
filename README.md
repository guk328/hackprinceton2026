# Backflow

**Every command to a water plant, checked against physics before it runs.**

Live demo: **[backflow.design](https://backflow.design)** (plays a recording of the real system on a loop)

---

# Inspiration

On 30 July 2026 the FBI and EPA put out a warning: attackers were changing settings on internet-facing
pump controllers at water systems in at least seven US states.

The targets weren't big city utilities with security teams. They were small towns, where one operator
looks after the whole system and is on call at 3 a.m. The controller that runs the chlorine doser sits on the
same network the contractor uses for remote access. If someone gets in and sets the doser to 100 %, nothing
stops it. The controller does what it's told.

Firewalls don't help here because the attack looks completely normal. It's a valid Modbus write, from an
address that's allowed to write, to a register that's meant to be written. The only way to see it's wrong
is to ask what the command would *do to the water*.

So we built a box that asks exactly that.

---

# What It Does

Backflow is a safety gateway that sits between remote access and the pump controller. Every command goes
through it, and every command gets run forward through a physics model of the plant before it's allowed
to reach the controller.

```
COMMAND ➔ PARSE ➔ POLICY + PHYSICS ➔ ALLOW or HOLD ➔ AGENT INVESTIGATES ➔ OPERATOR DECIDES ➔ REPORT
```

* **Predicts what a command does:** A model of the tank and the chlorine dosing simulates the next few
  hours. If the command would overdose chlorine, overflow the tank or drain the town's pressure, the gateway
  knows what would break and when (*"Chlorine reaches 4 mg/L in 139 min"*).
* **Holds what's unsafe, in milliseconds:** Safe commands pass straight through. Unsafe ones never reach the
  controller; the sender gets a Modbus exception and the station's alarm goes off.
* **No AI in the decision:** Allow or hold is plain code. It fails closed, so an error or timeout means hold.
* **An agent does the follow-up:** Once a command is held, an AI agent reads the traffic and the station's
  live state, reruns the physics, and sends the operator one plain-language briefing with a recommendation.
* **The operator stays in charge:** The agent can't write to the plant. It can only block a source, revert to
  the last known good settings, or put the station in safe mode. Releasing a held command always takes the
  operator's approval, plus a one-time code, and the command gets re-checked against physics on the way out.
* **Writes the incident report:** A timeline with evidence, ready to forward.
* **Catches tampering behind its back:** If the controller's settings change without an allowed write, the
  logic hash stops matching and the agent restores the last known good settings.

---

# How We Built It

### Tech Stack
* **Station:** ESP32 (PlatformIO, C++), Modbus TCP server, two relays (pump and chlorine doser), PIR and
  light sensor for "someone on site", potentiometer for town demand, OLED, buzzer and alarm LEDs
* **Physics:** Python model of a well-mixed tank with pump flow, demand and chlorine dosing. `predict()`
  returns what breaks and when; `step()` drives a digital twin that plays the real world for the demo.
* **Gateway:** Python, pymodbus, FastAPI, SQLite. Modbus proxy, policy and physics checks, held-command
  store, event log, live stream to the dashboard
* **Agent:** LangChain deepagents on Claude Haiku, with read-only investigation tools, three narrow action
  tools, reporter and forensics subagents, and a spending cap
* **Dashboard:** Plain HTML, CSS and JavaScript, no build step and no CDN, so it runs offline on a hotspot

### Architecture
```
 remote access ──► GATEWAY ──(allowed only)──► ESP32 STATION ◄── PLANT TWIN
                   policy + physics                (relays, sensors)    (tank level, chlorine)
                       │
                  held commands
                       ▼
                    AGENT ──► operator briefing ──► Approve / Reject ──► incident report
```

What's real and what's simulated: the Modbus traffic, the gateway, the block, the relays, the sensors and
the alarm are real. The tank level and chlorine are computed by the twin, and the ESP32 stands in for an
industrial controller.

---

# Running It Locally

```
python -m venv .venv
.venv\Scripts\Activate.ps1          # Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
python tasks.py demo --sim          # software station, twin and gateway; drop --sim to use the ESP32
```

Open http://localhost:8000 and sign in as `operator`. In a second terminal, run an attack:

```
python -m attacks.normal_day           # allowed
python -m attacks.chlorine_overdose    # held, with the physics reason
```

There are six scenarios in [`attacks/`](attacks/): normal day, chlorine overdose, drain the town, overflow,
logic tamper and operator on site. Only run them against your own station or the software station. The
register map is in [`shared/registers.yaml`](shared/registers.yaml), the site is documented in
[`dashboard/README.md`](dashboard/README.md), and the firmware in [`firmware/README.md`](firmware/README.md).
`python tasks.py test` runs the test suite.
