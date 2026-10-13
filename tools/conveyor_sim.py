"""
tools/conveyor_sim.py
---------------------
Simulated conveyor agent (Arduino Opta + camera) for TEST mode. Publishes
results in exactly the Opta's format on the conveyor topic, so the real
conveyor_inspection driver (agents/qc_01_conveyor.json) can be tested
end to end without the belt:

    sim/autonomous/product/Transformation
    {"x":12.500,"y":30.200,"theta":45.000,"result":"OK","valid":true,"cycle":5,"raw":"12.5;30.2;45.0;OK;",
     "product":"ABCD","switch_ok":true}

By default it answers every inspection the driver announces on
qc/<station>/request after --delay seconds (the time the part needs to reach
the camera), with the product name from the request (like the Opta, which
switches the camera to that product's program). Typed commands work as well:
    Enter / ok      publish an OK result        ng          publish an NG result
    raw <text>      publish an invalid camera message (valid=false, NG)

    python3 tools/conveyor_sim.py --test                     # answer requests, 90 % OK
    python3 tools/conveyor_sim.py --test --pass-rate 0.5 --delay 4
    python3 tools/conveyor_sim.py --test --manual            # only typed commands
    (or: python3 launch.py test --conveyor)

Refuses to run in REAL mode: a simulated verdict on the real topic could
send a real part to packaging.
"""

import argparse
import json
import os
import random
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import modes  # noqa: E402
modes.apply_cli_mode()
import config  # noqa: E402
from mqtt_common import connected_ok, guarded, make_client, start, stop  # noqa: E402


def opta_payload(cycle: int, ok: bool, valid: bool = True, raw: str = "", product=None) -> dict:
    """The JSON the Opta publishes (see publishResult() in the conveyor_agent sketch)."""
    switch = {"product": product, "switch_ok": True if product else None}
    if not valid:
        return {"x": None, "y": None, "theta": None, "result": "NG", "valid": False, "cycle": cycle,
                "raw": raw or "garbage", **switch}
    x, y, theta = round(random.uniform(0, 50), 3), round(random.uniform(0, 50), 3), round(random.uniform(-180, 180), 3)
    verdict = "OK" if ok else "NG"
    return {"x": x, "y": y, "theta": theta, "result": verdict, "valid": True, "cycle": cycle,
            "raw": f"{x};{y};{theta};{verdict};", **switch}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--station", default="qc_01")
    p.add_argument("--topic", default="autonomous/product/Transformation", help="the Opta's MQTT_TOPIC")
    p.add_argument("--pass-rate", type=float, default=0.9)
    p.add_argument("--delay", type=float, default=2.0, help="seconds from request to result")
    p.add_argument("--manual", action="store_true", help="do not answer requests; typed commands only")
    args = p.parse_args()
    if not config.TEST_MODE:
        sys.exit("The conveyor simulator only runs in TEST mode, so it can never publish a verdict for a "
                 "real part.\nUse:  python3 tools/conveyor_sim.py --test   (or: python3 launch.py test --conveyor)")

    result_topic = config.topic(args.topic)
    request_topic = config.topic(f"qc/{args.station}/request")
    print(f"{config.describe_mode()}\nconveyor simulator {args.station}: results on {result_topic}"
          + ("" if args.manual else f", answering {request_topic} after {args.delay}s"), flush=True)

    client = make_client(f"conveyor_sim_{args.station}", clean_session=True)
    lock = threading.Lock()
    state = {"cycle": 0}

    def publish(ok: bool, valid: bool = True, raw: str = "", product=None):
        with lock:
            state["cycle"] += 1
            payload = opta_payload(state["cycle"], ok, valid, raw, product)
        client.publish(result_topic, json.dumps(payload), qos=0)
        print(f"{time.strftime('%H:%M:%S')} published {payload}", flush=True)

    def answer(request):
        time.sleep(args.delay)
        publish(random.random() < args.pass_rate, product=request.get("product"))

    def on_connect(c, u, flags, rc, props):
        if connected_ok(rc) and not args.manual:
            c.subscribe(request_topic, qos=1)

    def on_message(c, u, msg):
        try:
            request = json.loads(msg.payload.decode("utf-8"))
        except ValueError:
            return
        if isinstance(request, dict):
            print(f"{time.strftime('%H:%M:%S')} part {request.get('product')} on the belt "
                  f"({request.get('request_id')})", flush=True)
            threading.Thread(target=answer, args=(request,), daemon=True).start()

    client.on_connect = guarded(on_connect)
    client.on_message = guarded(on_message)
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    start(client)

    def read_commands():
        for line in sys.stdin:
            cmd = line.strip()
            if cmd.lower() in ("", "ok", "s"):
                publish(True)
            elif cmd.lower() == "ng":
                publish(False)
            elif cmd.lower().startswith("raw"):
                publish(False, valid=False, raw=cmd[3:].strip())
            else:
                print("commands: Enter/ok | ng | raw <text>", flush=True)

    if sys.stdin and sys.stdin.isatty():
        threading.Thread(target=read_commands, daemon=True).start()
    try:
        while not stop_event.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        stop(client)


if __name__ == "__main__":
    main()
