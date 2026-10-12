"""
allocator.py
------------
Thin wrapper around RobotRegistry.try_claim for a single subtask. Atomicity
lives in the registry (one lock, search + claim together).

Sticky continuation (the same physical robot for a task's whole
navigate/pick/navigate/place chain) is handled in scheduler.py.
"""

from typing import Any, Dict, List, Optional
from robot_registry import RobotRegistry, Robot


class AllocationError(Exception):
    """Nothing allocatable right now. Retryable -- see scheduler's pending queue."""


class TaskAllocator:
    def __init__(self, registry: RobotRegistry):
        self.registry = registry

    def allocate(self, subtask: Dict[str, Any], locations: Optional[List[str]] = None) -> Robot:
        """locations: every place the claimed robot will drive to for this job
        (carrier claims); robots whose base lacks coordinates for one are skipped."""
        robot = self.registry.try_claim(
            required_capabilities=subtask["requires"],
            zone=subtask.get("zone"),
            require_manipulator=subtask.get("require_manipulator"),
            locations=locations,
        )
        if robot is None:
            raise AllocationError(
                f"No allocatable robot for subtask {subtask['subtask_id']} "
                f"(requires={subtask['requires']}, zone={subtask.get('zone')}, "
                f"manipulator={subtask.get('require_manipulator')}"
                + (f", needs coordinates for {locations}" if locations else "") + ")"
            )
        return robot
