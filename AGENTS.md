# CLAUDE.md (team working rules for AI assistants)

Loaded at the start of every session, for whichever teammate is working. Keep it short.

## First: who am I working with?
The user says their role at the start of a session (CompE, EE or ME). If they don't, ask.

| Role | Owns (edit only these) | Spec | Plan |
|---|---|---|---|
| CompE | `sim/`, `gateway/`, `dashboard/`, repo-root config | `docs/prompts/PROMPT_COMPE.md` | `docs/plans/PLAN_COMPE.md` |
| EE | `firmware/` | `docs/prompts/PROMPT_EE.md` | `docs/plans/PLAN_EE.md` |
| ME | `physics/`, `twin/`, `attacks/`, `docs/research.md`, `docs/demo.md` | `docs/prompts/PROMPT_ME.md` | `docs/plans/PLAN_ME.md` |

Never edit another role's folders. If something there blocks you, say so and I will ask
the owner.

## Read on demand, not up front
- `README.md` section 4 is **the contract** (ports, register map, physics API, event format).
  Do not change it. If it looks wrong, tell me.
- `shared/registers.yaml`: the register map.
- Your role's spec and plan (table above).
Open only the files the current step needs. Do not scan the whole repo.

## How to work
1. Work on **one plan step at a time**. Restate it in one or two lines and list the files
   you will touch.
2. Make the smallest change that works. Edit with diffs; do not rewrite whole files.
3. Run only the relevant tests (`pytest -x -q <path>` for Python). Show failures only.
4. If the same test fails twice with the same approach, stop and tell me what you think is
   wrong. Do not loop.
5. When the step passes: tick it in the plan, give me a one-line commit message, stop.

## Libraries
Pin versions (`requirements.txt`, `firmware/platformio.ini`). Before using a library's API,
check the **installed** version locally (`pip show`, the function signature in
site-packages, or the library's examples folder) instead of guessing or browsing. Browse
docs only if that is not enough. No new dependencies without asking.

## Hard rules
- No LLM in the allow/hold path. Fail closed. The agent has no raw Modbus write tool.
- Secrets only in `.env` or `firmware/src/secrets.h`, both git-ignored.
- All code written this weekend; nothing copied from earlier projects.
- Attack scripts run only against our own station or the software station.

## Output style
Short. No recaps. Code over prose.
