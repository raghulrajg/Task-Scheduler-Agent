"""
drivers/onix.py
---------------
The ONIX mobile robot (the AMR under the Dobot), driven through the same
API its web app uses (protocol from amr_tools/amr_client.py):

    ws://<host>:4000/nav       free_nav (api 4002) {linear_x, linear_y, angular_z} = x, y, theta
                               path_nav (api 4003) {target_node}; cancel (api 4004)
    ws://<host>:7000/status    robot_status (api 7004, ~1 Hz): x, y, angle, confidence,
                               emergency, error_id, percentage, linear_speed, angular_speed
                               errors_warnings (api 7010)
    every WebSocket message is {"key": <api key>, "request": {...}}

navigate(zone):
    1. Read the target x, y, theta for `zone` from stations.json -- fresh, at
       the start of every move, so edits apply immediately.
    2. Refuse to start if the robot is e-stopped, not localized, in error,
       or already moving.
    3. Send the goal (free navigation to x, y, theta; or path navigation to
       the linked station if nav_mode = "station").
    4. Follow the live status and report DONE only when the robot is
       VERIFIED there: within xy/yaw tolerance of the target, stopped, and
       still localized, continuously for settle_s. Only then does the
       scheduler send the next step (e.g. the Dobot pick).
    5. Fail (and cancel the goal) on: e-stop, localization lost, a lasting
       robot error, no movement after start_timeout_s, stopped short of the
       target for stall_timeout_s, or mission_timeout_s.

The heartbeat reports where the robot is (at_location / at_locations, from
the live pose and stations.json), which locations have coordinates
(locations) and the pose. The scheduler only accepts jobs whose locations
have coordinates, and the arm agent ("require_base_at_zone": "amr") only
picks/places when the base reports it is at that zone.

Config:
{
  "type": "onix",
  "host": "172.22.74.119",
  "stations_file": "stations.json",
  "nav_mode": "point",                 # "point" = x,y,theta (default)  |  "station" = path nav to the linked station
  "xy_tolerance_m": 0.10,              # defaults; stations.json defaults / per-location values override
  "yaw_tolerance_deg": 5,
  "settle_s": 1.5,
  "stationary_linear": 0.02,           # m/s below which the robot counts as stopped
  "stationary_angular": 0.02,          # rad/s
  "start_timeout_s": 15,
  "stall_timeout_s": 30,
  "mission_timeout_s": 280,
  "max_comm_loss_s": 15,
  "min_localization": 70,
  "arrival_drift_m": 0.05,             # after a verified arrival, the heartbeat keeps reporting that
  "arrival_drift_deg": 3,              #   location while the robot stays within this drift of it
  "api_key": null,                     # default: the key the robot's web app uses
  "ports": {"rest": 3000, "control": 4000, "status": 7000}
}
"""

import json
import logging
import math
import threading
import time
import urllib.request
from typing import Any, Dict, Optional

import stations as stations_mod
from drivers.base import Driver, cancelled, done, failed, robot_state

logger = logging.getLogger("driver.onix")

WEB_APP_API_KEY = "043ea683c1d0b7b5a9f584ef2e6c36079c62a7e7f0a76606778bab99"
API_FREE_NAV, API_PATH_NAV, API_CANCEL_NAV = "4002", "4003", "4004"
CH_STATUS, CH_ERRORS = "7004", "7010"
ERROR_LEVEL_ERROR = 2


