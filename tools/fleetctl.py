"""
tools/fleetctl.py
-----------------
Command-line operator console for the scheduler.

    python3 tools/fleetctl.py status                 # fleet + tasks snapshot
    python3 tools/fleetctl.py submit                 # default ABCD instruction
    python3 tools/fleetctl.py submit my_instruction.json
    python3 tools/fleetctl.py cancel <task_id>
    python3 tools/fleetctl.py reset <robot_id>       # after recovering a robot in 'error'
    python3 tools/fleetctl.py maintenance <robot_id> on|off
    python3 tools/fleetctl.py watch                  # stream scheduler events
"""

import json
import os
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from mqtt_common import make_client, parse_json, start, stop  # noqa: E402

DEFAULT_INSTRUCTION = {
    "product": "ABCD", "source": "manufacturing_1", "quality_check": "quality_station",
    "success_dest": "package_area", "fail_dest": "waste_area",
}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    verb, args = sys.argv[1], sys.argv[2:]

    client = make_client(f"operator_{uuid.uuid4().hex[:6]}", clean_session=True)
    got = threading.Event()
    result = {}

    def on_connect(c, u, flags, rc, props):
        c.subscribe([(config.EVENT_TOPIC, 1), (config.FLEET_STATUS_TOPIC, 0)])
        got.set()

    def on_message(c, u, msg):
        payload = parse_json(msg)
        if verb == "watch" and msg.topic == config.EVENT_TOPIC:
            print(json.dumps(payload))
        elif verb == "status" and msg.topic == config.FLEET_STATUS_TOPIC:
            result["status"] = payload
        elif msg.topic == config.EVENT_TOPIC and isinstance(payload, dict):
            if payload.get("event") in ("command_result", "task_rejected", "task_accepted"):
                result["event"] = payload

    client.on_connect = on_connect
    client.on_message = on_message
    start(client)
    if not got.wait(10):
        sys.exit("could not connect to broker")

    def send(topic, payload):
        client.publish(topic, json.dumps(payload), qos=1)

    if verb == "status":
        send(config.COMMAND_TOPIC, {"cmd": "status"})
    elif verb == "submit":
        instr = DEFAULT_INSTRUCTION
        if args:
            with open(args[0]) as f:
                instr = json.load(f)
        instr = {**instr, "request_id": instr.get("request_id") or uuid.uuid4().hex[:8]}
        send(config.INSTRUCTION_TOPIC, instr)
    elif verb == "cancel" and args:
        send(config.COMMAND_TOPIC, {"cmd": "cancel_task", "task_id": args[0]})
    elif verb == "reset" and args:
        send(config.COMMAND_TOPIC, {"cmd": "reset_robot", "robot_id": args[0]})
    elif verb == "maintenance" and len(args) == 2:
        send(config.COMMAND_TOPIC, {"cmd": "set_maintenance", "robot_id": args[0], "on": args[1] == "on"})
    elif verb == "watch":
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        stop(client)
        return
    else:
        print(__doc__)
        sys.exit(1)

    deadline = time.time() + 5
    while time.time() < deadline and not result:
        time.sleep(0.1)
    if verb == "status":
        time.sleep(0.5)
    stop(client)
    if verb == "status" and "status" in result:
        snap = result["status"]
        print("ROBOTS")
        for r in snap.get("robots", []):
            print(f"  {r['id']:10} {r['status']:12} {'healthy' if r['healthy'] else r['health_problem']}"
                  f"{'  (' + r['reason'] + ')' if r.get('reason') else ''}")
        print(f"TASKS (pending subtasks: {snap.get('pending')})")
        for t in snap.get("tasks", []):
            cur = ", ".join(f"{c['action']}@{c['zone']} on {c['robot']}" for c in t.get("current", []))
            print(f"  {t['task_id']} {t['state']:10} {t['product']:10} carrier={t['carrier_robot']} "
                  f"{cur} {t.get('error') or ''}")
    else:
        print(json.dumps(result.get("event") or {"note": "no reply within 5s"}, indent=2))


if __name__ == "__main__":
    main()
