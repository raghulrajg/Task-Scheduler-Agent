"""
drivers/ur.py
-------------
Universal Robots e-Series / CB3 (PolyScope 5.x) arms. Handles `pick` and
`place` by running a PolyScope program per (action, location) through the
Dashboard Server (TCP 29999), and reads the program's outcome from a Modbus
general-purpose register (TCP 502, registers 128-255).

Why programs and not raw URScript moves from here: the pick/place motion,
gripper handling, part-present check and (for a mobile manipulator) marker
based re-localisation of the arm relative to the station belong in a
program taught and tested on the teach pendant. This driver only starts it,
supervises it and reads its verdict.

Program contract (put this in every pick/place .urp):
    BeforeStart:  write_port_register(128, 0)          # driver also clears it
    ... motion, gripper, part-present check ...
    on success:   write_port_register(128, 1)
    on failure:   write_port_register(128, 2)          # e.g. no part in gripper
                  (then end the program; do not leave it waiting on a popup)

Robot must be in Remote Control mode (pendant) for load/play/stop.

Config:
{
  "type": "ur",
  "host": "192.168.12.21",
  "dashboard_port": 29999,
  "modbus_port": 502,
  "programs": {
    "pick":  {"manufacturing_1": "pick_manufacturing_1.urp"},
    "place": {"package_area": "place_package.urp", "waste_area": "place_waste.urp"}
  },
  "result_register": 128,          # null disables result checking (NOT recommended)
  "program_timeout_s": 110,
  "poll_interval_s": 0.25,
  "require_remote_control": true
}
"""

import logging
import socket
import struct
import threading
import time
from typing import Any, Dict, Optional

from drivers.base import Driver, cancelled, done, failed, robot_state

logger = logging.getLogger("driver.ur")

ESTOP_STATUSES = {"SYSTEM_EMERGENCY_STOP", "ROBOT_EMERGENCY_STOP"}
RESULT_OK, RESULT_FAIL = 1, 2


