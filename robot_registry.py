"""
robot_registry.py
------------------
Fleet metadata plus live health, with atomic claim helpers.

Each entry in robots_metadata.json is ONE PHYSICAL ROBOT, which may have
several SUBSYSTEMS with their own controllers/topics (e.g. a MiR base and a
UR arm mounted on it). The robot as a whole is locked busy/idle, because the
base and the arm belong to whichever task currently owns that robot.

Two independent kinds of state per robot:

  status   (owned by the scheduler)   idle | busy | error | maintenance
           - error: something went wrong and a human must look at the robot
             (e.g. it may still be holding a product). Only an operator
             'reset_robot' command returns it to idle.
           - maintenance: taken out of service by an operator.

  health   (reported by robot agents, per subsystem, via heartbeats)
           online / ready / estop / battery / last_seen
           A robot is only allocatable when it is idle AND every subsystem
           is online, ready, not e-stopped, recently seen, and above the
           minimum battery level.

  locations (reported by navigation subsystems, e.g. the ONIX base)
           the site locations that have x, y, theta coordinates on that
           robot (stations.json). A carrier job is only given to a robot
           whose base can reach every location of the job. A base that
           reports nothing (simulator, older driver) is not restricted.

try_claim() does the search and the mark-busy inside one lock, so two
concurrent allocations can never both get the same robot.
"""

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

VALID_STATUSES = ("idle", "busy", "error", "maintenance")


@dataclass
class SubsystemHealth:
    online: bool = False
    ready: bool = False
    estop: bool = False
    battery: Optional[float] = None
    busy: bool = False
    detail: str = "no heartbeat yet"
    last_seen: float = 0.0
    locations: Optional[List[str]] = None    # locations with coordinates (navigation subsystems)
    at_location: Optional[str] = None        # where a navigation subsystem says it is


@dataclass
class Robot:
    id: str
    name: str
    type: str
    subsystems: Dict[str, Dict[str, Any]]  # key -> {topic, capabilities, manipulator?}
    zones: List[str] = field(default_factory=lambda: ["*"])
    status: str = "idle"
    status_reason: str = ""
    health: Dict[str, SubsystemHealth] = field(default_factory=dict)  # keyed by topic segment

    def topics(self) -> List[str]:
        return [sub["topic"] for sub in self.subsystems.values()]

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

    def navigation_topics(self) -> List[str]:
        return [sub["topic"] for sub in self.subsystems.values() if "navigate" in sub.get("capabilities", [])]

    def missing_locations(self, needed: Iterable[str]) -> List[str]:
        """Locations from `needed` that this robot's base has no coordinates for.
        Bases that have not reported their locations are not restricted."""
        missing: List[str] = []
        for topic in self.navigation_topics():
            h = self.health.get(topic)
            if h is not None and h.locations is not None:
                missing += [loc for loc in needed if loc not in h.locations and loc not in missing]
        return missing

    def subsystem_for_capability(self, capability: str) -> Optional[str]:
        """MQTT topic segment of the subsystem providing this capability."""
        for sub in self.subsystems.values():
            if capability in sub.get("capabilities", []):
                return sub["topic"]
        return None

    def health_problem(self, now: float, heartbeat_timeout_s: float,
                       min_battery: float) -> Optional[str]:
        """None if every subsystem is healthy, else a human-readable reason."""
        for topic in self.topics():
            h = self.health.get(topic)
            if h is None or not h.online:
                return f"{topic}: offline"
            if now - h.last_seen > heartbeat_timeout_s:
                return f"{topic}: heartbeat stale ({now - h.last_seen:.0f}s)"
            if h.estop:
                return f"{topic}: emergency stop active"
            if not h.ready:
                return f"{topic}: not ready ({h.detail})"
            if h.battery is not None and h.battery < min_battery:
                return f"{topic}: battery {h.battery:.0f}% < {min_battery:.0f}%"
        return None


