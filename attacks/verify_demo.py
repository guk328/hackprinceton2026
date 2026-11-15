"""Reproduce scenarios 0/1 on a fresh, owned local software rig and save evidence.

python -m attacks.verify_demo --output docs/evidence/scenario_0_1.json
Starts and stops its own station/proxy. Existing listening ports are not reused.
No real ESP32 is contacted, and no teammate source files are changed.
"""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import io
import json
import logging
from pathlib import Path
import socket
import subprocess
import sys
import tempfile


def port_free(port):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


async def verify(output, station_port=5020, gateway_port=1502):
    from gateway import physics_client
    from gateway.proxy import Proxy
    from sim.poke import Link

    if not physics_client.USING_REAL_PHYSICS:
        raise RuntimeError("real physics is required; refusing to verify against the stub")
    port_free(station_port)
    port_free(gateway_port)
    root = Path(__file__).resolve().parents[1]
    logger = logging.getLogger("gateway.proxy")
    trace = io.StringIO()
    handler = logging.StreamHandler(trace)
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    proxy = None
    with tempfile.TemporaryDirectory(prefix="backflow-scenario-") as folder:
        inputs = Path(folder) / "inputs.json"
        program = ("import asyncio,sys; from pathlib import Path; from sim.station import serve; "
                   "asyncio.run(serve(port=int(sys.argv[1]), inputs_path=Path(sys.argv[2])))")
        with (Path(folder) / "station.log").open("w+") as station_log:
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", program, str(station_port), str(inputs)],
                cwd=root, stdout=station_log, stderr=station_log)
            try:
                for _ in range(160):
                    if process.poll() is not None:
                        station_log.seek(0)
                        raise RuntimeError("station exited: " + station_log.read())
                    try:
                        reader, writer = await asyncio.open_connection("127.0.0.1", station_port)
                        writer.close()
                        await writer.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(0.05)
                else:
                    raise RuntimeError("software station did not start")

                def setup():
                    # Owned isolated fixture, not a remote attack. No twin runs
                    # concurrently: telemetry stays fixed for reproducible forecasts.
                    with Link("127.0.0.1", station_port) as station:
                        station.write("holding", 100, 400)
                        station.write("holding", 101, 100)
                await asyncio.to_thread(setup)
                proxy = Proxy("127.0.0.1", station_port, host="127.0.0.1", port=gateway_port)
                await proxy.start()

                async def scenario(module):
                    result = await asyncio.to_thread(
                        subprocess.run,
                        [sys.executable, "-m", module, "--host", "127.0.0.1", "--port", str(gateway_port)],
                        cwd=root, capture_output=True, text=True, timeout=20)
                    print(result.stdout, end="", flush=True)
                    if result.returncode:
                        raise RuntimeError(result.stdout + result.stderr)
                    return [json.loads(line) for line in result.stdout.splitlines() if line]

                normal = await scenario("attacks.normal_day")
                # The software station publishes actual relay outputs on its next tick.
                for _ in range(40):
                    snap = await proxy.poller.refresh()
                    if snap.discrete_inputs.get("PUMP_RUNNING") and snap.discrete_inputs.get("DOSER_RUNNING"):
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise RuntimeError("normal command did not produce actual relay outputs")
                attack = await scenario("attacks.chlorine_overdose")
                held = proxy.held.all()
                if len(held) != 1:
                    raise RuntimeError(f"expected one held frame, got {len(held)}")
                command = held[0]
                if [(w.kind, w.address, w.value) for w in command.writes] != [("register", 0, 100)]:
                    raise RuntimeError("the held frame is not the overdose attempt")
                decision = command.decisions[0]
                prediction = decision.prediction
                crossing = next((v for v in prediction.violations
                                 if v.code == "CHLORINE_HIGH" and not v.pre_existing), None)
                if decision.decision != "HOLD" or crossing is None:
                    raise RuntimeError("hold is not supported by a new CHLORINE_HIGH prediction")

                def direct_readback():
                    with Link("127.0.0.1", station_port) as station:
                        return {"doser_rate_pct": station.read("holding_register", 0),
                                "high_sp_raw": station.read("holding_register", 1),
                                "pump_running": station.read("discrete_input", 4),
                                "doser_running": station.read("discrete_input", 5),
                                "level_raw": station.read("holding_register", 100),
                                "chlorine_raw": station.read("holding_register", 101)}
                readback = await asyncio.to_thread(direct_readback)
                if readback != {"doser_rate_pct": 10, "high_sp_raw": 840,
                                "pump_running": 1, "doser_running": 1,
                                "level_raw": 400, "chlorine_raw": 100}:
                    raise RuntimeError(f"unexpected station readback: {readback}")
                evidence = {
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "rig": "isolated local software station; no physical relay click verified",
                    "station_port": station_port, "gateway_port": gateway_port,
                    "using_real_physics": True, "horizon_min": physics_client.HORIZON_MIN,
                    "fixture": {"level_pct": 40, "chlorine_mgl": 1, "demand_pct": 40,
                                "initial_rate_pct": 10, "auto_mode": True,
                                "telemetry_frozen": True},
                    "normal_day": normal, "chlorine_overdose": attack,
                    "held_event": {"id": command.id, "ts": command.ts,
                                   "src_ip": command.src_ip, "function": command.frame.fc,
                                   "decision": decision.decision, "reasons": decision.reasons,
                                   "writes": [asdict(w) for w in command.writes],
                                   "prediction": asdict(prediction)},
                    "direct_station_readback": readback, "gateway_trace": trace.getvalue(),
                }
                output = Path(output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(evidence, indent=2, allow_nan=False) + "\n")
                print(f"PASS: CHLORINE_HIGH at {crossing.minutes_until:.3f} plant-min; "
                      f"rate remains 10%; evidence={output}", flush=True)
            finally:
                if proxy is not None:
                    await proxy.close()
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
                logger.removeHandler(handler)
                logger.setLevel(old_level)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/evidence/scenario_0_1.json"))
    parser.add_argument("--station-port", type=int, default=5020)
    parser.add_argument("--gateway-port", type=int, default=1502)
    args = parser.parse_args(argv)
    if args.station_port == args.gateway_port or any(not 1 <= p <= 65535 for p in (args.station_port, args.gateway_port)):
        parser.error("choose two distinct valid local ports")
    if args.output.exists():
        parser.error("evidence file exists; choose a new --output filename")
    try:
        asyncio.run(verify(args.output, args.station_port, args.gateway_port))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
