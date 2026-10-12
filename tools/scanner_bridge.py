"""
tools/scanner_bridge.py
-----------------------
Runs on the vision / scanner PC. Connects the camera to the fleet over MQTT
only: it answers inspection requests from the QC agent.

    qc/<station>/request  (in)   {"request_id", "product", "station", ...}
    qc/<station>/result   (out)  {"request_id", "pass": true|false, "details": {...}}  or  {"request_id", "error"}
    qc/<station>/status   (out)  retained heartbeat {"online": true}; last will {"online": false}

Three ways to produce the verdict:
  1. Your own Python: edit inspect() below (camera capture + your vision check).
  2. An existing program:  --command "python3 my_check.py"
     It gets QC_PRODUCT, QC_STATION, QC_REQUEST_ID in its environment and must
     print JSON like {"pass": true, ...} or just PASS / FAIL (OK / NG).
  3. Simulation:  --simulate 0.8   (80 % of parts pass; for tests only)

    python3 tools/scanner_bridge.py --station qc_01 --command "python3 /opt/vision/check.py"
    python3 tools/scanner_bridge.py --station qc_01 --simulate 0.8 --test     # TEST mode topics (sim/...)
    (or: python3 launch.py scanner qc_01 --command "..."; SCANNER_COMMAND in .env is the default command)

Broker settings come from .env, like every other program.
"""

import argparse
import json
import os
import random
import shlex
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import modes  # noqa: E402
modes.apply_cli_mode()
import config  # noqa: E402
from mqtt_common import connected_ok, guarded, make_client, start, stop  # noqa: E402


def inspect(request: dict) -> dict:
    """Put your camera code here. Return {"pass": True/False, "details": {...}}.
    Raise an exception if the inspection could not be done (camera fault, no
    part visible...): that is reported as an error, which the scheduler treats
    as a failed inspection -- never as a pass."""
    raise NotImplementedError("no vision code configured: edit inspect() in tools/scanner_bridge.py, "
                              "or start the bridge with --command or --simulate")


def run_command(command: str, request: dict, timeout: float) -> dict:
    env = dict(os.environ, QC_PRODUCT=str(request.get("product") or ""),
               QC_STATION=str(request.get("station") or ""), QC_REQUEST_ID=str(request.get("request_id")))
    proc = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=timeout, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"command exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    out = proc.stdout.strip()
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    word = out.split()[0].upper() if out else ""
    if word in ("PASS", "OK", "TRUE", "GOOD"):
        return {"pass": True, "details": {"output": out}}
    if word in ("FAIL", "NG", "FALSE", "BAD", "REJECT"):
        return {"pass": False, "details": {"output": out}}
    raise RuntimeError(f"command output not understood: {out[:200]!r}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--station", default="qc_01")
    p.add_argument("--command", default=os.environ.get("SCANNER_COMMAND") or None,
                   help="program that performs one inspection and prints the verdict "
                        "(default: SCANNER_COMMAND from .env)")
    p.add_argument("--simulate", type=float, metavar="PASS_RATE", help="random verdicts (tests only)")
    p.add_argument("--timeout", type=float, default=25, help="max seconds for one inspection")
    p.add_argument("--heartbeat", type=float, default=5)
    args = p.parse_args()

    req_t = config.topic(f"qc/{args.station}/request")
    res_t = config.topic(f"qc/{args.station}/result")
    hb_t = config.topic(f"qc/{args.station}/status")
    mode = "simulated" if args.simulate is not None else ("command" if args.command else "inspect()")
    print(f"{config.describe_mode()}\nscanner bridge {args.station}: {req_t} -> {res_t} ({mode})")

    client = make_client(f"scanner_{args.station}", clean_session=False, will_topic=hb_t,
                         will_payload={"online": False, "detail": "scanner bridge connection lost"})
    busy = threading.Lock()

    def heartbeat(online=True, detail="ready"):
        client.publish(hb_t, json.dumps({"online": online, "detail": detail, "mode": mode, "ts": time.time()}),
                       qos=1, retain=True)

    def handle(request):
        rid = request.get("request_id")
        with busy:
            try:
                if args.simulate is not None:
                    time.sleep(1.0)
                    verdict = {"pass": random.random() < args.simulate, "details": {"simulated": True}}
                elif args.command:
                    verdict = run_command(args.command, request, args.timeout)
                else:
                    verdict = inspect(request)
                result = {"request_id": rid, "ts": time.time(), **verdict}
            except Exception as e:
                result = {"request_id": rid, "ts": time.time(), "error": f"{type(e).__name__}: {e}"}
        client.publish(res_t, json.dumps(result), qos=1)
        print(f"{time.strftime('%H:%M:%S')} {rid} {request.get('product')}: "
              f"{'PASS' if result.get('pass') is True else 'FAIL' if result.get('pass') is False else 'ERROR ' + str(result.get('error'))}")

    def on_connect(c, u, flags, rc, props):
        if connected_ok(rc):
            c.subscribe(req_t, qos=1)
            heartbeat()

    def on_message(c, u, msg):
        try:
            request = json.loads(msg.payload.decode("utf-8"))
        except ValueError:
            return
        if isinstance(request, dict) and isinstance(request.get("request_id"), str):
            threading.Thread(target=handle, args=(request,), daemon=True).start()

    client.on_connect = guarded(on_connect)
    client.on_message = guarded(on_message)
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    start(client)
    try:
        while not stop_event.wait(args.heartbeat):
            heartbeat()
    except KeyboardInterrupt:
        pass
    finally:
        heartbeat(False, "scanner bridge stopped")
        time.sleep(0.3)
        stop(client)


if __name__ == "__main__":
    main()
