"""
tools/fleetctl.py
-----------------
Command-line operator console for the scheduler.

    python3 tools/fleetctl.py status                 # fleet + tasks snapshot
    python3 tools/fleetctl.py --test status          # same, for the TEST mode stack
    python3 tools/fleetctl.py submit                 # default ABCD instruction
    python3 tools/fleetctl.py submit my_instruction.json
    python3 tools/fleetctl.py cancel <task_id>
    python3 tools/fleetctl.py reset <robot_id>       # after recovering a robot in 'error'
    python3 tools/fleetctl.py maintenance <robot_id> on|off
    python3 tools/fleetctl.py watch                  # stream scheduler events
    python3 tools/fleetctl.py prompt "take ABCD from manufacturing_1, check it, good to packaging"
    python3 tools/fleetctl.py confirm <request_id>   # when REQUIRE_CONFIRMATION=1
    python3 tools/fleetctl.py reject <request_id>

Add --test to talk to the TEST mode stack instead of the real one.
"""

import json
import os
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import modes  # noqa: E402
modes.apply_cli_mode()
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

    request_id = args[0] if verb in ("confirm", "reject") and args else uuid.uuid4().hex[:8]
    feedback_topic = config.topic(config.HMI_FEEDBACK_TOPIC)

    def on_connect(c, u, flags, rc, props):
        c.subscribe([(config.topic(config.EVENT_TOPIC), 1), (config.topic(config.FLEET_STATUS_TOPIC), 0),
                     (feedback_topic, 1)])
        got.set()

    def on_message(c, u, msg):
        payload = parse_json(msg)
        if msg.topic == feedback_topic:
            if isinstance(payload, dict) and payload.get("request_id") == request_id:
                result.setdefault("feedback", []).append(payload)
            return
        if verb == "watch" and msg.topic == config.topic(config.EVENT_TOPIC):
            print(json.dumps(payload))
        elif verb == "status" and msg.topic == config.topic(config.FLEET_STATUS_TOPIC):
            result["status"] = payload
        elif msg.topic == config.topic(config.EVENT_TOPIC) and isinstance(payload, dict):
            if payload.get("event") in ("command_result", "task_rejected", "task_accepted"):
                result["event"] = payload

    client.on_connect = on_connect
    client.on_message = on_message
    start(client)
    if not got.wait(10):
        sys.exit("could not connect to broker")

    def send(topic, payload):
        client.publish(topic, json.dumps(payload), qos=1)

    if verb in ("prompt", "confirm", "reject"):
        time.sleep(0.5)  # let the subscription settle
        if verb == "prompt":
            if not args:
                sys.exit('usage: fleetctl.py prompt "<what the robots should do>"')
            send(config.topic(config.HMI_PROMPT_TOPIC), {"prompt": " ".join(args), "request_id": request_id})
            print(f"prompt sent (request_id {request_id}); waiting for the reasoning agent (up to 90 s)...")
            wait_s = 90
        else:
            send(config.topic(config.HMI_CONFIRM_TOPIC), {"request_id": request_id, "confirm": verb == "confirm"})
            wait_s = 10
        end = time.time() + wait_s
        while time.time() < end and not any(
                f.get("forwarded") or f.get("needs_confirmation") or f.get("ok") is False or f.get("discarded")
                for f in result.get("feedback", [])):
            time.sleep(0.2)
        stop(client)
        fb = result.get("feedback")
        if not fb:
            sys.exit("no answer -- is reasoning_agent.py running in this mode? (launch.py server / test --with-llm)")
        last = fb[-1]
        if last.get("needs_confirmation"):
            print("understood as:", json.dumps(last["instruction"]))
            print(f"confirm:  python3 tools/fleetctl.py{' --test' if config.TEST_MODE else ''} confirm {request_id}")
            print(f"reject:   python3 tools/fleetctl.py{' --test' if config.TEST_MODE else ''} reject {request_id}")
        elif last.get("forwarded"):
            print("sent to the scheduler:", json.dumps(last["instruction"]))
        elif last.get("discarded"):
            print("discarded.")
        else:
            print("NOT accepted:", last.get("error"))
            if last.get("llm_output") is not None:
                print("model output was:", json.dumps(last["llm_output"]))
        return
    if verb == "status":
        send(config.topic(config.COMMAND_TOPIC), {"cmd": "status"})
    elif verb == "submit":
        instr = DEFAULT_INSTRUCTION
        if args:
            with open(args[0]) as f:
                instr = json.load(f)
        instr = {**instr, "request_id": instr.get("request_id") or uuid.uuid4().hex[:8]}
        send(config.topic(config.INSTRUCTION_TOPIC), instr)
    elif verb == "cancel" and args:
        send(config.topic(config.COMMAND_TOPIC), {"cmd": "cancel_task", "task_id": args[0]})
    elif verb == "reset" and args:
        send(config.topic(config.COMMAND_TOPIC), {"cmd": "reset_robot", "robot_id": args[0]})
    elif verb == "maintenance" and len(args) == 2:
        send(config.topic(config.COMMAND_TOPIC), {"cmd": "set_maintenance", "robot_id": args[0], "on": args[1] == "on"})
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
        age = time.time() - float(snap.get("ts", 0) or 0)
        print(config.describe_mode())
        if snap.get("scheduler") == "stopped":
            print(f"!! the scheduler in this mode is STOPPED (last status {age:.0f}s ago) -- showing its last state")
        elif age > 15:
            print(f"!! last status is {age:.0f}s old -- is the scheduler running in this mode?")
        print("ROBOTS")
        for r in snap.get("robots", []):
            where = "".join(f"  [{t} at {h.get('at_location') or '-'}]"
                            for t, h in (r.get("subsystems") or {}).items() if "at_location" in h)
            print(f"  {r['id']:10} {r['status']:12} {'healthy' if r['healthy'] else r['health_problem']}"
                  f"{where}{'  (' + r['reason'] + ')' if r.get('reason') else ''}")
        print(f"TASKS (pending subtasks: {snap.get('pending')})")
        for t in snap.get("tasks", []):
            cur = ", ".join(f"{c['action']}@{c['zone']} on {c['robot']}" for c in t.get("current", []))
            print(f"  {t['task_id']} {t['state']:10} {t['product']:10} carrier={t['carrier_robot']} "
                  f"{cur} {t.get('error') or ''}")
    elif verb == "status":
        print(f"No status from a scheduler in this mode ({config.describe_mode()}). Is it running?")
    else:
        print(json.dumps(result.get("event") or {"note": "no reply within 5s -- is the scheduler running "
                                                         "in this mode? (add --test for test mode)"}, indent=2))


if __name__ == "__main__":
    main()
