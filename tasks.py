"""Task runner (stdlib only, same on Windows and Mac): python tasks.py <command>."""
import argparse
import os
import socket
import subprocess
import sys
import time


def run(*cmd):
    try:
        return subprocess.call([sys.executable, *cmd])
    except KeyboardInterrupt:
        return 130


def not_built(name, step):
    print(f"'{name}' is not built yet (plan step {step}).")
    return 1


def listening(host, port):
    try:
        socket.create_connection((host, port), timeout=0.3).close()
        return True
    except OSError:
        return False


DEMO_PASSWORD = "backflow-demo"   # used only when .env sets no ADMIN_PASSWORD; printed at start


def demo(sim=False):
    """Clean start of the whole demo: software station (unless .env points at the ESP32 or
    one is already up), twin, then the gateway with a full reset. Ctrl-C stops everything.
    `sim` ignores STATION_HOST in .env and uses the software station (the board is down)."""
    env = dict(os.environ)
    if os.path.exists(".env"):
        for line in open(".env", encoding="utf-8"):
            k, sep, v = line.strip().partition("=")
            if sep and k and not k.startswith("#"):
                env.setdefault(k, v.split("#")[0].strip())
    host, port = env.get("STATION_HOST", "127.0.0.1"), int(env.get("STATION_PORT", "5020"))
    gw_env = {**os.environ, "BACKFLOW_RESET_ON_START": "1"}
    if sim:     # real environment variables win over .env in the gateway (load_dotenv)
        host, port = "127.0.0.1", 5020
        gw_env.update(STATION_HOST=host, STATION_PORT=str(port))
    password = env.get("ADMIN_PASSWORD") or DEMO_PASSWORD
    gw_env["ADMIN_PASSWORD"] = password
    gw, api = int(env.get("GATEWAY_PORT", "1502")), int(env.get("API_PORT", "8000"))
    if listening("127.0.0.1", gw) or listening("127.0.0.1", api):
        print(f"A gateway is already running (port {gw} or {api}). Stop it first.")
        return 1
    procs = []
    try:
        if host in ("127.0.0.1", "localhost") and not listening(host, port):
            procs.append(subprocess.Popen([sys.executable, "-m", "sim.station", "--port", str(port)]))
            for _ in range(50):
                if listening(host, port):
                    break
                time.sleep(0.1)
        if not listening(host, port):
            print(f"Station {host}:{port} is not answering.")
            return 1
        if os.path.exists(os.path.join("twin", "run.py")):
            procs.append(subprocess.Popen([sys.executable, "-m", "twin.run", "--host", host,
                                           "--port", str(port), "--reset"]))
        kind = "software station" if host in ("127.0.0.1", "localhost") else "ESP32"
        shown = password if password == DEMO_PASSWORD else "(ADMIN_PASSWORD from .env)"
        print(f"\n  Site:      http://localhost:{api}   (replay: /console.html?mode=replay)"
              f"\n  Sign in:   operator / {shown}"
              f"\n  Gateway:   port {gw}   station: {host}:{port} ({kind})"
              f"\n  Scenarios: python -m attacks.normal_day   then   python -m attacks.chlorine_overdose\n")
        return subprocess.call([sys.executable, "-m", "gateway.main"], env=gw_env)
    except KeyboardInterrupt:
        return 130
    finally:
        for p in procs:
            p.terminate()


def main():
    p = argparse.ArgumentParser(prog="tasks.py", description="Backflow task runner")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sim", help="run the software station on 127.0.0.1:5020 "
                              "(extra flags pass through: --host 0.0.0.0 --demand 60)")
    sub.add_parser("gateway", help="run proxy + poller + API")
    t = sub.add_parser("test", help="run pytest (default: sim and gateway)")
    t.add_argument("paths", nargs="*", default=["sim", "gateway"])
    d = sub.add_parser("demo", help="reset state and start the full demo")
    d.add_argument("--sim", action="store_true", help="use the software station even if .env points at the ESP32")
    a, extra = p.parse_known_args()
    if extra and a.cmd != "sim":
        p.error(f"unrecognized arguments: {' '.join(extra)}")

    if a.cmd == "sim":
        return run("-m", "sim.station", *extra)
    if a.cmd == "gateway":
        return run("-m", "gateway.main")
    if a.cmd == "test":
        return run("-m", "pytest", "-x", "-q", *a.paths)
    return demo(sim=a.sim)


if __name__ == "__main__":
    sys.exit(main())
