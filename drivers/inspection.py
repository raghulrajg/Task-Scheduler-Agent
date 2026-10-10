"""
drivers/inspection.py
---------------------
Quality-check stations. Handles the `quality_check` action and always
returns result {"pass": true|false} on success. Anything ambiguous is a
FAILURE of the inspection (escalated by the scheduler), never a pass.

Two generic transports cover most vision systems:

HTTP (custom vision service, PLC gateway, Node-RED, ...):
{
  "type": "http_inspection",
  "trigger_url": "http://192.168.12.30:8080/inspect",
  "method": "POST",                 # body: {"product","subtask_id","zone"}
  "pass_field": "result.pass",      # dotted path in the JSON response
  "pass_values": [true, "OK", "PASS"],
  "fail_values": [false, "NG", "FAIL"],
  "health_url": "http://192.168.12.30:8080/health",   # optional, GET -> 200 = ready
  "headers": {"Authorization": "${QC_TOKEN}"},
  "timeout_s": 30
}

TCP line protocol (Keyence / Cognex / SICK style "trigger -> OK/NG"):
{
  "type": "tcp_inspection",
  "host": "192.168.12.31", "port": 8500,
  "trigger": "T1\\r",              # bytes sent to trigger one inspection
  "terminator": "\\r",
  "pass_regex": "^OK", "fail_regex": "^NG",
  "timeout_s": 10
}
"""

import json
import re
import socket
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, List

from drivers.base import Driver, cancelled, done, failed, robot_state


def _dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def _classify(value: Any, pass_values: List[Any], fail_values: List[Any]):
    """True/False for a recognised verdict, None otherwise. Type-strict, so
    the string "false" is never mistaken for a pass and 1 != True."""
    def member(v, values):
        return any(type(v) is type(x) and v == x for x in values)
    if member(value, pass_values):
        return True
    if member(value, fail_values):
        return False
    return None


class HttpInspectionDriver(Driver):
    actions = ("quality_check",)

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self._url = cfg["trigger_url"]
        self._method = cfg.get("method", "POST").upper()
        self._field = cfg.get("pass_field", "pass")
        self._pass_values = cfg.get("pass_values", [True])
        self._fail_values = cfg.get("fail_values", [False])
        self._health = cfg.get("health_url")
        self._headers = {"Content-Type": "application/json", **cfg.get("headers", {})}
        self._timeout = float(cfg.get("timeout_s", 30))

    def execute(self, subtask: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        body = json.dumps({"product": subtask.get("target"), "subtask_id": subtask["subtask_id"],
                           "zone": subtask.get("zone")}).encode("utf-8")
        req = urllib.request.Request(self._url, data=body if self._method != "GET" else None,
                                     method=self._method, headers=self._headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            return failed(f"QC service HTTP {e.code}: {e.read()[:200]!r}")
        except OSError as e:
            return failed(f"QC service unreachable: {e}")
        if cancel.is_set():
            return cancelled()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return failed(f"QC service returned non-JSON: {raw[:200]!r}")
        verdict = _classify(_dig(payload, self._field), self._pass_values, self._fail_values)
        if verdict is None:
            return failed(f"QC response has no recognised verdict at '{self._field}': {payload!r}"[:300])
        return done({"pass": verdict, "raw": payload})

    def get_state(self) -> Dict[str, Any]:
        if not self._health:
            return robot_state(True, "no health_url configured")
        try:
            with urllib.request.urlopen(self._health, timeout=3) as resp:
                return robot_state(resp.status == 200, f"health HTTP {resp.status}")
        except Exception as e:
            return robot_state(False, f"health check failed: {e}")


def _unescape(s: str) -> str:
    return s.encode("utf-8").decode("unicode_escape")


class TcpInspectionDriver(Driver):
    actions = ("quality_check",)

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self._host, self._port = cfg["host"], int(cfg["port"])
        self._trigger = _unescape(cfg.get("trigger", "T1\\r")).encode("latin-1")
        self._term = _unescape(cfg.get("terminator", "\\r")).encode("latin-1")
        self._pass_re = re.compile(cfg.get("pass_regex", "^OK"))
        self._fail_re = re.compile(cfg.get("fail_regex", "^NG"))
        self._timeout = float(cfg.get("timeout_s", 10))

    def execute(self, subtask: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        try:
            with socket.create_connection((self._host, self._port), timeout=self._timeout) as s:
                s.settimeout(self._timeout)
                s.sendall(self._trigger)
                buf = b""
                while self._term not in buf:
                    chunk = s.recv(1024)
                    if not chunk:
                        break
                    buf += chunk
        except OSError as e:
            return failed(f"QC device {self._host}:{self._port} error: {e}")
        if cancel.is_set():
            return cancelled()
        line = buf.split(self._term)[0].decode("latin-1").strip()
        if self._pass_re.search(line):
            return done({"pass": True, "raw": line})
        if self._fail_re.search(line):
            return done({"pass": False, "raw": line})
        return failed(f"QC device reply not recognised: {line!r}")

    def get_state(self) -> Dict[str, Any]:
        try:
            with socket.create_connection((self._host, self._port), timeout=2):
                return robot_state(True, "reachable")
        except OSError as e:
            return robot_state(False, f"unreachable: {e}")
