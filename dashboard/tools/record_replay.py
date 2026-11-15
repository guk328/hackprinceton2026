"""Record a real run of scenarios 0 and 1 for the console's replay mode.

Start the demo first (python tasks.py demo), then from the repo root:

    python dashboard/tools/record_replay.py              (operator replies BLOCK)
    python dashboard/tools/record_replay.py --reply ALLOW

It watches the same API the console uses (/stream, /state, /operator/inbox), runs
attacks.normal_day and attacks.chlorine_overdose against our own gateway, answers the agent's
question the way the console's buttons do, waits for the incident report, and writes
dashboard/replay/scenario_0_1.js. Nothing is edited by hand: the file is what the gateway said.
One-time reply codes are left out of the file. Stdlib only.
"""
import argparse
import http.cookiejar
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "dashboard" / "replay" / "scenario_0_1.js"
MAX_POINTS = 300


def env_file():
    out = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and k and not k.startswith("#"):
                out[k] = v.split("#")[0].strip()
    return out


class Api:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path, body=None, text=False):
        req = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"content-type": "application/json"})
        with self.opener.open(req, timeout=10) as r:
            raw = r.read().decode("utf-8")
        return raw if text else json.loads(raw)


def thin(points):
    if not points or len(points) <= MAX_POINTS:
        return points
    step = len(points) // MAX_POINTS + 1
    keep = points[::step]
    if keep[-1] != points[-1]:
        keep.append(points[-1])
    return [[round(x, 3) for x in p] for p in keep]


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--api", default="http://127.0.0.1:8000")
    p.add_argument("--gateway-port", type=int, default=1502)
    p.add_argument("--reply", choices=["BLOCK", "ALLOW"], default="BLOCK")
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument("--no-reset", action="store_true", help="do not POST /reset before recording")
    a = p.parse_args()
    env = env_file()
    password = os.getenv("ADMIN_PASSWORD") or env.get("ADMIN_PASSWORD") or "backflow-demo"
    host, port = env.get("STATION_HOST", "127.0.0.1"), env.get("STATION_PORT", "5020")
    station = "the software station" if host in ("127.0.0.1", "localhost") else "the ESP32 station"

    api = Api(a.api)
    api.call("/login", {"password": password})
    if not a.no_reset:
        print("reset:", api.call("/reset", {}))
        time.sleep(2)
    live = api.call("/state")
    if not live.get("connected"):
        sys.exit("the station is not connected; nothing to record")

    timeline, lock, t0 = [], threading.Lock(), time.time()
    steps, event_ids = [], []

    def add(kind, data):
        with lock:
            timeline.append({"t": round(time.time() - t0, 2), "k": kind, "d": data})

    def stream():
        with urllib.request.urlopen(a.api + "/stream", timeout=600) as r:
            name = None
            for raw in r:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:") and name in ("event", "agent"):
                    data = json.loads(line[5:])
                    add(name, data)
                    if name == "event":
                        event_ids.append(data["id"])
                    else:
                        steps.append(data)

    threading.Thread(target=stream, daemon=True).start()
    last_inbox, stop = [None], threading.Event()

    def poll():
        while not stop.is_set():
            try:
                add("state", api.call("/state"))
                inbox = [{k: v for k, v in q.items() if k != "code"} for q in api.call("/operator/inbox")]
                key = [(q["event_id"], q["reply"], q["open"]) for q in inbox]
                if key != last_inbox[0]:
                    last_inbox[0] = key
                    add("inbox", inbox)
            except Exception as e:  # keep recording through one bad poll
                print("poll:", e)
            stop.wait(1.0)

    threading.Thread(target=poll, daemon=True).start()

    def scenario(module):
        print(f"--- {module}")
        r = subprocess.run([sys.executable, "-m", module, "--port", str(a.gateway_port)], cwd=ROOT,
                           capture_output=True, text=True)
        print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip())
        if r.returncode != 0:
            stop.set()
            sys.exit(f"{module} failed; not writing a recording")

    def wait(what, check, timeout):
        end = time.time() + timeout
        while time.time() < end:
            got = check()
            if got:
                return got
            time.sleep(0.5)
        print(f"timed out waiting for {what}")
        return None

    time.sleep(4)
    scenario("attacks.normal_day")
    time.sleep(8)
    scenario("attacks.chlorine_overdose")
    question = wait("the agent's question", lambda: next((q for q in api.call("/operator/inbox") if q["open"]), None), 180)
    if question:
        time.sleep(8)       # reading time for whoever watches the replay
        print("reply:", a.reply, api.call("/operator/reply",
              {"reply": a.reply, "event_id": question["event_id"], "code": question["code"]}))
        wait("the incident report", lambda: any(s["kind"] == "report" for s in steps), 240)
        quiet = [len(steps)]

        def settled():
            now = len(steps)
            done = now == quiet[0]
            quiet[0] = now
            return done
        time.sleep(6)
        wait("the agent to finish", settled, 60)
    time.sleep(3)
    stop.set()

    events, reports = {}, {}
    for eid in dict.fromkeys(event_ids):
        e = api.call(f"/events/{eid}")
        pred = e.get("prediction") or {}
        for key in ("trajectory", "baseline_trajectory"):
            if pred.get(key):
                pred[key] = thin(pred[key])
        events[eid] = e
        try:
            reports[eid] = api.call(f"/reports/{eid}", text=True)
        except Exception:
            pass
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    with lock:
        timeline.sort(key=lambda x: x["t"])
        rec = {"meta": {"t0": t0, "duration_s": timeline[-1]["t"], "station": station, "station_port": port,
                        "scenarios": ["attacks.normal_day", "attacks.chlorine_overdose"], "operator_reply": a.reply,
                        "agent_model": env.get("AGENT_MODEL", ""), "commit": commit},
               "timeline": timeline, "events": events, "reports": reports}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text("// Recorded by dashboard/tools/record_replay.py from the running gateway. Do not edit.\n"
                     "window.BACKFLOW_REPLAY = " + json.dumps(rec, separators=(",", ":")) + ";\n", encoding="utf-8")
    kinds = {}
    for item in timeline:
        kinds[item["k"]] = kinds.get(item["k"], 0) + 1
    print(f"wrote {a.out} ({a.out.stat().st_size // 1024} KB): {kinds}, events {list(events)}, reports {list(reports)}")


if __name__ == "__main__":
    main()
