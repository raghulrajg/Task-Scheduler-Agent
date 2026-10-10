"""
mqtt_client.py
--------------
Scheduler-side MQTT transport.

Subscribes:
    scheduler/instructions        new tasks (from reasoning agent / HMI)
    scheduler/commands            operator commands (cancel_task, reset_robot, ...)
    robots/+/+/status             task acks from robot agents
    robots/+/+/state              retained heartbeats / last-will from robot agents

Publishes:
    robots/<id>/<sub>/task        {"type": "task", ...} and {"type": "cancel", ...}
                                  (same topic, so a cancel can never overtake
                                  the task it cancels)
    scheduler/events              task lifecycle events for HMIs / logging
    scheduler/status              retained fleet + task snapshot
"""

import json
import logging
from typing import Callable, Optional

import config
from mqtt_common import (connected_ok, guarded, make_client, parse_json, parse_robot_topic, start,
                         state_topic, status_topic, stop, task_topic)

logger = logging.getLogger("mqtt_client")


class SchedulerMQTTClient:
    def __init__(self, client_id: str = "task_scheduler",
                 on_status: Optional[Callable[[str, str, dict], None]] = None,
                 on_state: Optional[Callable[[str, str, dict], None]] = None,
                 on_instruction: Optional[Callable[[object], None]] = None,
                 on_command: Optional[Callable[[object], None]] = None,
                 host: Optional[str] = None, port: Optional[int] = None):
        self._client = make_client(client_id, clean_session=False)
        self._client.on_connect = guarded(self._on_connect)
        self._client.on_disconnect = guarded(self._on_disconnect)
        self._client.on_message = guarded(self._on_message)
        self._on_status = on_status
        self._on_state = on_state
        self._on_instruction = on_instruction
        self._on_command = on_command
        self._host, self._port = host, port
        self.connected = False

    def connect(self):
        start(self._client, self._host, self._port)

    def disconnect(self):
        stop(self._client)

    # ---- callbacks -------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if not connected_ok(reason_code):
            logger.error("MQTT connect refused: %s", reason_code)
            return
        self.connected = True
        logger.info("Connected to MQTT broker (session_present=%s)", flags.session_present)
        subs = [(status_topic("+", "+"), 1), (state_topic("+", "+"), 1)]
        if self._on_instruction:
            subs.append((config.topic(config.INSTRUCTION_TOPIC), 1))
        if self._on_command:
            subs.append((config.topic(config.COMMAND_TOPIC), 1))
        client.subscribe(subs)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        logger.warning("Disconnected from MQTT broker (%s); auto-reconnecting", reason_code)

    def _on_message(self, client, userdata, msg):
        payload = parse_json(msg)
        if payload is None:
            return
        if msg.topic == config.topic(config.INSTRUCTION_TOPIC):
            if self._on_instruction:
                self._on_instruction(payload)
            return
        if msg.topic == config.topic(config.COMMAND_TOPIC):
            if self._on_command:
                self._on_command(payload)
            return
        parsed = parse_robot_topic(msg.topic)
        if parsed:
            robot_id, subsystem, kind = parsed
            if kind == "status" and self._on_status:
                self._on_status(robot_id, subsystem, payload)
            elif kind == "state" and self._on_state:
                self._on_state(robot_id, subsystem, payload)

    # ---- publishing --------------------------------------------------------------
    def publish_task(self, robot_id: str, subsystem: str, subtask: dict):
        topic = task_topic(robot_id, subsystem)
        logger.info("-> %s %s#%s %s %s", topic, subtask["subtask_id"], subtask.get("attempt"),
                    subtask["action"], subtask.get("zone"))
        self._client.publish(topic, json.dumps(subtask), qos=1)

    def publish_cancel(self, robot_id: str, subsystem: str, subtask_id: str, attempt: int, reason: str):
        topic = task_topic(robot_id, subsystem)
        payload = {"type": "cancel", "subtask_id": subtask_id, "attempt": attempt, "reason": reason}
        logger.warning("-> %s CANCEL %s#%s (%s)", topic, subtask_id, attempt, reason)
        self._client.publish(topic, json.dumps(payload), qos=1)

    def publish_event(self, event: dict):
        self._client.publish(config.topic(config.EVENT_TOPIC), json.dumps(event, default=str), qos=1)

    def publish_fleet_status(self, snapshot: dict):
        self._client.publish(config.topic(config.FLEET_STATUS_TOPIC), json.dumps(snapshot, default=str),
                             qos=0, retain=True)