class RobotRegistry:
    """Thread-safe in-memory registry of robots, backed by a JSON file."""

    def __init__(self, metadata_path: str, require_heartbeat: bool = True,
                 heartbeat_timeout_s: float = 15.0, min_battery_percent: float = 25.0,
                 clock: Callable[[], float] = time.time):
        self._path = metadata_path
        self._clock = clock
        self._lock = threading.Lock()
        self._robots: Dict[str, Robot] = {}
        self.require_heartbeat = require_heartbeat
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.min_battery_percent = min_battery_percent
        self._load()

    def _load(self):
        with open(self._path, "r") as f:
            data = json.load(f)
        robots = {}
        for r in data.get("robots", []):
            if r["id"] in robots:
                raise ValueError(f"duplicate robot id {r['id']!r} in {self._path}")
            status = r.get("status", "idle")
            robots[r["id"]] = Robot(
                id=r["id"],
                name=r.get("name", r["id"]),
                type=r.get("type", "generic"),
                subsystems=r.get("subsystems", {}),
                zones=r.get("zones", ["*"]),
                # 'busy' in a metadata file is meaningless at startup; only
                # maintenance is honoured as a static setting.
                status="maintenance" if status == "maintenance" else "idle",
            )
        with self._lock:
            self._robots = robots

    # ---- queries -------------------------------------------------------------
    def all(self) -> List[Robot]:
        with self._lock:
            return list(self._robots.values())

    def get(self, robot_id: str) -> Optional[Robot]:
        with self._lock:
            return self._robots.get(robot_id)

    def zones_with_capability(self, capability: str) -> List[str]:
        with self._lock:
            zones = set()
            for r in self._robots.values():
                if r.has_capability(capability):
                    zones.update(r.zones)
            return sorted(zones)

    def reachable_locations(self) -> Optional[Set[str]]:
        """Union of the locations the fleet's bases report coordinates for, or
        None when no base reports any (then nothing is checked)."""
        with self._lock:
            reported = [h.locations for r in self._robots.values() for t in r.navigation_topics()
                        for h in [r.health.get(t)] if h is not None and h.locations is not None]
        if not reported:
            return None
        return set().union(*reported)

    def is_allocatable(self, robot: Robot, now: Optional[float] = None) -> bool:
        if robot.status != "idle":
            return False
        if not self.require_heartbeat:
            return True
        now = self._clock() if now is None else now
        return robot.health_problem(now, self.heartbeat_timeout_s, self.min_battery_percent) is None

    def _matches(self, robot: Robot, required_capabilities: List[str], zone: Optional[str],
                 require_manipulator: Optional[bool], now: float,
                 locations: Optional[List[str]] = None) -> bool:
        if not self.is_allocatable(robot, now):
            return False
        if locations and robot.missing_locations(locations):
            return False
        if not all(robot.has_capability(c) for c in required_capabilities):
            return False
        if not robot.can_operate_in(zone):
            return False
        if require_manipulator is not None:
            if not all(robot.manipulator_matches(require_manipulator, c) for c in required_capabilities):
                return False
        return True

    # ---- claims ------------------------------------------------------------------
    def try_claim(self, required_capabilities: List[str], zone: Optional[str] = None,
                  require_manipulator: Optional[bool] = None,
                  locations: Optional[List[str]] = None,
                  robot_id: Optional[str] = None) -> Optional[Robot]:
        """Atomically find an allocatable, capable robot and mark it busy.
        locations: every place the robot will have to drive to for this job;
        robots whose base has no coordinates for one of them are skipped.
        robot_id: only this robot may be claimed (e.g. a conveyor's own QC station).
        Among candidates, prefers the one with the most battery left.
        Returns None if nothing qualifies right now (retry later)."""
        now = self._clock()
        with self._lock:
            candidates = [r for r in self._robots.values()
                          if (robot_id is None or r.id == robot_id)
                          and self._matches(r, required_capabilities, zone, require_manipulator, now, locations)]
            if not candidates:
                return None
            robot = max(candidates, key=lambda r: min(
                [h.battery for h in r.health.values() if h.battery is not None] or [100.0]))
            robot.status = "busy"
            robot.status_reason = ""
            return robot

    # ---- status changes ------------------------------------------------------------
    def set_status(self, robot_id: str, status: str, reason: str = ""):
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid robot status {status!r}")
        with self._lock:
            robot = self._robots.get(robot_id)
            if robot:
                robot.status = status
                robot.status_reason = reason

    def mark_idle(self, robot_id: str):
        self.set_status(robot_id, "idle")

    # ---- health (heartbeats from robot agents) ------------------------------------
    def update_health(self, robot_id: str, topic: str, payload: Dict[str, Any],
                      now: Optional[float] = None) -> bool:
        """Apply a heartbeat. Returns True if the robot's allocatability might
        have changed (so the caller can retry pending work)."""
        now = self._clock() if now is None else now
        with self._lock:
            robot = self._robots.get(robot_id)
            if robot is None or topic not in robot.topics():
                return False
            h = robot.health.setdefault(topic, SubsystemHealth())
            before = robot.health_problem(now, self.heartbeat_timeout_s, self.min_battery_percent)
            locs_before = h.locations
            h.online = bool(payload.get("online", False))
            h.ready = bool(payload.get("ready", False)) and h.online
            h.estop = bool(payload.get("estop", False))
            h.busy = bool(payload.get("busy", False))
            battery = payload.get("battery")
            h.battery = float(battery) if isinstance(battery, (int, float)) else None
            h.detail = str(payload.get("detail", ""))[:200]
            h.last_seen = now
            # Location info is kept from earlier heartbeats when a message has
            # none (last will, a failed status read): stations.json did not
            # change just because the agent went quiet.
            locs = payload.get("locations")
            if isinstance(locs, list):
                h.locations = [str(x) for x in locs]
            if "at_location" in payload:
                at = payload.get("at_location")
                h.at_location = str(at) if isinstance(at, str) else None
            elif not h.online:
                h.at_location = None
            after = robot.health_problem(now, self.heartbeat_timeout_s, self.min_battery_percent)
            # a newly taught location can unblock a queued job too
            return (before is None) != (after is None) or h.locations != locs_before

    # ---- persistence helpers --------------------------------------------------------
    def snapshot_statuses(self) -> Dict[str, Dict[str, str]]:
        with self._lock:
            return {r.id: {"status": r.status, "reason": r.status_reason} for r in self._robots.values()}

    def restore_statuses(self, statuses: Dict[str, Dict[str, str]]):
        with self._lock:
            for rid, s in statuses.items():
                robot = self._robots.get(rid)
                if robot and s.get("status") in VALID_STATUSES:
                    robot.status = s["status"]
                    robot.status_reason = s.get("reason", "")

    def fleet_view(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = self._clock() if now is None else now
        with self._lock:
            out = []
            for r in self._robots.values():
                problem = r.health_problem(now, self.heartbeat_timeout_s, self.min_battery_percent)
                out.append({
                    "id": r.id, "name": r.name, "status": r.status, "reason": r.status_reason,
                    "healthy": problem is None, "health_problem": problem,
                    "subsystems": {t: {"online": h.online, "ready": h.ready, "estop": h.estop,
                                       "battery": h.battery, "detail": h.detail,
                                       "age_s": round(now - h.last_seen, 1) if h.last_seen else None,
                                       **({"at_location": h.at_location, "locations": h.locations}
                                          if h.locations is not None else {})}
                                   for t, h in r.health.items()},
                })
            return out