class OnixAPI:
    """Minimal client for the robot's WebSocket/HTTP API (ports configurable)."""

    def __init__(self, host: str, api_key: str = WEB_APP_API_KEY, rest_port: int = 3000,
                 control_port: int = 4000, status_port: int = 7000, timeout: float = 5.0):
        import websocket  # websocket-client
        self._ws = websocket
        self.host, self.api_key, self.timeout = host, api_key, timeout
        self.rest_port, self.control_port, self.status_port = rest_port, control_port, status_port

    def _payload(self, request: Dict[str, Any]) -> str:
        return json.dumps({"key": self.api_key, "request": request})

    def request(self, port: int, path: str, request: Dict[str, Any], wait: float = 5.0) -> Optional[dict]:
        ws = self._ws.create_connection(f"ws://{self.host}:{port}{path}", timeout=wait)
        try:
            ws.send(self._payload(request))
            try:
                return json.loads(ws.recv())
            except self._ws.WebSocketTimeoutException:
                return None
        finally:
            ws.close()

    def navigate_to_point(self, x: float, y: float, theta: float) -> Optional[dict]:
        return self.request(self.control_port, "/nav", {
            "api_id": API_FREE_NAV, "free_nav": {"linear_x": x, "linear_y": y, "angular_z": theta}})

    def navigate_to_station(self, station: str) -> Optional[dict]:
        return self.request(self.control_port, "/nav", {
            "api_id": API_PATH_NAV, "path_nav": {"target_node": station, "previous_node": "-1"}})

    def cancel(self) -> Optional[dict]:
        return self.request(self.control_port, "/nav", {"api_id": API_CANCEL_NAV, "action": "cancel"})

    def open_channel(self, api_id: str, timeout: float):
        ws = self._ws.create_connection(f"ws://{self.host}:{self.status_port}/status", timeout=timeout)
        ws.send(self._payload({"api_id": api_id}))
        return ws

    def next_data(self, ws) -> Any:
        """Next {status: true} message's data on an open channel (raises on timeout/close)."""
        while True:
            msg = json.loads(ws.recv())
            if msg.get("status") is True:
                return msg.get("data")

    def read(self, api_id: str, timeout: float = 3.0) -> Any:
        ws = self.open_channel(api_id, timeout)
        try:
            return self.next_data(ws)
        finally:
            ws.close()

    def live_map(self) -> Dict[str, Any]:
        with urllib.request.urlopen(f"http://{self.host}:{self.rest_port}/map/live", timeout=60) as r:
            body = json.loads(r.read())
        return body.get("data", body) if isinstance(body, dict) else body


