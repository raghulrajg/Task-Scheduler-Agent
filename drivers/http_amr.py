"""
drivers/http_amr.py
-------------------
Mobile robots that are driven from a web dashboard. Nothing here is
vendor-specific: the endpoints, request bodies and status fields come from
the agent config, so once you have captured what the robot's own web UI
sends (see docs/AMR_API_CAPTURE.md and tools/har_summary.py), you only fill
in JSON -- no code changes.

Handles the `navigate` action:
    1. pre-check status (no e-stop, not already moving, optional ready flag)
    2. send the "navigate" request for the location's id/name
    3. poll the "status" request until the nav state says arrived / failed
    4. on cancel or timeout send the "cancel" request

Guards against the classic web-API traps:
    * a stale "arrived"/"failed" left over from the previous goal is ignored
      until the robot has been seen moving (or settle_s has passed and the
      reported target is ours);
    * optional target check: arrival only counts if the robot reports the
      location we asked for;
    * session/token login with automatic re-login on 401/403;
    * short network drops are tolerated up to max_comm_loss_s.

Config (every path is a dotted path into the JSON reply, list items by
index, e.g. "data.tasks.0.state"; {placeholders} are filled from the
location entry plus {zone} and {subtask_id}):
{
  "type": "http_amr",
  "base_url": "http://192.168.10.10",
  "headers": {"Content-Type": "application/json"},
  "login": {                                           # optional
    "method": "POST", "path": "/api/login",
    "json": {"username": "${AMR_USER}", "password": "${AMR_PASSWORD}"},
    "token_path": "data.token",                        # omit if the UI uses a session cookie
    "token_header": "Authorization", "token_prefix": "Bearer "
  },
  "locations": {
    "manufacturing_1": {"id": "station_A"},            # whatever the robot calls that drop point
    "quality_station": {"id": "station_B"}
  },
  "navigate": {"method": "POST", "path": "/api/nav/goto", "json": {"point": "{id}"},
               "ok_path": "code", "ok_values": [0]},   # optional reply check
  "cancel":   {"method": "POST", "path": "/api/nav/cancel"},
  "status":   {"method": "GET",  "path": "/api/robot/status"},
  "fields": {
    "nav_state": "data.nav.state",
    "running_values": ["RUNNING"],
    "arrived_values": ["SUCCEEDED"],
    "failed_values":  ["FAILED", "CANCELED"],
    "target": "data.nav.target",                       # optional, compared with the location "id"
    "battery": "data.battery", "battery_scale": 1,     # use 100 if the robot reports 0..1
    "estop": "data.emergency", "estop_values": [true],
    "error": "data.error_msg",                         # optional, copied into failure messages
    "ready": null, "ready_values": [true]              # optional
  },
  "poll_interval_s": 0.5,
  "settle_s": 2,
  "start_timeout_s": 15,
  "mission_timeout_s": 280,
  "max_comm_loss_s": 30,
  "request_timeout_s": 5
}
"""

import http.cookiejar
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from drivers.base import Driver, cancelled, done, failed, robot_state
from drivers.util import dig, matches, render

logger = logging.getLogger("driver.http_amr")


