"""
drivers/sim.py
--------------
Simulated hardware. Use it to validate the whole MQTT loop (scheduler,
agents, HMI) before touching a real robot, and in automated tests.

Config:
{
  "type": "sim",
  "actions": ["navigate"],                    # or ["pick","place"] / ["quality_check"]
  "durations_s": {"navigate": 2, "pick": 1},  # per action
  "fail_rate": {"navigate": 0.0},             # probability of a simulated failure
  "qc_pass_rate": 0.8,
  "battery_start": 100, "battery_per_task": 0.5,
  "seed": null,
  "report_location": false,                   # navigate: report at_location like the ONIX driver,
  "locations": null                           #   and these as the locations with coordinates
}
"""

import random
import threading
from typing import Any, Dict

from drivers.base import Driver, cancelled, done, failed, robot_state


class SimDriver(Driver):
    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        self.actions = tuple(cfg.get("actions", ("navigate", "pick", "place", "quality_check")))
        self._durations = {"navigate": 2.0, "pick": 1.0, "place": 1.0, "quality_check": 1.0,
                           **cfg.get("durations_s", {})}
        self._fail_rate = cfg.get("fail_rate", {})
        self._qc_pass_rate = float(cfg.get("qc_pass_rate", 0.8))
        self._battery = float(cfg.get("battery_start", 100.0))
        self._battery_per_task = float(cfg.get("battery_per_task", 0.5))
        self._rng = random.Random(cfg.get("seed"))
        self._report_location = bool(cfg.get("report_location")) and "navigate" in self.actions
        self._locations = cfg.get("locations")
        self._at = None

    def execute(self, subtask: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        action = subtask["action"]
        if action == "navigate":
            self._at = None
            if self._locations is not None and subtask.get("zone") not in self._locations:
                return failed(f"no coordinates for {subtask.get('zone')!r} (simulated)")
        if cancel.wait(self._durations.get(action, 1.0)):
            return cancelled()
        self._battery = max(0.0, self._battery - self._battery_per_task)
        if self._rng.random() < float(self._fail_rate.get(action, 0.0)):
            return failed(f"simulated {action} failure")
        if action == "quality_check":
            return done({"pass": self._rng.random() < self._qc_pass_rate})
        if action == "navigate":
            self._at = subtask.get("zone")
            return done({"verified": True, "simulated": True})
        return done()

    def get_state(self) -> Dict[str, Any]:
        extra = {}
        if self._report_location:
            extra = {"at_location": self._at, "at_locations": [self._at] if self._at else []}
            if self._locations is not None:
                extra["locations"] = list(self._locations)
        return robot_state(True, "simulated" + (f" at {self._at or '-'}" if self._report_location else ""),
                           battery=round(self._battery, 1), **extra)
