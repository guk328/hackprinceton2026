# The Backflow site

Static files served by the gateway's FastAPI app (`gateway/api.py` mounts this folder at `/`),
and published as-is to https://backflow.design (below). No build step, no CDN, system fonts. It
works on the hotspot with no internet.

## Run

```
python tasks.py demo          # station from .env (the ESP32), twin, gateway, full reset
python tasks.py demo --sim    # same, but the software station (use when the board is down)
```

Open http://localhost:8000 and sign in as `operator`. The password is `ADMIN_PASSWORD` from
`.env`; if that is empty, the gateway falls back to the built-in demo password `backflow-demo`
(`gateway/auth.py`). Set `ADMIN_PASSWORD` for anything but the demo.

Then, in a second terminal:

```
python -m attacks.normal_day           # scenario 0: allowed
python -m attacks.chlorine_overdose    # scenario 1: held with a physics reason
```

Approve or Reject on the centre card once the agent's briefing arrives (10 to 20 s).
After a Reject the source address is blocked for 60 minutes: press **Reset demo** before the
next run.

## Public demo: https://backflow.design

`.github/workflows/pages.yml` publishes this folder to GitHub Pages on every push to `main` that
touches `dashboard/`. There is no gateway behind the public site, so on `backflow.design` or
`*.github.io` the pages switch themselves to the recording (`Backflow.hosted` in `data.js`):

- the console plays `replay/scenario_0_1.js` and loops: at the operator's question it pulses the
  button the recorded operator pressed and continues after 8 s if nobody clicks; 10 s after the
  end it starts over
- the landing page's sign-in button becomes "Watch the demo"; the sign-in page says it is the
  public demo; report links read from the recording

Try the same loop locally with `console.html?mode=replay&loop=1`.

One-time setup (repo admin and whoever holds the domain):

1. The repo must be public (GitHub Pages on a free account).
2. Repo Settings → Pages → Build and deployment → Source: **GitHub Actions**. Then run the
   "Publish site" workflow once (Actions tab → Run workflow), or push.
3. Same page → Custom domain: `backflow.design` → Save.
4. At the registrar, DNS for `backflow.design` (delete any parking or forwarding records first):

   | Type | Host | Value |
   |---|---|---|
   | A | @ | 185.199.108.153 |
   | A | @ | 185.199.109.153 |
   | A | @ | 185.199.110.153 |
   | A | @ | 185.199.111.153 |
   | AAAA | @ | 2606:50c0:8000::153 |
   | AAAA | @ | 2606:50c0:8001::153 |
   | AAAA | @ | 2606:50c0:8002::153 |
   | AAAA | @ | 2606:50c0:8003::153 |
   | CNAME | www | guk328.github.io |

5. Once the DNS check in Settings → Pages goes green (minutes to an hour), tick **Enforce HTTPS**.
6. Recommended: verify the domain under your GitHub profile → Settings → Pages, so nobody else
   can point a repo at it.

To refresh the recording, run the full demo and `python dashboard/tools/record_replay.py`, then
push.

## Pages

| Page | What it is |
|---|---|
| `index.html` | Company landing page (`landing.css`, `landing.js`) |
| `login.html` | Sign-in (one account, `POST /login`) |
| `console.html` | Operator console, live from the API (`app.css`, `data.js`, `console.js`) |
| `console.html?mode=replay` | The same console playing `replay/scenario_0_1.js`. No sign-in, no gateway needed; it also opens straight from disk |
| `report.html?id=evt_0003` | The agent's incident report (`GET /reports/{id}`) |

## Replay

`replay/scenario_0_1.js` is a recording of the real system, made by
`python dashboard/tools/record_replay.py` while the demo is running. It holds what the API
returned during scenarios 0 and 1 (state once a second, events, agent steps, the operator's
question and reply, the report). One-time reply codes are not stored. The console marks replay
with a dark striped bar and the word REPLAY; it pauses at the operator's question and continues
when you press the button the recorded operator pressed.

## What the site can and cannot do

It reads `/events`, `/events/{id}`, `/state`, `/stream`, `/agent/log`, `/operator/inbox`,
`/reports/{id}` and `/me`. It posts only `/login`, `/logout`, `/reset` and `/operator/reply`
(the operator's ALLOW or BLOCK with the one-time code). There is no route from the site to a
Modbus write, and nothing here is in the allow/hold path.

The console is laid out for a 1420 x 735 px viewport (a 1080p laptop at 150 % scaling) and scales
up from there. A missing DHT11 shows as "no sensor": the station reports 0 for both temperature and
humidity until the sensor has answered, and 0 % humidity is outside a DHT11's range.
