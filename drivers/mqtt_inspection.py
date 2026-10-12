"""
drivers/mqtt_inspection.py
--------------------------
Quality-check station that talks MQTT only (no TCP/HTTP to the camera).
The agent and the scanner exchange messages through the same EMQX broker:

    qc/<station>/request   agent -> scanner   {"request_id", "subtask_id", "product", "station", "ts"}
    qc/<station>/result    scanner -> agent   {"request_id", "pass": true|false, "details": {...}}
                                              or {"request_id", "error": "camera not ready"}
    qc/<station>/status    scanner -> all     retained {"online": true, "ts"}; last will {"online": false}

(In TEST mode these topics get the "sim/" prefix like every other topic.)
On the scanner side, run tools/scanner_bridge.py, or have the camera/PLC
publish the same messages itself.

Only an explicit pass/fail verdict counts. A missing, garbled or "error"
result, or no answer within timeout_s, is a FAILURE of the inspection
(the scheduler retries, then escalates) -- never a silent pass.

Config:
{
  "type": "mqtt_inspection",
  "station": "qc_01",                            # topic segment; default: the agent's robot_id
  "request_topic": "qc/qc_01/request",           # optional overrides
  "result_topic":  "qc/qc_01/result",
  "health_topic":  "qc/qc_01/status",            # null = don't require a scanner heartbeat
  "timeout_s": 30,
  "health_timeout_s": 20,
  "pass_field": "pass",
  "pass_values": [true, "OK", "PASS"],
  "fail_values": [false, "NG", "FAIL"]
}
"""

import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, Optional

from drivers.base import Driver, cancelled, done, failed, robot_state
from drivers.util import dig, matches

logger = logging.getLogger("driver.mqtt_inspection")


class MqttInspectionDriver(Driver):
    actions = ("quality_check",)

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        import config
        from mqtt_common import connected_ok, guarded, make_client, start
        station = cfg.get("station") or cfg.get("_robot_id") or "qc_01"
        self.request_topic = config.topic(cfg.get("request_topic") or f"qc/{station}/request")
        self.result_topic = config.topic(cfg.get("result_topic") or f"qc/{station}/result")
        health = cfg.get("health_topic", f"qc/{station}/status")
        self.health_topic = config.topic(health) if health else None
        self._timeout = float(cfg.get("timeout_s", 30))
        self._health_timeout = float(cfg.get("health_timeout_s", 20))
        self._field = cfg.get("pass_field", "pass")
        self._pass_values = cfg.get("pass_values", [True, "OK", "PASS"])
        self._fail_values = cfg.get("fail_values", [False, "NG", "FAIL"])
        self._lock = threading.Lock()
        self._waiting: Dict[str, Dict[str, Any]] = {}       # request_id -> {"event", "result"}
        self._health: Optional[Dict[str, Any]] = None
        self._health_seen = 0.0
        self.connected = False
        self._connected_ok = connected_ok
        self._created = time.time()

        self._client = make_client(f"qcdrv_{station}_{uuid.uuid4().hex[:6]}", clean_session=True)
        self._client.on_connect = guarded(self._on_connect)
        self._client.on_disconnect = guarded(self._on_disconnect)
        self._client.on_message = guarded(self._on_message)
        start(self._client, cfg.get("_mqtt_host"), cfg.get("_mqtt_port"))

    # ---- MQTT ------------------------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if not self._connected_ok(reason_code):
            logger.error("scanner link: MQTT connect refused: %s", reason_code)
            return
        self.connected = True
        subs = [(self.result_topic, 1)]
        if self.health_topic:
            subs.append((self.health_topic, 1))
        client.subscribe(subs)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            logger.warning("non-JSON message on %s ignored", msg.topic)
            return
        if not isinstance(payload, dict):
            return
        if msg.topic == self.health_topic:
            with self._lock:
                self._health = payload
                ts = payload.get("ts")
                self._health_seen = float(ts) if msg.retain and isinstance(ts, (int, float)) else time.time()
            return
        rid = payload.get("request_id")
        with self._lock:
            waiter = self._waiting.get(rid) if isinstance(rid, str) else None
            if waiter is None:
                return  # someone else's, a duplicate, or a late answer
            waiter["result"] = payload
            waiter["event"].set()

    def _startup_wait(self, window: float = 5.0) -> None:
        """Right after start-up (e.g. robot_agent.py --check / --run) give the
        connection and the retained scanner heartbeat a moment to arrive."""
        end = self._created + window
        while time.time() < end and not (self.connected and (self._health is not None or not self.health_topic)):
            time.sleep(0.05)

    # ---- Driver API ---------------------------------------------------------------------------
    def execute(self, subtask: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        self._startup_wait()
        rid = f"{subtask['subtask_id']}#{subtask.get('attempt', 1)}"
        waiter = {"event": threading.Event(), "result": None}
        with self._lock:
            self._waiting[rid] = waiter
        try:
            if not self.connected:
                return failed("not connected to the MQTT broker")
            request = {"request_id": rid, "subtask_id": subtask["subtask_id"], "product": subtask.get("target"),
                       "station": subtask.get("zone"), "ts": time.time()}
            self._client.publish(self.request_topic, json.dumps(request), qos=1)
            logger.info("QC request %s -> %s", rid, self.request_topic)
            deadline = time.time() + self._timeout
            while not waiter["event"].wait(0.2):
                if cancel.is_set():
                    return cancelled()
                if time.time() > deadline:
                    health = self._health_problem()
                    return failed(f"no result from the scanner on {self.result_topic} within "
                                  f"{self._timeout:.0f}s" + (f" ({health})" if health else ""))
            result = waiter["result"]
        finally:
            with self._lock:
                self._waiting.pop(rid, None)

        verdict = dig(result, self._field)
        if matches(verdict, self._pass_values):
            return done({"pass": True, "details": result.get("details"), "raw": result})
        if matches(verdict, self._fail_values):
            return done({"pass": False, "details": result.get("details"), "raw": result})
        if result.get("error"):
            return failed(f"scanner error: {result['error']}")
        return failed(f"scanner result has no recognised verdict at '{self._field}': {result!r}"[:300])

    def _health_problem(self) -> Optional[str]:
        if not self.connected:
            return "not connected to the MQTT broker"
        if not self.health_topic:
            return None
        with self._lock:
            h, seen = self._health, self._health_seen
        if h is None:
            return f"no scanner heartbeat on {self.health_topic}"
        if not h.get("online"):
            return "scanner reports offline"
        if time.time() - seen > self._health_timeout:
            return f"scanner heartbeat is {time.time() - seen:.0f}s old"
        return None

    def get_state(self) -> Dict[str, Any]:
        self._startup_wait()
        problem = self._health_problem()
        return robot_state(problem is None, problem or "scanner online")

    def close(self) -> None:
        from mqtt_common import stop
        stop(self._client)
