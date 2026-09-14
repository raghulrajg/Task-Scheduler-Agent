"""
robot_agent.py
--------------
Runs ON the robot's onboard PC (or the controller box for that subsystem).
One process per subsystem: it subscribes to that subsystem's own task
topic, and YOU wire execute_task() to your real hardware calls (Nav2 goal,
MoveIt pick, a vision-station gRPC call, whatever your controller exposes).
When the action finishes, it publishes the status ack the scheduler is
waiting for -- that's the whole contract.

This is the missing piece that makes the earlier diagram a closed loop
instead of just the scheduler's half of it. execute_task() below is ported
from your manipulator_control_node.py's navigate_to()/actuate_gripper()
logic -- the same simulated timing and (for the QC subsystem) the same
random pass/fail placeholder you had, just split per action instead of one
node running the whole job.

Run:
    python3 robot_agent.py --robot-id robot_01 --subsystem amr
    python3 robot_agent.py --robot-id robot_01 --subsystem arm
    python3 robot_agent.py --robot-id qc_01     --subsystem scanner

Each subsystem controller on each physical robot runs its own instance of
this script (or your own equivalent implementing the same topic contract).
"""

import argparse
import json
import logging
import random
import time
from typing import Dict, Any

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
logger = logging.getLogger("robot_agent")

TASK_TOPIC_TEMPLATE = "robots/{robot_id}/{subsystem}/task"
STATUS_TOPIC_TEMPLATE = "robots/{robot_id}/{subsystem}/status"


def navigate_to(target: str):
    """Ported from manipulator_control_node.py's navigate_to(). Replace the
    sleep with your real Nav2 goal call -- keep the same signature/return."""
    logger.info("Navigating mobile base to: %s...", target)
    time.sleep(1)


def actuate_gripper(action: str):
    """Ported from manipulator_control_node.py's actuate_gripper(). Replace
    the sleep with your real MoveIt/IK call."""
    logger.info("6-DOF arm executing IK for: %s", action.upper())
    time.sleep(1)


def run_quality_check() -> bool:
    """Ported from manipulator_control_node.py's simulated QC step
    (random.choice). Replace with a real subscriber/call to your vision
    sensor -- this is the one placeholder you explicitly flagged as
    temporary in the original script."""
    time.sleep(2)
    return random.choice([True, False])


def execute_task(subtask: Dict[str, Any]) -> Dict[str, Any]:
    """
    Dispatches to the right simulated (for now) hardware call based on the
    subtask's action. Each of navigate_to/actuate_gripper/run_quality_check
    above is the one function to replace with a real call for that
    subsystem -- this function itself shouldn't need to change.

    Must return a dict with at least {"state": "done"|"failed"}. For
    action == "quality_check", also include {"result": {"pass": bool}} --
    that's what the scheduler reads to pick the success/fail branch.
    """
    action = subtask["action"]
    logger.info("Executing %s: target=%s zone=%s", action, subtask.get("target"), subtask.get("zone"))

    try:
        if action == "navigate":
            navigate_to(subtask.get("zone"))
        elif action in ("pick", "place"):
            actuate_gripper(action)
        elif action == "quality_check":
            passed = run_quality_check()
            state = "PASS (OK)" if passed else "FAIL (BAD)"
            logger.info("--- QUALITY CHECK RESULT: %s ---", state)
            return {"state": "done", "result": {"pass": passed}}
        else:
            return {"state": "failed", "error": f"unknown action: {action}"}
    except Exception as e:
        return {"state": "failed", "error": str(e)}

    return {"state": "done"}


class RobotAgent:
    def __init__(self, robot_id: str, subsystem: str):
        import paho.mqtt.client as mqtt
        self.robot_id = robot_id
        self.subsystem = subsystem
        self.task_topic = TASK_TOPIC_TEMPLATE.format(robot_id=robot_id, subsystem=subsystem)
        self.status_topic = STATUS_TOPIC_TEMPLATE.format(robot_id=robot_id, subsystem=subsystem)

        self._client = mqtt.Client(client_id=f"agent_{robot_id}_{subsystem}", clean_session=True)
        if config.MQTT_USERNAME:
            self._client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        logger.info("Connected (rc=%s). Subscribing to %s", rc, self.task_topic)
        client.subscribe(self.task_topic, qos=1)

    def _on_message(self, client, userdata, msg):
        try:
            subtask = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.error("Bad JSON on %s: %r", msg.topic, msg.payload)
            return

        logger.info("Received subtask %s", subtask.get("subtask_id"))
        try:
            result = execute_task(subtask)
        except Exception as e:
            logger.exception("execute_task raised for subtask %s", subtask.get("subtask_id"))
            result = {"state": "failed", "error": str(e)}

        payload = {"subtask_id": subtask["subtask_id"], **result}
        client.publish(self.status_topic, json.dumps(payload), qos=1)
        logger.info("Published status to %s: %s", self.status_topic, payload)

    def run_forever(self):
        logger.info("Connecting to %s:%s as %s ...", config.MQTT_BROKER_HOST,
                    config.MQTT_BROKER_PORT, config.MQTT_USERNAME)
        self._client.connect(config.MQTT_BROKER_HOST, config.MQTT_BROKER_PORT, keepalive=60)
        self._client.loop_forever()  # blocks; auto-reconnects on drop


def main():
    parser = argparse.ArgumentParser(description="Run a robot subsystem's MQTT agent.")
    parser.add_argument("--robot-id", required=True, help="e.g. robot_01, qc_01")
    parser.add_argument("--subsystem", required=True, help="e.g. amr, arm, scanner")
    args = parser.parse_args()

    agent = RobotAgent(args.robot_id, args.subsystem)
    agent.run_forever()


if __name__ == "__main__":
    main()