class DashboardClient:
    """Minimal line-based client for the UR Dashboard Server."""

    def __init__(self, host: str, port: int = 29999, timeout: float = 5.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: Optional[socket.socket] = None
        self._file = None
        self._lock = threading.Lock()

    def _connect(self):
        self.close()
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        self._file = self._sock.makefile("r", encoding="utf-8", newline="\n")
        banner = self._file.readline()
        if not banner:
            raise ConnectionError("dashboard server closed the connection")
        logger.debug("Dashboard: %s", banner.strip())

    def close(self):
        for obj in (self._file, self._sock):
            try:
                if obj:
                    obj.close()
            except OSError:
                pass
        self._file = self._sock = None

    def cmd(self, text: str) -> str:
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._connect()
                    self._sock.sendall((text + "\n").encode("utf-8"))
                    line = self._file.readline()
                    if not line:
                        raise ConnectionError("dashboard server closed the connection")
                    return line.strip()
                except OSError:
                    self.close()
                    if attempt == 2:
                        raise
        raise ConnectionError("unreachable")


class ModbusTCP:
    """Just enough Modbus TCP for UR general-purpose registers (FC3 / FC6)."""

    def __init__(self, host: str, port: int = 502, timeout: float = 3.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._tid = 0
        self._lock = threading.Lock()

    def _transact(self, pdu: bytes) -> bytes:
        with self._lock:
            self._tid = (self._tid + 1) & 0xFFFF
            header = struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, 1)
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
                s.settimeout(self.timeout)
                s.sendall(header + pdu)
                head = self._recv_exact(s, 7)
                tid, proto, length, _unit = struct.unpack(">HHHB", head)
                body = self._recv_exact(s, length - 1)
        if tid != self._tid or proto != 0:
            raise ConnectionError("Modbus: mismatched response header")
        if body[0] & 0x80:
            raise ConnectionError(f"Modbus exception code {body[1]} for function {body[0] & 0x7F}")
        return body

    @staticmethod
    def _recv_exact(s: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Modbus: connection closed")
            buf += chunk
        return buf

    def read_register(self, address: int) -> int:
        body = self._transact(struct.pack(">BHH", 3, address, 1))
        if body[0] != 3 or body[1] != 2:
            raise ConnectionError(f"Modbus: unexpected FC3 response {body!r}")
        return struct.unpack(">H", body[2:4])[0]

    def write_register(self, address: int, value: int):
        body = self._transact(struct.pack(">BHH", 6, address, value & 0xFFFF))
        if body[0] != 6:
            raise ConnectionError(f"Modbus: unexpected FC6 response {body!r}")


class URDriver(Driver):
    actions = ("pick", "place")

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        host = cfg["host"]
        self._dash = DashboardClient(host, int(cfg.get("dashboard_port", 29999)),
                                     float(cfg.get("request_timeout_s", 5)))
        self._result_reg = cfg.get("result_register", 128)
        self._modbus = ModbusTCP(host, int(cfg.get("modbus_port", 502))) if self._result_reg is not None else None
        self._programs: Dict[str, Dict[str, str]] = cfg.get("programs", {})
        self._timeout = float(cfg.get("program_timeout_s", 110))
        self._poll = float(cfg.get("poll_interval_s", 0.25))
        self._require_remote = bool(cfg.get("require_remote_control", True))
        if self._result_reg is None:
            logger.warning("UR result_register disabled: a missed grasp will be reported as success")

    # ---- helpers --------------------------------------------------------------------
    def _value(self, command: str, prefix: str) -> str:
        """e.g. _value('robotmode', 'Robotmode:') -> 'RUNNING'"""
        resp = self._dash.cmd(command)
        return resp[len(prefix):].strip() if resp.startswith(prefix) else resp

    def _safety(self) -> str:
        value = self._value("safetystatus", "Safetystatus:")
        if "could not understand" in value.lower():  # PolyScope < 5.4
            value = self._value("safetymode", "Safetymode:")
        return value

    def _program_state(self) -> str:
        return self._dash.cmd("programState").split(" ")[0].upper()

    def _remote_control(self) -> Optional[bool]:
        resp = self._dash.cmd("is in remote control").strip().lower()
        if resp in ("true", "false"):
            return resp == "true"
        return None  # older software without this command

    def _preflight(self) -> Optional[str]:
        mode = self._value("robotmode", "Robotmode:")
        if mode != "RUNNING":
            return f"robot mode is {mode}, expected RUNNING (arm powered and brakes released)"
        safety = self._safety()
        if safety != "NORMAL":
            return f"safety status is {safety}"
        if self._require_remote and self._remote_control() is False:
            return "robot is in Local control; switch the pendant to Remote Control"
        state = self._program_state()
        if state != "STOPPED":
            return f"another program is {state}"
        return None

    def _stop(self):
        try:
            logger.warning("UR stop -> %s", self._dash.cmd("stop"))
        except OSError as e:
            logger.error("UR stop failed: %s", e)

    # ---- Driver API -------------------------------------------------------------------
    def execute(self, subtask: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        action, zone = subtask["action"], subtask.get("zone")
        program = self._programs.get(action, {}).get(zone)
        if not program:
            return failed(f"no UR program configured for {action} at {zone!r}")
        try:
            problem = self._preflight()
            if problem:
                return failed(f"UR not ready: {problem}")
            if self._modbus:
                self._modbus.write_register(self._result_reg, 0)

            resp = self._dash.cmd(f"load {program}")
            if not resp.startswith("Loading program"):
                return failed(f"UR load {program}: {resp}")
            resp = self._dash.cmd("play")
            if not resp.startswith("Starting program"):
                return failed(f"UR play {program}: {resp}")
            logger.info("UR running %s for %s", program, subtask.get("subtask_id"))

            started = time.time()
            saw_running = False
            paused_logged = False
            while True:
                if cancel.wait(self._poll):
                    self._stop()
                    return cancelled()
                if time.time() - started > self._timeout:
                    self._stop()
                    return failed(f"{program} did not finish within {self._timeout:.0f}s")
                state = self._program_state()
                if state == "PLAYING":
                    saw_running = True
                elif state == "PAUSED":
                    saw_running = True
                    safety = self._safety()
                    if safety != "NORMAL":
                        self._stop()
                        return failed(f"{program} interrupted: safety status {safety}")
                    if not paused_logged:
                        logger.warning("UR program %s is PAUSED (pendant/popup?)", program)
                        paused_logged = True
                elif state == "STOPPED":
                    if saw_running or time.time() - started > 2.0:
                        break

            safety = self._safety()
            if safety != "NORMAL":
                return failed(f"{program} ended with safety status {safety}")
            if not self._modbus:
                return done({"program": program, "verified": False})
            value = self._modbus.read_register(self._result_reg)
            if value == RESULT_OK:
                return done({"program": program, "verified": True})
            if value == RESULT_FAIL:
                return failed(f"{program} reported failure (register {self._result_reg}=2)")
            return failed(f"{program} stopped without reporting a result (register {self._result_reg}={value})")
        except OSError as e:
            return failed(f"lost connection to UR controller: {e}")

    def cancel(self) -> None:
        self._stop()

    def get_state(self) -> Dict[str, Any]:
        try:
            mode = self._value("robotmode", "Robotmode:")
            safety = self._safety()
            remote = self._remote_control()
        except OSError as e:
            return robot_state(False, f"cannot reach UR dashboard: {e}")
        ready = mode == "RUNNING" and safety == "NORMAL" and (remote is not False or not self._require_remote)
        detail = f"mode={mode} safety={safety} remote={remote}"
        return robot_state(ready, detail, estop=safety in ESTOP_STATUSES)

    def close(self) -> None:
        self._dash.close()
