"""
manual_runner.py
------------------
Runs the scheduler stand-alone, with NO ROS2 dependency, for testing against
a real MQTT broker before the reasoning agent is wired in. You supply the
instruction JSON yourself (either edited below, or via a file path arg).

Usage:
    python3 manual_runner.py
    python3 manual_runner.py my_instruction.json
"""

import json
import sys
import time
import logging

from robot_registry import RobotRegistry
from mqtt_client import SchedulerMQTTClient
from scheduler import TaskScheduler

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

# ---- broker config ----
MQTT_BROKER_HOST = "172.25.200.57"
MQTT_BROKER_PORT = 1883
MQTT_USERNAME = "raghulrajg"
MQTT_PASSWORD = "Gr2_nemam"

# ---- fallback instruction if no file is passed on the command line ----
DEFAULT_INSTRUCTION = {
    "product": "ABCD",
    "source": "manufacturing_1",
    "quality_check": "quality_station",
    "success_dest": "package_area",
    "fail_dest": "waste_area",
}


def load_instruction() -> dict:
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r") as f:
            return json.load(f)
    return DEFAULT_INSTRUCTION


def main():
    registry = RobotRegistry("robots_metadata.json")
    state = {"task_id": None}

    def on_status(robot_id, subsystem, payload):
        scheduler.on_robot_status(robot_id, subsystem, payload)
        if state["task_id"] and scheduler._task_complete(state["task_id"]):
            print(f"\nTask {state['task_id']} complete.")

    mqtt_client = SchedulerMQTTClient(
        broker_host=MQTT_BROKER_HOST,
        broker_port=MQTT_BROKER_PORT,
        client_id="task_scheduler_manual",
        username=MQTT_USERNAME,
        password=MQTT_PASSWORD,
        on_status=on_status,
    )
    scheduler = TaskScheduler(registry, mqtt_client)

    print(f"Connecting to {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT} as {MQTT_USERNAME} ...")
    mqtt_client.connect()
    time.sleep(1)  # give the connect/subscribe handshake a moment

    instruction = load_instruction()
    print("Submitting instruction:", instruction)
    state["task_id"] = scheduler.submit_instruction(instruction)
    print(f"Task id: {state['task_id']}")
    print("Watching for robots/+/status acks. Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        mqtt_client.disconnect()


if __name__ == "__main__":
    main()
