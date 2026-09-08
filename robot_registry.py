"""
robot_registry.py
------------------
Loads robot fleet metadata and exposes the query/claim helpers the scheduler
uses. Each entry in robots_metadata.json is ONE PHYSICAL ROBOT, which may
have multiple SUBSYSTEMS with independent controllers/topics (e.g. an AMR
base and a 6-DOF arm mounted on it). The robot as a whole is what gets
locked idle/busy -- the AMR and arm on the same physical unit can't be
double-booked by two different jobs even though they have separate topics,
because moving the base and using the arm both belong to whichever task
currently "owns" that robot.

Concurrency: find_candidates() + claiming used to be two separate calls in
an earlier version, which is a race condition -- two threads could both see
the same robot as idle before either marked it busy. try_claim() below does
the search-and-mark-busy as a single atomic operation under one lock, which
is what makes it safe for multiple instructions to be submitted to the
scheduler at the same time.
"""

import json
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


@dataclass
class Robot:
    id: str
    name: str
    type: str
    subsystems: Dict[str, Dict[str, Any]]  # subsystem_key -> {topic, capabilities, manipulator?}
    zones: List[str] = field(default_factory=lambda: ["*"])
    status: str = "idle"  # idle | busy | offline | error

    def has_capability(self, capability: str) -> bool:
        return any(capability in sub.get("capabilities", []) for sub in self.subsystems.values())

    def manipulator_matches(self, require_manipulator: Optional[bool], capability: str) -> bool:
        if require_manipulator is None:
            return True
        for sub in self.subsystems.values():
            if capability in sub.get("capabilities", []):
                return sub.get("manipulator", False) == require_manipulator
        return False

    def can_operate_in(self, zone: Optional[str]) -> bool:
        if zone is None or "*" in self.zones:
            return True
        return zone in self.zones

    def is_available(self) -> bool:
        return self.status == "idle"

    def subsystem_for_capability(self, capability: str) -> Optional[str]:
        """Returns the MQTT topic segment for whichever subsystem provides this
        capability, e.g. 'arm' for 'pick', 'amr' for 'navigate'."""
        for sub in self.subsystems.values():
            if capability in sub.get("capabilities", []):
                return sub["topic"]
        return None


class RobotRegistry:
    """Thread-safe in-memory registry of robots, backed by a JSON file."""

    def __init__(self, metadata_path: str):
        self._path = metadata_path
        self._lock = threading.Lock()
        self._robots: Dict[str, Robot] = {}
        self.reload()

    def reload(self):
        with open(self._path, "r") as f:
            data = json.load(f)
        with self._lock:
            self._robots = {
                r["id"]: Robot(
                    id=r["id"],
                    name=r["name"],
                    type=r.get("type", "generic"),
                    subsystems=r.get("subsystems", {}),
                    zones=r.get("zones", ["*"]),
                    status=r.get("status", "idle"),
                )
                for r in data.get("robots", [])
            }

    def all(self) -> List[Robot]:
        with self._lock:
            return list(self._robots.values())

    def get(self, robot_id: str) -> Optional[Robot]:
        with self._lock:
            return self._robots.get(robot_id)

    def subsystem_topic(self, robot_id: str, capability: str) -> Optional[str]:
        with self._lock:
            robot = self._robots.get(robot_id)
            return robot.subsystem_for_capability(capability) if robot else None

    def _matches(self, robot: Robot, required_capabilities: List[str],
                 zone: Optional[str], require_manipulator: Optional[bool]) -> bool:
        if not robot.is_available():
            return False
        if not all(robot.has_capability(c) for c in required_capabilities):
            return False
        if not robot.can_operate_in(zone):
            return False
        if require_manipulator is not None:
            if not all(robot.manipulator_matches(require_manipulator, c) for c in required_capabilities):
                return False
        return True

    def try_claim(self, required_capabilities: List[str],
                   zone: Optional[str] = None,
                   require_manipulator: Optional[bool] = None) -> Optional[Robot]:
        """Atomically find an idle, capable robot and mark it busy in one step.
        Returns None if nothing currently qualifies (caller should treat this
        as 'try again later', not a hard failure)."""
        with self._lock:
            for robot in self._robots.values():
                if self._matches(robot, required_capabilities, zone, require_manipulator):
                    robot.status = "busy"
                    return robot
        return None

    def try_claim_specific(self, robot_id: str) -> Optional[Robot]:
        """Atomically claim a SPECIFIC robot (used when a task must keep using
        the same physical robot for its next step). Returns None if that
        robot is not idle right now."""
        with self._lock:
            robot = self._robots.get(robot_id)
            if robot and robot.is_available():
                robot.status = "busy"
                return robot
        return None

    def set_status(self, robot_id: str, status: str):
        with self._lock:
            if robot_id in self._robots:
                self._robots[robot_id].status = status

    def mark_idle(self, robot_id: str):
        self.set_status(robot_id, "idle")
