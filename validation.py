"""
validation.py
-------------
Site configuration loading and instruction validation, shared by the
scheduler and the reasoning agent so they can never disagree about which
locations exist.

An LLM is not a trusted source of robot targets. Every instruction (whether
it came from the LLM, an HMI or a test script) is checked here before a
single subtask is dispatched.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

REQUIRED_KEYS = ("product", "source", "quality_check", "success_dest", "fail_dest")
_PRODUCT_RE = re.compile(r"^[A-Za-z0-9 _\-.]{1,64}$")

DEFAULT_TIMEOUTS = {"accept": 30, "navigate": 300, "pick": 120, "place": 120, "quality_check": 90}
DEFAULT_RETRIES = {"navigate": 1, "pick": 1, "place": 0, "quality_check": 1}


class InstructionError(ValueError):
    """The instruction is malformed or refers to something that doesn't exist."""


@dataclass
class SiteConfig:
    locations: Dict[str, Dict[str, Any]]
    timeouts_s: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TIMEOUTS))
    max_retries: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_RETRIES))
    max_task_reassignments: int = 2
    min_battery_percent: float = 25.0
    heartbeat_timeout_s: float = 15.0
    startup_grace_s: float = 30.0
    finished_task_retention: int = 200

    @classmethod
    def load(cls, path: str) -> "SiteConfig":
        with open(path, "r") as f:
            data = json.load(f)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SiteConfig":
        locations = data.get("locations")
        if not isinstance(locations, dict) or not locations:
            raise ValueError("site config must define a non-empty 'locations' object")
        for name, meta in locations.items():
            conv = meta.get("conveyor") if isinstance(meta, dict) else None
            if conv in (None, False, True):
                continue
            if not isinstance(conv, dict):
                raise ValueError(f"locations.{name}.conveyor must be true or an object")
            exit_loc = conv.get("exit_location")
            if exit_loc is not None and (exit_loc not in locations or exit_loc == name):
                raise ValueError(f"locations.{name}.conveyor.exit_location {exit_loc!r} must be another "
                                 f"location in site.json")
        return cls(
            locations=locations,
            timeouts_s={**DEFAULT_TIMEOUTS, **data.get("timeouts_s", {})},
            max_retries={**DEFAULT_RETRIES, **data.get("max_retries", {})},
            max_task_reassignments=int(data.get("max_task_reassignments", 2)),
            min_battery_percent=float(data.get("min_battery_percent", 25)),
            heartbeat_timeout_s=float(data.get("heartbeat_timeout_s", 15)),
            startup_grace_s=float(data.get("startup_grace_s", 30)),
            finished_task_retention=int(data.get("finished_task_retention", 200)),
        )

    def timeout_for(self, action: str) -> float:
        return float(self.timeouts_s.get(action, 300))

    def retries_for(self, action: str) -> int:
        return int(self.max_retries.get(action, 0))

    def conveyor_for(self, location: str) -> Optional[Dict[str, Any]]:
        """The conveyor settings of a QC location, or None when the part is
        inspected while the robot holds it. With a conveyor, the carrier
        places the part on the belt, the conveyor runs it past the camera,
        and the carrier picks it up again at the exit (exit_location, or the
        same location when that is null)."""
        conv = (self.locations.get(location) or {}).get("conveyor")
        if conv is None or conv is False:
            return None
        return dict(conv) if isinstance(conv, dict) else {}

    def job_locations(self, instruction: Dict[str, Any]) -> List[str]:
        """Every location the carrier robot may drive to for this job."""
        places = [instruction["source"], instruction["quality_check"]]
        exit_loc = (self.conveyor_for(instruction["quality_check"]) or {}).get("exit_location")
        if exit_loc:
            places.append(exit_loc)
        places += [instruction["success_dest"], instruction["fail_dest"]]
        return list(dict.fromkeys(places))


def validate_instruction(instruction: Any, site: SiteConfig,
                         inspection_zones: Optional[Iterable[str]] = None,
                         reachable_locations: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Returns a cleaned copy of the instruction or raises InstructionError.

    inspection_zones: zones where at least one QC-capable robot can work. When
    given, quality_check must be one of them -- otherwise the carrier would
    drive there and wait forever for an inspector that can never come.

    reachable_locations: locations that at least one mobile base has x, y,
    theta coordinates for (reported in its heartbeat from stations.json).
    When given, every location of the job must be one of them -- a job is
    refused up front instead of failing halfway with a part on board.
    """
    if not isinstance(instruction, dict):
        raise InstructionError(f"instruction must be a JSON object, got {type(instruction).__name__}")

    missing = [k for k in REQUIRED_KEYS if k not in instruction]
    if missing:
        raise InstructionError(f"missing required keys: {missing}")

    clean: Dict[str, Any] = {}
    for key in REQUIRED_KEYS:
        value = instruction[key]
        if not isinstance(value, str) or not value.strip():
            raise InstructionError(f"'{key}' must be a non-empty string")
        clean[key] = value.strip()

    if not _PRODUCT_RE.match(clean["product"]):
        raise InstructionError("'product' may only contain letters, digits, space, '_', '-', '.' (max 64)")

    for key in ("source", "quality_check", "success_dest", "fail_dest"):
        if clean[key] not in site.locations:
            raise InstructionError(
                f"'{key}'={clean[key]!r} is not a known location; valid: {sorted(site.locations)}")

    if inspection_zones is not None:
        zones = set(inspection_zones)
        if "*" not in zones and clean["quality_check"] not in zones:
            raise InstructionError(
                f"no QC station operates at {clean['quality_check']!r}; QC zones: {sorted(zones)}")

    if reachable_locations is not None:
        reachable = set(reachable_locations)
        missing = [loc for loc in site.job_locations(clean) if loc not in reachable]
        if missing:
            raise InstructionError(
                f"no mobile base has coordinates for {sorted(set(missing))}: assign or teach them with "
                f"tools/amr_stations.py (stations.json); locations with coordinates: {sorted(reachable)}")

    if clean["success_dest"] == clean["fail_dest"]:
        raise InstructionError("success_dest and fail_dest must differ (otherwise rejected parts mix with good ones)")

    # Optional pass-through fields
    request_id = instruction.get("request_id")
    if request_id is not None:
        if not isinstance(request_id, str) or len(request_id) > 64:
            raise InstructionError("'request_id' must be a string of at most 64 chars")
        clean["request_id"] = request_id
    return clean
