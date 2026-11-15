"""Kill the whole gateway process mid-incident and start it again: it serves in under 5 s,
the held command is still held, the operator is asked again, and release still works
(and still refuses the unsafe one). Runs gateway.main for real, with no model."""
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from http.cookiejar import CookieJar

import pytest

from sim import poke
from test_block import PUMP, RATE, fc6, on_station, send
from test_proxy import HOST, ROOT, free_port, station  # noqa: F401 (fixture)


class Gateway:
    def __init__(self, station, db):
        self.port, self.api = free_port(), free_port()
        self.env = {**os.environ, "AGENT_MODEL": "", "STATION_HOST": HOST,
                    "STATION_PORT": str(station.port), "GATEWAY_PORT": str(self.port),
                    "API_PORT": str(self.api), "DATABASE_URL": f"sqlite:///{db}",
                    "ADMIN_PASSWORD": "pw", "BACKFLOW_RESET_ON_START": "", "DEMO_CLOCK": ""}
        self.proc = None
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def start(self) -> float:
        """Start and return the seconds until both the API and the proxy answer."""
        t0 = time.time()
        self.proc = subprocess.Popen([sys.executable, "-m", "gateway.main"], cwd=ROOT, env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while time.time() - t0 < 20:
            try:
                self.get("/me")
                poke.read_all(HOST, self.port)
                return time.time() - t0
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("gateway did not start")

    def kill(self):
        if self.proc:
            self.proc.kill()                 # SIGKILL on POSIX, TerminateProcess on Windows
            self.proc.wait()
            self.proc = None

    def get(self, path):
        return json.load(self.http.open(f"http://{HOST}:{self.api}{path}", timeout=2))

    def post(self, path, body):
        req = urllib.request.Request(f"http://{HOST}:{self.api}{path}", json.dumps(body).encode(),
                                     {"content-type": "application/json"})
        return self.http.open(req, timeout=5).status

    def wait(self, cond, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if cond():
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False


@pytest.fixture
def gw(station, tmp_path):
    g = Gateway(station, tmp_path / "gw.db")
    yield g
    g.kill()


def test_restart_under_5_s_with_held_commands_intact(station, gw):
    gw.start()
    poke.write("coil", PUMP, 1, HOST, gw.port)
    assert send(gw.port, fc6(RATE, 40))[0] == 0x86                 # scenario 5: releasable
    assert send(gw.port, fc6(RATE, 100))[0] == 0x86                # scenario 1: never releasable
    held = {e["new_value"]: e["id"] for e in gw.get("/events") if e["decision"] == "HOLD"}
    assert set(held) == {40, 100}

    gw.kill()                                                      # hard kill, mid-incident
    took = gw.start()
    assert took < 5, f"restart took {took:.1f} s"
    assert {e["id"] for e in gw.get("/events") if e["decision"] == "HOLD"} == set(held.values())

    gw.post("/login", {"password": "pw"})
    assert gw.wait(lambda: len([m for m in gw.get("/operator/inbox") if m["open"]]) == 2)
    codes = {m["event_id"]: m["code"] for m in gw.get("/operator/inbox")}
    assert gw.post("/operator/reply", {"reply": "ALLOW", "event_id": held[100], "code": codes[held[100]]}) == 202
    assert gw.wait(lambda: any(s["kind"] == "refused" for s in gw.get("/agent/log")))
    assert on_station(station, "holding_register", RATE) == 10     # unsafe one stays out
    assert gw.post("/operator/reply", {"reply": "ALLOW", "event_id": held[40], "code": codes[held[40]]}) == 202
    assert gw.wait(lambda: on_station(station, "holding_register", RATE) == 40)

    gw.kill()                                                      # released/refused ones are gone
    gw.start()
    gw.post("/login", {"password": "pw"})
    time.sleep(0.5)
    assert gw.get("/operator/inbox") == []
