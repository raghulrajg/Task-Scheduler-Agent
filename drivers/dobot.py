"""
drivers/dobot.py
----------------
Dobot CR / Nova series cobots (tested design target: Nova 2) over Dobot's
TCP/IP remote-control protocol, dashboard port 29999. Handles `pick` and
`place` by running a DobotStudio Pro *project* per (action, location) and
supervising it through RobotMode().

Why projects and not raw MovJ/MovL from here: the approach, grasp, gripper
check and (on a mobile base) visual/marker re-localisation belong in a
project that is taught and tested in DobotStudio Pro. This driver starts it,
watches it, stops it on cancel, and reads its verdict.

Protocol (Dobot TCP/IP Remote Control Interface Guide):
    send   Command(args)\n
    reply  ErrorID,{values},Command(args);      ErrorID 0 = accepted
    RobotMode(): 1 INIT, 2 BRAKE_OPEN, 3 POWEROFF, 4 DISABLED, 5 ENABLE (idle),
                 6 BACKDRIVE (drag), 7 RUNNING, 8 SINGLE_MOVE (jog), 9 ERROR,
                 10 PAUSE, 11 COLLISION

Firmware generations ("protocol" in config):
    v4  DobotStudio Pro 4.x controllers. RunScript("name"), Stop(),
        bus registers SetOutputInt/GetOutputInt. Controllers on 4.6+ also need
        RequestControl() (only accepted while the arm is disabled/powered off).
    v3  older controllers. RunScript(name), StopScript(). Set the robot to
        "TCP/IP secondary development" mode in DobotStudio Pro first.

Project result contract -- put this at the end of every pick/place project:
    v4, result.kind = "output_int" (default):
        success:  SetOutputInt(0, 1)
        failure:  SetOutputInt(0, 2)        -- e.g. gripper didn't detect the part
    The driver writes 0 before RunScript and reads it after the project ends.
    (Verify on your controller that bus output registers work without a
    fieldbus configured: run  robot_agent.py --config ... --check  and see
    "result_channel": "ok".)

Config:
{
  "type": "dobot",
  "host": "192.168.5.1",               # LAN1 default on many controllers
  "port": 29999,
  "protocol": "v4",                    # or "v3"
  "projects": {
    "pick":  {"manufacturing_1": "pick_manufacturing_1"},
    "place": {"package_area": "place_package_area", "waste_area": "place_waste_area"}
  },
  "result": {"kind": "output_int", "address": 0},   # or {"kind": "none"}
  "request_control": true,             # v4.6+: send RequestControl() at start-up
  "auto_enable": false,                # true: EnableRobot() if the arm is disabled
  "enable_args": "",                   # e.g. "1.5,0,0,30" (payload kg, centre of mass mm)
  "travel_joints": [0, -30, 120, 0, 90, 0],  # optional: arm pose that is safe for driving
  "travel_tolerance_deg": 2.0,
  "project_timeout_s": 110,
  "poll_interval_s": 0.25,
  "request_timeout_s": 5
}

Travel pose (mobile manipulator): when travel_joints is set, a project only
counts as done if it leaves the arm in that pose, and the heartbeat reports
travel_pose true/false -- the base agent (interlock_with ["arm"]) refuses
to drive while it is false. Teach every project to end in this pose.
"""

import logging
import re
import socket
import threading
import time
from typing import Any, Dict, Optional, Tuple

from drivers.base import Driver, cancelled, done, failed, robot_state

logger = logging.getLogger("driver.dobot")

MODE_NAMES = {1: "INIT", 2: "BRAKE_OPEN", 3: "POWEROFF", 4: "DISABLED", 5: "ENABLE", 6: "BACKDRIVE",
              7: "RUNNING", 8: "SINGLE_MOVE", 9: "ERROR", 10: "PAUSE", 11: "COLLISION"}
MODE_ENABLE, MODE_RUNNING, MODE_ERROR, MODE_PAUSE, MODE_COLLISION = 5, 7, 9, 10, 11
MODE_DISABLED, MODE_POWEROFF = 4, 3
ERR_UNKNOWN_COMMAND = -10000
RESULT_OK, RESULT_FAIL = 1, 2
_REPLY = re.compile(r"^\s*(-?\d+)\s*,\s*\{(.*?)\}\s*,", re.S)


class DobotError(ConnectionError):
    pass