class OnixDriver(Driver):
    actions = ("navigate",)

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        ports = cfg.get("ports", {})
        self._api = OnixAPI(cfg["host"], cfg.get("api_key") or WEB_APP_API_KEY,
                            int(ports.get("rest", 3000)), int(ports.get("control", 4000)),
                            int(ports.get("status", 7000)), float(cfg.get("request_timeout_s", 5)))
        self._stations_file = cfg.get("stations_file", "stations.json")
        self._mode = cfg.get("nav_mode", "point")
        if self._mode not in ("point", "station"):
            raise ValueError("onix nav_mode must be 'point' or 'station'")
        self._xy_tol = float(cfg.get("xy_tolerance_m", stations_mod.DEFAULT_XY_TOLERANCE_M))
        self._yaw_tol_deg = float(cfg.get("yaw_tolerance_deg", stations_mod.DEFAULT_YAW_TOLERANCE_DEG))
        self._settle = float(cfg.get("settle_s", 1.5))
        self._still_lin = float(cfg.get("stationary_linear", 0.02))
        self._still_ang = float(cfg.get("stationary_angular", 0.02))
        self._start_timeout = float(cfg.get("start_timeout_s", 15))
        self._stall_timeout = float(cfg.get("stall_timeout_s", 30))
        self._mission_timeout = float(cfg.get("mission_timeout_s", 280))
        self._max_comm_loss = float(cfg.get("max_comm_loss_s", 15))
        self._min_loc = float(cfg.get("min_localization", 70))
        self._error_grace = float(cfg.get("error_grace_s", 3))
        self._drift_m = float(cfg.get("arrival_drift_m", 0.05))
        self._drift_rad = math.radians(float(cfg.get("arrival_drift_deg", 3)))
        self._lock = threading.Lock()
        self._active = False
        self._arrived: Optional[Dict[str, Any]] = None   # last verified arrival {"zone", "x", "y", "theta"}

    # ---- helpers -----------------------------------------------------------------------
    def _stations(self) -> Dict[str, Any]:
        data = stations_mod.load(self._stations_file)
        d = data.setdefault("defaults", {})
        d.setdefault("xy_tolerance_m", self._xy_tol)
        d.setdefault("yaw_tolerance_deg", self._yaw_tol_deg)
        return data

    def _errors_text(self) -> str:
        try:
            e = self._api.read(CH_ERRORS, timeout=2) or {}
            parts = [str(x) for x in (e.get("errors") or [])] + [f"warning: {x}" for x in (e.get("warnings") or [])]
            return f" [{'; '.join(parts)[:300]}]" if parts else ""
        except Exception:
            return ""

    def _moving(self, st: Dict[str, Any]) -> bool:
        return abs(float(st.get("linear_speed") or 0)) > self._still_lin or \
            abs(float(st.get("angular_speed") or 0)) > self._still_ang

    def _abort(self, why: str) -> Dict[str, Any]:
        try:
            self._api.cancel()
        except Exception as e:
            logger.error("ONIX cancel failed: %s", e)
        return failed(why + self._errors_text())

    # ---- Driver API --------------------------------------------------------------------------
    def execute(self, subtask: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        zone = subtask.get("zone")
        with self._lock:
            self._arrived = None      # from here on, only a new verified arrival counts
        try:
            data = self._stations()
            gx, gy, gt, xy_tol, yaw_tol = stations_mod.location_pose(data, zone)
        except FileNotFoundError:
            return failed(f"stations file {self._stations_file} not found")
        except stations_mod.LocationError as e:
            return failed(str(e))
        station = data["locations"][zone].get("station")
        if self._mode == "station" and not station:
            return failed(f"nav_mode 'station' but location {zone!r} is not linked to a station")

        try:
            st = self._api.read(CH_STATUS, timeout=5)
        except Exception as e:
            return failed(f"cannot read ONIX status: {e}")
        if st.get("emergency") == 1:
            return failed("ONIX emergency stop is pressed")
        if float(st.get("confidence") or 0) < self._min_loc:
            return failed(f"ONIX not localized ({float(st.get('confidence') or 0):.0f}% < {self._min_loc:.0f}%) "
                          f"-- relocate it in the web app first")
        if int(st.get("error_id") or 0) >= ERROR_LEVEL_ERROR:
            return failed("ONIX reports an error" + self._errors_text())
        if self._moving(st):
            return failed("ONIX is already moving (driven from the web app?)")

        target = f"{zone} (x={gx:.3f}, y={gy:.3f}, theta={math.degrees(gt):.1f} deg" + \
                 (f", station {station})" if station else ")")
        try:
            reply = (self._api.navigate_to_station(station) if self._mode == "station"
                     else self._api.navigate_to_point(gx, gy, gt))
        except Exception as e:
            return failed(f"sending the goal to ONIX failed: {e}")
        if isinstance(reply, dict) and reply.get("status") is False:
            return failed(f"ONIX rejected the goal {target}: {reply.get('message')} "
                          f"(error_code {reply.get('error_code')})")
        if reply is None:
            logger.warning("ONIX gave no reply to the goal; verifying by motion instead")
        logger.info("ONIX driving to %s", target)
        with self._lock:
            self._active = True
        try:
            outcome = self._follow(gx, gy, gt, xy_tol, yaw_tol, target, cancel)
        finally:
            with self._lock:
                self._active = False
        if outcome.get("state") == "done":
            r = outcome["result"]
            with self._lock:
                self._arrived = {"zone": zone, "x": r["x"], "y": r["y"], "theta": r["theta"]}
        return outcome

    def _follow(self, gx, gy, gt, xy_tol, yaw_tol, target, cancel) -> Dict[str, Any]:
        started = last_ok = last_motion = time.time()
        seen_motion = False
        settled_since = None
        error_since = None
        ws = None
        try:
            while True:
                if cancel.is_set():
                    self._abort("cancelled")
                    return cancelled()
                now = time.time()
                if now - started > self._mission_timeout:
                    return self._abort(f"did not reach {target} within {self._mission_timeout:.0f}s")
                try:
                    if ws is None:
                        ws = self._api.open_channel(CH_STATUS, timeout=3)
                    st = self._api.next_data(ws)
                    last_ok = now = time.time()
                except Exception as e:
                    if ws is not None:
                        try:
                            ws.close()
                        except Exception:
                            pass
                    ws = None
                    if time.time() - last_ok > self._max_comm_loss:
                        return self._abort(f"lost contact with ONIX for >{self._max_comm_loss:.0f}s: {e}")
                    if cancel.wait(0.5):
                        continue
                    continue

                if st.get("emergency") == 1:
                    return self._abort(f"emergency stop pressed while driving to {target}")
                conf = float(st.get("confidence") or 0)
                if conf < self._min_loc:
                    return self._abort(f"localization lost ({conf:.0f}%) while driving to {target}")
                if int(st.get("error_id") or 0) >= ERROR_LEVEL_ERROR:
                    error_since = error_since or now
                    if now - error_since >= self._error_grace:
                        return self._abort(f"ONIX error while driving to {target}")
                else:
                    error_since = None

                x, y, th = float(st.get("x") or 0), float(st.get("y") or 0), float(st.get("angle") or 0)
                dist, yaw = stations_mod.pose_error(x, y, th, gx, gy, gt)
                moving = self._moving(st)
                if moving:
                    last_motion, seen_motion = now, True
                at_goal = dist <= xy_tol and yaw <= yaw_tol

                if at_goal and not moving:
                    settled_since = settled_since or now
                    if now - settled_since >= self._settle:
                        logger.info("ONIX verified at %s: error %.3f m / %.1f deg", target, dist, math.degrees(yaw))
                        return done({"x": round(x, 4), "y": round(y, 4), "theta": round(th, 4),
                                     "error_m": round(dist, 4), "error_deg": round(math.degrees(yaw), 2),
                                     "verified": True})
                else:
                    settled_since = None

                if not moving and not at_goal:
                    if not seen_motion and now - started > self._start_timeout:
                        return self._abort(f"ONIX did not start moving towards {target} within "
                                           f"{self._start_timeout:.0f}s (goal not accepted?)")
                    if seen_motion and now - last_motion > self._stall_timeout:
                        return self._abort(f"ONIX stopped {dist:.2f} m / {math.degrees(yaw):.1f} deg away from "
                                           f"{target} for {self._stall_timeout:.0f}s (blocked, or tolerance "
                                           f"tighter than the robot's own goal tolerance?)")
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass

    def cancel(self) -> None:
        with self._lock:
            active = self._active
        if active:
            try:
                self._api.cancel()
            except Exception as e:
                logger.error("ONIX cancel failed: %s", e)

    def get_state(self) -> Dict[str, Any]:
        try:
            st = self._api.read(CH_STATUS, timeout=3)
        except Exception as e:
            return robot_state(False, f"cannot reach ONIX at {self._api.host}: {e}")
        conf = float(st.get("confidence") or 0)
        estop = st.get("emergency") == 1
        err = int(st.get("error_id") or 0) >= ERROR_LEVEL_ERROR
        x, y, th = float(st.get("x") or 0), float(st.get("y") or 0), float(st.get("angle") or 0)
        with self._lock:
            active = self._active
        try:
            data = self._stations()
            locations = stations_mod.assigned_locations(data)
            here = stations_mod.locations_at(data, x, y, th) if conf >= self._min_loc else []
        except (OSError, ValueError):
            locations, here = [], []
        with self._lock:
            arrived = self._arrived
        if arrived and conf >= self._min_loc and arrived["zone"] in locations:
            d, yaw = stations_mod.pose_error(x, y, th, arrived["x"], arrived["y"], arrived["theta"])
            if d <= self._drift_m and yaw <= self._drift_rad:
                # still where the last move was verified: that is the location
                # we are "at", even if another name shares the spot.
                here = [arrived["zone"]] + [z for z in here if z != arrived["zone"]]
        at = here[0] if here else None
        ready = conf >= self._min_loc and not estop and not err and (active or not self._moving(st))
        problems = [p for p, bad in (("NOT localized", conf < self._min_loc), ("e-stop", estop),
                                     ("error", err)) if bad]
        detail = (f"loc {conf:.0f}% pose ({x:.2f}, {y:.2f}, {math.degrees(th):.0f} deg) "
                  f"at {at or '-'}" + (f" [{', '.join(problems)}]" if problems else ""))
        battery = st.get("percentage")
        return robot_state(ready, detail, estop=estop,
                           battery=float(battery) if isinstance(battery, (int, float)) else None,
                           at_location=at, at_locations=here, locations=locations,
                           pose={"x": round(x, 3), "y": round(y, 3), "theta": round(th, 3)})
