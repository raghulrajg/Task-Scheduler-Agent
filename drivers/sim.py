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
  "seed": null
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

    def execute(self, subtask: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        action = subtask["action"]
        if cancel.wait(self._durations.get(action, 1.0)):
            return cancelled()
        self._battery = max(0.0, self._battery - self._battery_per_task)
        if self._rng.random() < float(self._fail_rate.get(action, 0.0)):
            return failed(f"simulated {action} failure")
        if action == "quality_check":
            return done({"pass": self._rng.random() < self._qc_pass_rate})
        return done()

    def get_state(self) -> Dict[str, Any]:
        return robot_state(True, "simulated", battery=round(self._battery, 1))