class DobotDashboard:
    """Line client for port 29999. One persistent connection, one command at a time."""

    def __init__(self, host: str, port: int = 29999, timeout: float = 5.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _connect(self):
        self.close()
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)

    def _read_reply(self) -> str:
        buf = b""
        while not buf.rstrip().endswith(b";"):
            chunk = self._sock.recv(4096)
            if not chunk:
                raise DobotError("controller closed the connection")
            buf += chunk
        return buf.decode("utf-8", "replace").strip()

    def cmd(self, text: str) -> Tuple[int, str, str]:
        """Returns (error_id, values_inside_braces, raw_reply)."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._connect()
                    self._sock.sendall((text + "\n").encode("utf-8"))
                    raw = self._read_reply()
                    break
                except OSError as e:
                    self.close()
                    if attempt == 2:
                        raise DobotError(f"{self.host}:{self.port}: {e}") from e
        m = _REPLY.match(raw)
        if not m:
            raise DobotError(f"unparseable reply to {text}: {raw!r}")
        return int(m.group(1)), m.group(2).strip(), raw


class DobotDriver(Driver):
    actions = ("pick", "place")

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self._dash = DobotDashboard(cfg["host"], int(cfg.get("port", 29999)),
                                    float(cfg.get("request_timeout_s", 5)))
        self._protocol = str(cfg.get("protocol", "v4")).lower()
        if self._protocol not in ("v3", "v4"):
            raise ValueError("dobot protocol must be 'v3' or 'v4'")
        self._projects: Dict[str, Dict[str, str]] = cfg.get("projects", {})
        default_result = {"kind": "output_int", "address": 0} if self._protocol == "v4" else {"kind": "none"}
        self._result = cfg.get("result", default_result)
        if self._result.get("kind") not in ("output_int", "none"):
            raise ValueError("dobot result.kind must be 'output_int' or 'none'")
        if self._result["kind"] == "none":
            logger.warning("Dobot result channel disabled: a missed grasp will be reported as success")
        self._timeout = float(cfg.get("project_timeout_s", 110))
        self._poll = float(cfg.get("poll_interval_s", 0.25))
        self._auto_enable = bool(cfg.get("auto_enable", False))
        self._enable_args = str(cfg.get("enable_args", ""))
        self._request_control = bool(cfg.get("request_control", self._protocol == "v4"))
        self._control_requested = False
        self._travel = cfg.get("travel_joints")
        if self._travel is not None and (not isinstance(self._travel, list) or len(self._travel) != 6
                                         or not all(isinstance(j, (int, float)) for j in self._travel)):
            raise ValueError("travel_joints must be a list of 6 joint angles in degrees, "
                             "e.g. [0, -30, 120, 0, 90, 0] (read them with tools/dobot_console.py GetAngle())")
        self._travel_tol = float(cfg.get("travel_tolerance_deg", 2.0))

    # ---- protocol helpers -------------------------------------------------------------
    def _ok(self, text: str) -> str:
        err, values, raw = self._dash.cmd(text)
        if err != 0:
            raise DobotError(f"{text} -> ErrorID {err} ({raw})")
        return values

    def _mode(self) -> int:
        return int(self._ok("RobotMode()"))

    def _errors(self) -> str:
        try:
            return self._ok("GetErrorID()")
        except DobotError as e:
            return f"(GetErrorID failed: {e})"

    def _stop(self):
        try:
            self._dash.cmd("Stop()" if self._protocol == "v4" else "StopScript()")
        except DobotError as e:
            logger.error("Dobot stop failed: %s", e)

    def _run_cmd(self, project: str) -> str:
        if self._protocol == "v4":
            return f'RunScript("{project}")'
        return f"RunScript({project})"

    def _ensure_control(self):
        """v4.6+: TCP commands only work after RequestControl(), which the
        controller accepts only while the arm is disabled. Older firmware
        answers -10000 (unknown command), which is fine."""
        if not self._request_control or self._control_requested:
            return
        err, _, raw = self._dash.cmd("RequestControl()")
        if err in (0, ERR_UNKNOWN_COMMAND):
            self._control_requested = True
            logger.info("Dobot RequestControl -> %s", raw)
        else:
            logger.warning("Dobot RequestControl refused (%s). If commands fail, disable the arm "
                           "and restart the agent, or switch to TCP mode in DobotStudio Pro.", raw)
            self._control_requested = True  # don't spam; the preflight reports the real problem

    def _in_travel_pose(self) -> Optional[bool]:
        if self._travel is None:
            return None
        joints = [float(x) for x in self._ok("GetAngle()").split(",")]
        return all(abs(j - t) <= self._travel_tol for j, t in zip(joints, self._travel))

    def _write_result(self, value: int):
        if self._result["kind"] == "output_int":
            self._ok(f"SetOutputInt({int(self._result.get('address', 0))},{value})")

    def _read_result(self) -> Optional[int]:
        if self._result["kind"] == "output_int":
            return int(float(self._ok(f"GetOutputInt({int(self._result.get('address', 0))})")))
        return None

    def _preflight(self) -> Optional[str]:
        self._ensure_control()
        mode = self._mode()
        if mode in (MODE_DISABLED, MODE_POWEROFF) and self._auto_enable:
            logger.warning("Dobot is %s -- auto_enable: EnableRobot(%s)", MODE_NAMES.get(mode), self._enable_args)
            self._ok(f"EnableRobot({self._enable_args})")
            end = time.time() + 15
            while time.time() < end and mode != MODE_ENABLE:
                time.sleep(0.5)
                mode = self._mode()
        if mode == MODE_ENABLE:
            return None
        name = MODE_NAMES.get(mode, f"mode {mode}")
        if mode in (MODE_ERROR, MODE_COLLISION):
            return f"arm in {name}, alarms {self._errors()} (clear them on the teach pendant/DobotStudio)"
        if mode in (MODE_DISABLED, MODE_POWEROFF):
            return f"arm is {name}; enable it (or set auto_enable in the agent config)"
        if mode == MODE_RUNNING:
            return "another project or motion is running"
        return f"arm not idle ({name})"

    # ---- Driver API -------------------------------------------------------------------------
    def execute(self, subtask: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        action, zone = subtask["action"], subtask.get("zone")
        project = self._projects.get(action, {}).get(zone)
        if not project:
            return failed(f"no Dobot project configured for {action} at {zone!r}")
        try:
            problem = self._preflight()
            if problem:
                return failed(f"Dobot not ready: {problem}")
            self._write_result(0)
            err, _, raw = self._dash.cmd(self._run_cmd(project))
            if err != 0:
                return failed(f"RunScript {project} rejected: {raw}")
            logger.info("Dobot running project %s for %s", project, subtask.get("subtask_id"))

            started = time.time()
            saw_running = False
            paused_logged = False
            while True:
                if cancel.wait(self._poll):
                    self._stop()
                    return cancelled()
                if time.time() - started > self._timeout:
                    self._stop()
                    return failed(f"project {project} did not finish within {self._timeout:.0f}s")
                mode = self._mode()
                if mode == MODE_RUNNING:
                    saw_running = True
                elif mode == MODE_ENABLE:
                    if saw_running or time.time() - started > 2.0:
                        break
                elif mode in (MODE_ERROR, MODE_COLLISION):
                    errors = self._errors()
                    self._stop()
                    return failed(f"project {project} stopped: {MODE_NAMES[mode]}, alarms {errors}")
                elif mode == MODE_PAUSE:
                    saw_running = True
                    if not paused_logged:
                        logger.warning("Dobot project %s PAUSED (pendant / safety input?)", project)
                        paused_logged = True
                elif mode in (MODE_DISABLED, MODE_POWEROFF):
                    return failed(f"arm became {MODE_NAMES[mode]} during {project} (e-stop or power loss?)")

            value = self._read_result()
            if value not in (None, RESULT_OK):
                if value == RESULT_FAIL:
                    return failed(f"project {project} reported failure (result register = 2)")
                return failed(f"project {project} ended without reporting a result (register = {value})")
            if self._in_travel_pose() is False:
                return failed(f"project {project} finished but the arm is not in its travel pose")
            return done({"project": project, "verified": value == RESULT_OK})
        except (DobotError, ValueError) as e:
            return failed(f"Dobot communication error: {e}")

    def cancel(self) -> None:
        self._stop()

    def get_state(self) -> Dict[str, Any]:
        try:
            self._ensure_control()
            mode = self._mode()
        except DobotError as e:
            return robot_state(False, f"cannot reach Dobot: {e}")
        name = MODE_NAMES.get(mode, str(mode))
        detail = f"mode={name}"
        if mode in (MODE_ERROR, MODE_COLLISION):
            detail += f" alarms={self._errors()}"
        extra = {}
        try:
            travel = self._in_travel_pose()
        except (DobotError, ValueError):
            travel = False
        if travel is not None:
            extra["travel_pose"] = travel
            detail += f" travel_pose={travel}"
        ready = mode == MODE_ENABLE or (mode in (MODE_DISABLED, MODE_POWEROFF) and self._auto_enable)
        if travel is False:
            ready = False
        return robot_state(ready, detail, **extra)

    def check_result_channel(self) -> str:
        """Used by robot_agent --check: confirm the result register is usable."""
        if self._result["kind"] == "none":
            return "disabled"
        try:
            self._write_result(0)
            return "ok" if self._read_result() == 0 else "read-back mismatch"
        except DobotError as e:
            return f"NOT usable: {e}"

    def close(self) -> None:
        self._dash.close()
