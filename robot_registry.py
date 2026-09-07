"""
robot_registry.py
------------------
Loads robot fleet metadata (id, name, capabilities, manipulator flag, zones,
status) and exposes query helpers the allocator uses to pick a robot for a
given subtask.

Metadata source is a JSON file so it can be edited/extended without touching
code, and reloaded at runtime if the fleet changes.
"""

import json
import threading
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Robot:
    id: str
    name: str
    type: str
    manipulator: bool
    capabilities: List[str]
    zones: List[str] = field(default_factory=lambda: ["*"])
    status: str = "idle"  # idle | busy | offline | error

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def can_operate_in(self, zone: Optional[str]) -> bool:
        if zone is None or "*" in self.zones:
            return True
        return zone in self.zones

    def is_available(self) -> bool:
        return self.status == "idle"


class RobotRegistry:
    """Thread-safe in-memory registry of robots, backed by a JSON file."""

    def __init__(self, metadata_path: str):
        self._path = metadata_path
        self._lock = threading.Lock()
        self._robots = {}
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
                    manipulator=r.get("manipulator", False),
                    capabilities=r.get("capabilities", []),
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

    def find_candidates(self, required_capabilities: List[str],
                         zone: Optional[str] = None,
                         require_manipulator: Optional[bool] = None) -> List[Robot]:
        """Return idle robots that satisfy all required capabilities,
        the target zone, and the manipulator requirement (if specified)."""
        with self._lock:
            robots = list(self._robots.values())

        candidates = []
        for r in robots:
            if not r.is_available():
                continue
            if not all(r.has_capability(c) for c in required_capabilities):
                continue
            if not r.can_operate_in(zone):
                continue
            if require_manipulator is not None and r.manipulator != require_manipulator:
                continue
            candidates.append(r)
        return candidates

    def set_status(self, robot_id: str, status: str):
        with self._lock:
            if robot_id in self._robots:
                self._robots[robot_id].status = status

    def mark_busy(self, robot_id: str):
        self.set_status(robot_id, "busy")

    def mark_idle(self, robot_id: str):
        self.set_status(robot_id, "idle")
