"""
drivers/mir.py
--------------
Mobile Industrial Robots (MiR100/200/250/600/1350) over the robot's REST
API v2.0.0. Handles the `navigate` action.

How it maps to MiR concepts
  Each scheduler location (site.json) maps to a MiR *mission* you build in
  the MiR web interface, e.g. "Go to manufacturing_1" containing a
  "Move to position" (or "Docking" to a VL marker for precise arm work).
  Put the mission GUIDs here. List them with:  python3 tools/mir_missions.py

  navigate(zone) =
      wait for robot state Ready (3)
      POST   /mission_queue {"mission_id": <guid>, "parameters": [...]}
      poll   GET /mission_queue/<id> until Done / Aborted
      cancel DELETE /mission_queue/<id>

Config:
{
  "type": "mir",
  "host": "192.168.12.20",
  "port": 80,
  "username": "${MIR_USERNAME}",        # ${VAR} is read from the environment
  "password": "${MIR_PASSWORD}",
  "auth_header": null,                  # OR paste the ready-made "Basic ..." value
  "locations": {
    "manufacturing_1": {"mission_guid": "...", "parameters": []},
    "quality_station": {"mission_guid": "..."}
  },
  "poll_interval_s": 1.0,
  "ready_wait_s": 15,
  "request_timeout_s": 5,
  "max_comm_loss_s": 30,
  "mission_timeout_s": 600
}

Never automatically un-pause, clear errors or release an e-stop on the MiR:
those states exist because a person or the safety system decided to stop.
"""

import base64
import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from drivers.base import Driver, cancelled, done, failed, robot_state

logger = logging.getLogger("driver.mir")

STATE_READY = 3
STATE_PAUSE = 4
STATE_EXECUTING = 5
STATE_EMERGENCY_STOP = 10
STATE_MANUAL = 11
STATE_ERROR = 12


