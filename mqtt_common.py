"""
mqtt_common.py
--------------
One place to build a correctly configured paho-mqtt client, used by the
scheduler, the robot agents and the reasoning agent.

Settings that matter on a real network (Wi-Fi AMRs drop off all the time):

  * clean_session=False + a stable, unique client_id: the broker keeps the
    session and queues QoS 1 messages while a client is briefly offline,
    instead of silently discarding them.
  * automatic reconnect with back-off (connect_async + loop_start).
  * exceptions in user callbacks are caught and logged. Without this, one
    malformed message kills paho's network thread and the process keeps
    running but stops receiving anything.
  * optional TLS and per-client credentials from config/env.
"""

import json
import logging
import ssl
from typing import Any, Callable, Optional

import paho.mqtt.client as mqtt

import config

logger = logging.getLogger("mqtt")

TASK_TOPIC = "robots/{robot_id}/{subsystem}/task"      # scheduler -> agent (task + cancel)
STATUS_TOPIC = "robots/{robot_id}/{subsystem}/status"  # agent -> scheduler (accepted/done/failed/...)
STATE_TOPIC = "robots/{robot_id}/{subsystem}/state"    # agent -> all (retained heartbeat, LWT)


def make_client(client_id: str, clean_session: bool = False,
                will_topic: Optional[str] = None, will_payload: Optional[dict] = None) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                         clean_session=clean_session)
    client.suppress_exceptions = True
    if config.MQTT_USERNAME:
        client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
    if config.MQTT_TLS:
        client.tls_set(ca_certs=config.MQTT_TLS_CA_CERT,
                       certfile=config.MQTT_TLS_CLIENT_CERT,
                       keyfile=config.MQTT_TLS_CLIENT_KEY,
                       cert_reqs=ssl.CERT_REQUIRED,
                       tls_version=ssl.PROTOCOL_TLS_CLIENT)
    if will_topic:
        client.will_set(will_topic, json.dumps(will_payload or {}), qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


def start(client: mqtt.Client, host: Optional[str] = None, port: Optional[int] = None):
    """Non-blocking connect that keeps retrying in the background."""
    host = host or config.MQTT_BROKER_HOST
    port = port or config.MQTT_BROKER_PORT
    logger.info("Connecting to MQTT %s:%s (tls=%s, user=%s)", host, port, config.MQTT_TLS,
                config.MQTT_USERNAME or "-")
    client.connect_async(host, port, keepalive=config.MQTT_KEEPALIVE_S)
    client.loop_start()


def stop(client: mqtt.Client):
    try:
        client.disconnect()
    finally:
        client.loop_stop()


def parse_json(msg) -> Any:
    """Decode a message payload as JSON; returns None (and logs) on garbage."""
    try:
        return json.loads(msg.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Ignoring non-JSON message on %s: %r", msg.topic, msg.payload[:200])
        return None


def guarded(fn: Callable) -> Callable:
    """Wrap a callback so an exception is logged instead of propagating into paho."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.exception("Unhandled error in MQTT callback %s", getattr(fn, "__name__", fn))
    wrapper.__name__ = getattr(fn, "__name__", "callback")
    return wrapper


def connected_ok(reason_code) -> bool:
    return not getattr(reason_code, "is_failure", False)
