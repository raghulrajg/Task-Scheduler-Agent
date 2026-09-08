"""
allocator.py
------------
Thin wrapper around RobotRegistry.try_claim for a single subtask. The actual
atomicity lives in the registry (one lock, search+claim together) -- this
class just translates "here's a subtask" into "here's what to ask the
registry for".

Sticky continuation (keeping the same physical robot across a task's own
navigate/pick/navigate/place sequence) is handled in scheduler.py, not here --
this module only knows about single, one-off allocations.
"""

from typing import Dict, Any, Optional
from robot_registry import RobotRegistry, Robot


class AllocationError(Exception):
    """Raised when nothing currently idle can satisfy a subtask. Callers
    should treat this as retryable (the fleet is just busy right now), not
    as a permanent failure -- see scheduler.py's retry queue."""
    pass


class TaskAllocator:
    def __init__(self, registry: RobotRegistry):
        self.registry = registry

    def allocate(self, subtask: Dict[str, Any]) -> Robot:
        robot = self.registry.try_claim(
            required_capabilities=subtask["requires"],
            zone=subtask.get("zone"),
            require_manipulator=subtask.get("require_manipulator"),
        )
        if robot is None:
            raise AllocationError(
                f"No idle robot currently satisfies subtask {subtask['subtask_id']} "
                f"(requires={subtask['requires']}, zone={subtask.get('zone')}, "
                f"manipulator={subtask.get('require_manipulator')})"
            )
        return robot