class MiRDriver(Driver):
    actions = ("navigate",)

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        host = cfg["host"]
        port = int(cfg.get("port", 80))
        self._base = f"http://{host}:{port}" + cfg.get("api_base", "/api/v2.0.0")
        self._timeout = float(cfg.get("request_timeout_s", 5))
        self._poll = float(cfg.get("poll_interval_s", 1.0))
        self._ready_wait = float(cfg.get("ready_wait_s", 15))
        self._max_comm_loss = float(cfg.get("max_comm_loss_s", 30))
        self._mission_timeout = float(cfg.get("mission_timeout_s", 600))
        self._locations: Dict[str, Dict[str, Any]] = cfg.get("locations", {})
        if cfg.get("auth_header"):
            self._auth = cfg["auth_header"]
        else:
            pw_hash = hashlib.sha256(cfg["password"].encode("utf-8")).hexdigest()
            token = base64.b64encode(f"{cfg['username']}:{pw_hash}".encode("utf-8")).decode("ascii")
            self._auth = f"Basic {token}"
        self._current_queue_id: Optional[int] = None
        self._lock = threading.Lock()

    # ---- HTTP -------------------------------------------------------------------
    def _request(self, method: str, path: str, body: Any = None) -> Tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self._base + path, data=data, method=method, headers={
            "Authorization": self._auth,
            "Accept-Language": "en_US",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return e.code, raw.decode("utf-8", "replace")

    def _status(self) -> Dict[str, Any]:
        code, body = self._request("GET", "/status")
        if code != 200 or not isinstance(body, dict):
            raise ConnectionError(f"GET /status -> HTTP {code}: {body}")
        return body

    @staticmethod
    def _describe(status: Dict[str, Any]) -> str:
        text = f"{status.get('state_text', '?')} (state {status.get('state_id')})"
        errors = status.get("errors") or []
        if errors:
            first = errors[0]
            text += f"; error {first.get('code')}: {first.get('description')}"
        return text

    # ---- Driver API -----------------------------------------------------------------
    def execute(self, subtask: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        zone = subtask.get("zone")
        loc = self._locations.get(zone)
        if not loc or not loc.get("mission_guid"):
            return failed(f"zone {zone!r} has no MiR mission_guid in this agent's config")

        # 1. Wait for Ready. Never un-pause / clear errors automatically.
        deadline = time.time() + self._ready_wait
        while True:
            try:
                status = self._status()
            except (OSError, ConnectionError) as e:
                status = None
                last = f"cannot reach MiR: {e}"
            if status is not None:
                sid = status.get("state_id")
                if sid == STATE_READY:
                    break
                last = self._describe(status)
                if sid in (STATE_EMERGENCY_STOP, STATE_MANUAL, STATE_ERROR):
                    return failed(f"MiR not ready: {last}")
            if time.time() > deadline:
                return failed(f"MiR not ready after {self._ready_wait:.0f}s: {last}")
            if cancel.wait(self._poll):
                return cancelled()

        # 2. Queue the mission.
        body = {"mission_id": loc["mission_guid"], "parameters": loc.get("parameters", [])}
        try:
            code, resp = self._request("POST", "/mission_queue", body)
        except OSError as e:
            return failed(f"POST /mission_queue failed: {e}")
        if code not in (200, 201) or not isinstance(resp, dict) or "id" not in resp:
            return failed(f"MiR rejected mission for {zone!r}: HTTP {code} {resp}")
        queue_id = resp["id"]
        with self._lock:
            self._current_queue_id = queue_id
        logger.info("MiR mission queued: zone=%s queue_id=%s", zone, queue_id)

        # 3. Follow it.
        try:
            started = time.time()
            last_ok = time.time()
            while True:
                if cancel.wait(self._poll):
                    self._abort(queue_id)
                    return cancelled()
                if time.time() - started > self._mission_timeout:
                    self._abort(queue_id)
                    return failed(f"mission to {zone!r} exceeded {self._mission_timeout:.0f}s")
                try:
                    code, entry = self._request("GET", f"/mission_queue/{queue_id}")
                except OSError as e:
                    if time.time() - last_ok > self._max_comm_loss:
                        return failed(f"lost contact with MiR for >{self._max_comm_loss:.0f}s: {e}")
                    continue
                if code != 200 or not isinstance(entry, dict):
                    if code == 404:
                        return failed(f"mission {queue_id} disappeared from the MiR queue (deleted on robot?)")
                    if time.time() - last_ok > self._max_comm_loss:
                        return failed(f"GET mission_queue/{queue_id} -> HTTP {code}")
                    continue
                last_ok = time.time()
                state = entry.get("state")
                if state == "Done":
                    return done({"mission_queue_id": queue_id})
                if state in ("Pending", "Executing"):
                    try:
                        status = self._status()
                    except (OSError, ConnectionError):
                        continue
                    if status.get("state_id") in (STATE_EMERGENCY_STOP, STATE_ERROR):
                        self._abort(queue_id)  # robot must not resume on its own when released
                        return failed(f"MiR stopped during mission to {zone!r}: {self._describe(status)}")
                    continue
                if state == "Aborted":
                    detail = entry.get("message") or ""
                    try:
                        detail = f"{detail} [{self._describe(self._status())}]"
                    except Exception:
                        pass
                    return failed(f"MiR mission to {zone!r} aborted {detail}".strip())
        finally:
            with self._lock:
                self._current_queue_id = None

    def _abort(self, queue_id: int):
        try:
            code, body = self._request("DELETE", f"/mission_queue/{queue_id}")
            logger.warning("MiR abort mission %s -> HTTP %s", queue_id, code)
        except OSError as e:
            logger.error("MiR abort mission %s failed: %s", queue_id, e)

    def cancel(self) -> None:
        with self._lock:
            queue_id = self._current_queue_id
        if queue_id is not None:
            self._abort(queue_id)

    def get_state(self) -> Dict[str, Any]:
        try:
            status = self._status()
        except Exception as e:
            return robot_state(False, f"cannot reach MiR: {e}")
        sid = status.get("state_id")
        with self._lock:
            executing_ours = self._current_queue_id is not None
        ready = sid == STATE_READY or (sid == STATE_EXECUTING and executing_ours)
        battery = status.get("battery_percentage")
        return robot_state(ready, self._describe(status), estop=(sid == STATE_EMERGENCY_STOP),
                           battery=float(battery) if isinstance(battery, (int, float)) else None)
