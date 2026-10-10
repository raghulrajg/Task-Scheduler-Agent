"""
manual_runner.py
----------------
Submit ONE instruction to the running scheduler_service.py and follow it
until it completes or fails. (Earlier versions started a second, private
scheduler instance -- two schedulers dispatching to the same robots is
unsafe, so this now only talks to the real one over MQTT.)

Usage:
    python3 manual_runner.py
    python3 manual_runner.py my_instruction.json
"""

import json
import sys
import threading
import time
import uuid

if __name__ == "__main__":      # --test / --real on the command line (must run before config import)
    import modes
    modes.apply_cli_mode()

import config
from mqtt_common import make_client, parse_json, start, stop

DEFAULT_INSTRUCTION = {
    "product": "ABCD",
    "source": "manufacturing_1",
    "quality_check": "quality_station",
    "success_dest": "package_area",
    "fail_dest": "waste_area",
}
FINAL = ("task_completed", "task_failed", "task_cancelled", "task_rejected")


def main():
    instruction = DEFAULT_INSTRUCTION
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            instruction = json.load(f)
    request_id = uuid.uuid4().hex[:8]
    instruction = {**instruction, "request_id": request_id}

    finished = threading.Event()
    connected = threading.Event()
    client = make_client(f"manual_runner_{request_id}", clean_session=True)

    def on_connect(c, u, flags, rc, props):
        c.subscribe(config.topic(config.EVENT_TOPIC), qos=1)
        connected.set()

    def on_message(c, u, msg):
        ev = parse_json(msg)
        if not isinstance(ev, dict) or ev.get("request_id") != request_id:
            return
        print(f"{time.strftime('%H:%M:%S')} {ev.get('event'):20} "
              f"{json.dumps({k: v for k, v in ev.items() if k not in ('event', 'ts', 'request_id')})}")
        if ev.get("event") in FINAL:
            finished.set()

    client.on_connect = on_connect
    client.on_message = on_message
    start(client)
    if not connected.wait(10):
        sys.exit("Could not connect to the MQTT broker")
    print("Submitting:", instruction)
    client.publish(config.topic(config.INSTRUCTION_TOPIC), json.dumps(instruction), qos=1)
    try:
        while not finished.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        stop(client)


if __name__ == "__main__":
    main()
