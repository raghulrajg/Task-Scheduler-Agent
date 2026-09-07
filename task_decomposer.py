"""
task_decomposer.py
-------------------
Turns the reasoning agent's structured instruction (e.g. the JSON your
Ollama-based agent emits: product / source / quality_check / success_dest /
fail_dest) into an ordered list of atomic subtasks, each tagged with the
capability it requires. This is deliberately generic so new instruction
shapes can be added without touching the allocator or MQTT layer.

Each subtask:
{
    "subtask_id": "t1-2",
    "task_id": "t1",
    "seq": 2,
    "action": "pick",
    "target": "ABCD",
    "zone": "manufacturing_1",
    "requires": ["pick"],
    "require_manipulator": true,
    "depends_on": "t1-1",          # subtask_id this must finish before starting
    "branch": None                 # "success" | "fail" | None, for conditional steps
}
"""

import uuid
from typing import List, Dict, Any


def decompose_pick_qc_route(instruction: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Handles the pattern from your reasoning agent's example payload:
    { product, source, quality_check, success_dest, fail_dest }

    Produces: navigate -> pick -> navigate -> quality_check -> (branch) ->
              navigate -> place

    The post-quality-check navigate/place steps are emitted twice, tagged
    branch="success"/"fail" — the scheduler only dispatches the branch that
    matches the quality station's reported result, once that ack arrives.
    """
    task_id = uuid.uuid4().hex[:8]
    product = instruction["product"]
    source = instruction["source"]
    qc_zone = instruction["quality_check"]
    success_dest = instruction["success_dest"]
    fail_dest = instruction["fail_dest"]

    subtasks = []

    def add(action, target, zone, requires, manipulator=None, branch=None):
        idx = len(subtasks) + 1
        subtasks.append({
            "subtask_id": f"{task_id}-{idx}",
            "task_id": task_id,
            "seq": idx,
            "action": action,
            "target": target,
            "zone": zone,
            "requires": requires,
            "require_manipulator": manipulator,
            "depends_on": subtasks[-1]["subtask_id"] if subtasks else None,
            "branch": branch,
        })

    add("navigate", product, source, ["navigate"])
    add("pick", product, source, ["pick"], manipulator=True)
    add("navigate", product, qc_zone, ["navigate"])
    add("quality_check", product, qc_zone, ["quality_check"])

    # Conditional branches share the same depends_on (the quality_check step);
    # the scheduler resolves which one to run once the QC result comes back.
    qc_subtask_id = subtasks[-1]["subtask_id"]

    for branch, dest in (("success", success_dest), ("fail", fail_dest)):
        nav_id = f"{task_id}-nav-{branch}"
        place_id = f"{task_id}-place-{branch}"
        subtasks.append({
            "subtask_id": nav_id, "task_id": task_id, "seq": None,
            "action": "navigate", "target": product, "zone": dest,
            "requires": ["navigate"], "require_manipulator": None,
            "depends_on": qc_subtask_id, "branch": branch,
        })
        subtasks.append({
            "subtask_id": place_id, "task_id": task_id, "seq": None,
            "action": "place", "target": product, "zone": dest,
            "requires": ["place"], "require_manipulator": True,
            "depends_on": nav_id, "branch": branch,
        })

    return subtasks


def decompose(instruction: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Dispatch to the right decomposition strategy based on instruction shape.
    Extend this as your reasoning agent starts emitting other instruction types."""
    keys = set(instruction.keys())
    if {"product", "source", "quality_check", "success_dest", "fail_dest"} <= keys:
        return decompose_pick_qc_route(instruction)
    raise ValueError(f"No decomposition strategy matches instruction keys: {keys}")
