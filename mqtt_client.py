"""
mqtt_client.py
--------------
Thin wrapper around paho-mqtt. Publishes tasks to `robots/<robot_id>/task`
and listens on `robots/+/status` for completion/failure acks, which is how
the scheduler advances a multi-step, multi-robot task graph.

Install: pip install paho-mqtt --break-system-packages
"""

import json
import logging
from typing import Callable, Optional

logger = logging.getLogger("mqtt_client")

TASK_TOPIC_TEMPLATE = "robots/{robot_id}/task"
STATUS_TOPIC_WILDCARD = "robots/+/status"
STATUS_TOPIC_TEMPLATE = "robots/{robot_id}/status"


class SchedulerMQTTClient:
    def __init__(self, broker_host: str = "localhost", broker_port: int = 1883,
                 client_id: str = "task_scheduler",
                 on_status: Optional[Callable[[str, dict], None]] = None):
        """
        on_status: callback(robot_id, payload_dict) invoked whenever a robot
        publishes on robots/<id>/status. payload_dict is expected to look like
        {"subtask_id": "...", "state": "done"|"failed", "result": {...}}
        """
        import paho.mqtt.client as mqtt  # imported lazily so this module can be
                                          # imported (e.g. for type hints/tests)
                                          # on machines without paho-mqtt installed
        self._client = mqtt.Client(client_id=client_id, clean_session=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._on_status = on_status
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

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Ignoring non-JSON message on %s", msg.topic)
            return

        # topic shape: robots/<robot_id>/status
        parts = msg.topic.split("/")
        if len(parts) == 3 and parts[0] == "robots" and parts[2] == "status":
            robot_id = parts[1]
            if self._on_status:
                self._on_status(robot_id, payload)

    def publish_task(self, robot_id: str, subtask: dict, qos: int = 1):
        topic = TASK_TOPIC_TEMPLATE.format(robot_id=robot_id)
        payload = json.dumps(subtask)
        logger.info("Publishing to %s: %s", topic, payload)
        self._client.publish(topic, payload, qos=qos)
