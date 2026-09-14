"""
mqtt_client.py
--------------
Thin wrapper around paho-mqtt. This is now the ONLY transport in the system
-- no ROS2, no rclpy. One MQTT client on the scheduler side subscribes to
two things:

    scheduler/instructions          reasoning agent -> scheduler (new task)
    robots/+/+/status                robot subsystem -> scheduler (task ack)

and publishes to:

    robots/<robot_id>/<subsystem>/task   scheduler -> robot subsystem

Install: pip install paho-mqtt --break-system-packages
"""

import json
import logging
from typing import Callable, Optional

import config

logger = logging.getLogger("mqtt_client")

TASK_TOPIC_TEMPLATE = "robots/{robot_id}/{subsystem}/task"
STATUS_TOPIC_WILDCARD = "robots/+/+/status"


class SchedulerMQTTClient:
    def __init__(self, broker_host: str = "localhost", broker_port: int = 1883,
                 client_id: str = "task_scheduler",
                 username: Optional[str] = None, password: Optional[str] = None,
                 on_status: Optional[Callable[[str, str, dict], None]] = None,
                 on_instruction: Optional[Callable[[dict], None]] = None,
                 instruction_topic: Optional[str] = None):
        """
        on_status: callback(robot_id, subsystem, payload_dict) for
        robots/<id>/<subsystem>/status messages.

        on_instruction: callback(instruction_dict) for new-task messages on
        instruction_topic (defaults to config.INSTRUCTION_TOPIC). Pass None
        if this client instance shouldn't listen for instructions at all
        (e.g. a test harness that only cares about robot status).
        """
        import paho.mqtt.client as mqtt  # imported lazily so this module can be
                                          # imported (e.g. for type hints/tests)
                                          # on machines without paho-mqtt installed
        self._client = mqtt.Client(client_id=client_id, clean_session=True)
        if username:
            self._client.username_pw_set(username, password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._on_status = on_status
        self._on_instruction = on_instruction
        self._instruction_topic = instruction_topic or config.INSTRUCTION_TOPIC
        self._host = broker_host
        self._port = broker_port

    def connect(self):
        self._client.connect(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def disconnect(self):
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, rc):
        logger.info("Connected to MQTT broker (rc=%s)", rc)
        client.subscribe(STATUS_TOPIC_WILDCARD, qos=1)
        if self._on_instruction:
            client.subscribe(self._instruction_topic, qos=1)
            logger.info("Subscribed to instruction topic: %s", self._instruction_topic)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Ignoring non-JSON message on %s", msg.topic)
            return

        if msg.topic == self._instruction_topic:
            if self._on_instruction:
                self._on_instruction(payload)
            return

        # topic shape: robots/<robot_id>/<subsystem>/status
        parts = msg.topic.split("/")
        if len(parts) == 4 and parts[0] == "robots" and parts[3] == "status":
            robot_id, subsystem = parts[1], parts[2]
            if self._on_status:
                self._on_status(robot_id, subsystem, payload)

    def publish_task(self, robot_id: str, subsystem: str, subtask: dict, qos: int = 1):
        topic = TASK_TOPIC_TEMPLATE.format(robot_id=robot_id, subsystem=subsystem)
        payload = json.dumps(subtask)
        logger.info("Publishing to %s: %s", topic, payload)
        self._client.publish(topic, payload, qos=qos)
