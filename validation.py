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
from typing import Any, Dict, Iterable, Optional

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


def validate_instruction(instruction: Any, site: SiteConfig,
                         inspection_zones: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Returns a cleaned copy of the instruction or raises InstructionError.

    inspection_zones: zones where at least one QC-capable robot can work. When
    given, quality_check must be one of them -- otherwise the carrier would
    drive there and wait forever for an inspector that can never come.
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

    if clean["success_dest"] == clean["fail_dest"]:
        raise InstructionError("success_dest and fail_dest must differ (otherwise rejected parts mix with good ones)")

    # Optional pass-through fields
    request_id = instruction.get("request_id")
    if request_id is not None:
        if not isinstance(request_id, str) or len(request_id) > 64:
            raise InstructionError("'request_id' must be a string of at most 64 chars")
        clean["request_id"] = request_id
    return clean
