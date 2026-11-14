"""Task runner (stdlib only, same on Windows and Mac): python tasks.py <command>."""
import argparse
import subprocess
import sys


def run(*cmd):
    try:
        return subprocess.call([sys.executable, *cmd])
    except KeyboardInterrupt:
        return 130


def not_built(name, step):
    print(f"'{name}' is not built yet (plan step {step}).")
    return 1


def main():
    p = argparse.ArgumentParser(prog="tasks.py", description="Backflow task runner")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sim", help="run the software station on 127.0.0.1:5020 "
                              "(extra flags pass through: --host 0.0.0.0 --demand 60)")
    sub.add_parser("gateway", help="run proxy + poller + API")
    t = sub.add_parser("test", help="run pytest (default: sim and gateway)")
    t.add_argument("paths", nargs="*", default=["sim", "gateway"])
    sub.add_parser("demo", help="reset state and start the full demo")
    a, extra = p.parse_known_args()
    if extra and a.cmd != "sim":
        p.error(f"unrecognized arguments: {' '.join(extra)}")

    if a.cmd == "sim":
        return run("-m", "sim.station", *extra)
    if a.cmd == "gateway":
        return run("-m", "gateway.main")
    if a.cmd == "test":
        return run("-m", "pytest", "-x", "-q", *a.paths)
    return not_built("demo", 28)


if __name__ == "__main__":
    sys.exit(main())