class HttpAmrDriver(Driver):
    actions = ("navigate",)

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self._base = cfg["base_url"].rstrip("/")
        self._headers = {"Accept": "application/json", **cfg.get("headers", {})}
        self._login_cfg = cfg.get("login")
        self._locations: Dict[str, Dict[str, Any]] = cfg.get("locations", {})
        self._nav = cfg["navigate"]
        self._cancel_req = cfg.get("cancel")
        self._status_req = cfg["status"]
        f = cfg.get("fields", {})
        self._f_state = f["nav_state"]
        self._running = f.get("running_values", [])
        self._arrived = f.get("arrived_values", [])
        self._failed = f.get("failed_values", [])
        self._f_target = f.get("target")
        self._f_battery = f.get("battery")
        self._battery_scale = float(f.get("battery_scale", 1))
        self._f_estop = f.get("estop")
        self._estop_values = f.get("estop_values", [True])
        self._f_error = f.get("error")
        self._f_ready = f.get("ready")
        self._ready_values = f.get("ready_values", [True])
        self._poll = float(cfg.get("poll_interval_s", 0.5))
        self._settle = float(cfg.get("settle_s", 2))
        self._start_timeout = float(cfg.get("start_timeout_s", 15))
        self._mission_timeout = float(cfg.get("mission_timeout_s", 280))
        self._max_comm_loss = float(cfg.get("max_comm_loss_s", 30))
        self._timeout = float(cfg.get("request_timeout_s", 5))
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._token_headers: Dict[str, str] = {}
        self._logged_in = self._login_cfg is None
        self._lock = threading.Lock()
        self._active = False
        self._ctx: Optional[Dict[str, Any]] = None

    # ---- HTTP ----------------------------------------------------------------------------
    def _raw(self, spec: Dict[str, Any], ctx: Optional[Dict[str, Any]]) -> Tuple[int, Any]:
        if ctx is not None:  # only navigate/cancel are templates; never touch login secrets
            spec = render(spec, ctx)
        method = spec.get("method", "GET").upper()
        url = self._base + spec["path"]
        if spec.get("query"):
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(spec["query"])
        data = None
        headers = {**self._headers, **self._token_headers, **spec.get("headers", {})}
        if "json" in spec:
            data = json.dumps(spec["json"]).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif "form" in spec:
            data = urllib.parse.urlencode(spec["form"]).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                status, body = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read()
        try:
            return status, json.loads(body) if body else None
        except json.JSONDecodeError:
            return status, body.decode("utf-8", "replace")

    def _login(self):
        lc = self._login_cfg
        status, body = self._raw(lc, None)
        if not 200 <= status < 300:
            raise ConnectionError(f"login failed: HTTP {status} {str(body)[:200]}")
        if lc.get("token_path"):
            token = dig(body, lc["token_path"])
            if not token:
                raise ConnectionError(f"login reply has no token at '{lc['token_path']}'")
            self._token_headers = {lc.get("token_header", "Authorization"): f"{lc.get('token_prefix', 'Bearer ')}{token}"}
        self._logged_in = True
        logger.info("Logged in to AMR web API")

    def _request(self, spec: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        if not self._logged_in:
            self._login()
        status, body = self._raw(spec, ctx)
        if status in (401, 403) and self._login_cfg:
            self._logged_in = False
            self._login()
            status, body = self._raw(spec, ctx)
        return status, body

    def _status(self) -> Dict[str, Any]:
        code, body = self._request(self._status_req)
        if not 200 <= code < 300:
            raise ConnectionError(f"status request -> HTTP {code}: {str(body)[:200]}")
        return body

    # ---- interpretation --------------------------------------------------------------------
    def _estop(self, st) -> bool:
        return bool(self._f_estop) and matches(dig(st, self._f_estop), self._estop_values)

    def _error_text(self, st) -> str:
        if not self._f_error:
            return ""
        err = dig(st, self._f_error)
        return f" ({err})" if err not in (None, "", 0) else ""

    def _target_ok(self, st, loc) -> bool:
        if not self._f_target:
            return True
        want = loc.get("id", loc.get("name"))
        return dig(st, self._f_target) == want

    # ---- Driver API ------------------------------------------------------------------------
    def execute(self, subtask: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        zone = subtask.get("zone")
        loc = self._locations.get(zone)
        if loc is None:
            return failed(f"zone {zone!r} has no entry in this agent's 'locations'")
        ctx = {"zone": zone, "subtask_id": subtask.get("subtask_id", ""), **loc}

        try:
            st = self._status()
        except Exception as e:
            return failed(f"cannot read AMR status: {e}")
        if self._estop(st):
            return failed(f"AMR emergency stop active{self._error_text(st)}")
        if matches(dig(st, self._f_state), self._running):
            return failed("AMR is already executing a navigation goal (started from the web UI?)")
        if self._f_ready and not matches(dig(st, self._f_ready), self._ready_values):
            return failed(f"AMR not ready ({self._f_ready}={dig(st, self._f_ready)!r}){self._error_text(st)}")

        try:
            code, body = self._request(self._nav, ctx)
        except Exception as e:
            return failed(f"navigate request failed: {e}")
        if not 200 <= code < 300:
            return failed(f"AMR rejected navigate to {zone!r}: HTTP {code} {str(body)[:200]}")
        if self._nav.get("ok_path") and not matches(dig(body, self._nav["ok_path"]), self._nav.get("ok_values", [])):
            return failed(f"AMR rejected navigate to {zone!r}: {str(body)[:200]}")
        with self._lock:
            self._active = True
            self._ctx = ctx
        logger.info("AMR navigating to %s (%s)", zone, loc)

        try:
            started = last_ok = time.time()
            saw_running = False
            while True:
                if cancel.wait(self._poll):
                    self._send_cancel()
                    return cancelled()
                elapsed = time.time() - started
                if elapsed > self._mission_timeout:
                    self._send_cancel()
                    return failed(f"navigation to {zone!r} exceeded {self._mission_timeout:.0f}s")
                try:
                    st = self._status()
                    last_ok = time.time()
                except Exception as e:
                    if time.time() - last_ok > self._max_comm_loss:
                        return failed(f"lost contact with AMR for >{self._max_comm_loss:.0f}s: {e}")
                    continue
                if self._estop(st):
                    self._send_cancel()  # must not resume on its own when the e-stop is released
                    return failed(f"AMR emergency stop during navigation to {zone!r}{self._error_text(st)}")
                state = dig(st, self._f_state)
                if matches(state, self._running):
                    saw_running = True
                    continue
                fresh = saw_running or (elapsed >= self._settle and self._target_ok(st, loc))
                if matches(state, self._failed) and fresh:
                    return failed(f"AMR navigation to {zone!r} failed: state={state!r}{self._error_text(st)}")
                if matches(state, self._arrived) and fresh:
                    if not self._target_ok(st, loc):
                        return failed(f"AMR reports arrival at {dig(st, self._f_target)!r}, expected {loc}")
                    return done({"state": state})
                if not saw_running and elapsed > self._start_timeout:
                    self._send_cancel()
                    return failed(f"AMR did not start moving within {self._start_timeout:.0f}s "
                                  f"(state={state!r}){self._error_text(st)}")
        finally:
            with self._lock:
                self._active = False

    def _send_cancel(self):
        if not self._cancel_req:
            logger.error("No 'cancel' request configured -- cannot stop the AMR remotely!")
            return
        try:
            code, body = self._request(self._cancel_req, self._ctx or {})
            logger.warning("AMR cancel -> HTTP %s %s", code, str(body)[:200])
        except Exception as e:
            logger.error("AMR cancel failed: %s", e)

    def cancel(self) -> None:
        with self._lock:
            active = self._active
        if active:
            self._send_cancel()

    def get_state(self) -> Dict[str, Any]:
        try:
            st = self._status()
        except Exception as e:
            return robot_state(False, f"cannot reach AMR API: {e}")
        estop = self._estop(st)
        state = dig(st, self._f_state)
        with self._lock:
            active = self._active
        ready = not estop and (active or not matches(state, self._running))
        if self._f_ready:
            ready = ready and matches(dig(st, self._f_ready), self._ready_values)
        battery = dig(st, self._f_battery) if self._f_battery else None
        battery = round(float(battery) * self._battery_scale, 1) if isinstance(battery, (int, float)) \
            and not isinstance(battery, bool) else None
        return robot_state(ready, f"nav_state={state!r}{self._error_text(st)}", estop=estop, battery=battery)
