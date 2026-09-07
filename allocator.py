"""
allocator.py
------------
Given a subtask and the current robot registry, picks which robot should
run it. Matching is: idle status -> has every required capability -> can
operate in the target zone -> manipulator flag matches if the subtask cares.

Tie-breaking: prefer whichever candidate already handled the previous
subtask in the same task (reduces unnecessary handoffs), otherwise first
available. Swap `_pick_best` for a smarter strategy (load, distance, battery)
later without touching the rest of the pipeline.
"""

from typing import Dict, Any, Optional, List
from robot_registry import RobotRegistry, Robot


class AllocationError(Exception):
    pass


class TaskAllocator:
    def __init__(self, registry: RobotRegistry):
        self.registry = registry

    def allocate(self, subtask: Dict[str, Any],
                  preferred_robot_id: Optional[str] = None) -> Robot:
        candidates = self.registry.find_candidates(
            required_capabilities=subtask["requires"],
            zone=subtask.get("zone"),
            require_manipulator=subtask.get("require_manipulator"),
        )
        if not candidates:
            raise AllocationError(
                f"No idle robot satisfies subtask {subtask['subtask_id']} "
                f"(requires={subtask['requires']}, zone={subtask.get('zone')}, "
                f"manipulator={subtask.get('require_manipulator')})"
            )
        return self._pick_best(candidates, preferred_robot_id)

    def _pick_best(self, candidates: List[Robot],
                    preferred_robot_id: Optional[str]) -> Robot:
        if preferred_robot_id:
            for c in candidates:
                if c.id == preferred_robot_id:
                    return c
        return candidates[0]
