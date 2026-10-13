"""
task_decomposer.py
-------------------
Turns the reasoning agent's structured instruction into an ordered list of
atomic subtasks. Two new fields versus the first version matter a lot now
that robots are mobile-manipulator combos with two subsystems:

  "group"           "carrier"  -> this subtask must run on the SAME physical
                                    robot as the other carrier subtasks in this
                                    task (it's the robot physically holding the
                                    product from pick to place).
                     "inspector" -> a one-off subtask that can go to any idle
                                    robot with the right capability (the QC
                                    station) -- no stickiness needed.

  "releases_robot"  True/False -> whether finishing this subtask should free
                                    its robot back to idle. Only the LAST
                                    carrier subtask on a given branch releases
                                    the carrier robot; navigate/pick/navigate
                                    in the middle do not, because the same
                                    robot has more work coming. Inspector
                                    subtasks always release immediately.

Each subtask:
{
    "subtask_id": "t1-2",
    "task_id": "t1",
    "action": "pick",
    "target": "ABCD",
    "zone": "manufacturing_1",
    "requires": ["pick"],
    "require_manipulator": true,
    "depends_on": "t1-1",
    "branch": None,             # "success" | "fail" | None
    "group": "carrier",         # "carrier" | "inspector"
    "releases_robot": False,
    "handover": None,           # "load" / "unload" at a conveyor QC station, else None
    "station": None,            # conveyor QC station (robot id) for load + quality_check steps
    "attempt": 1,               # bumped by the scheduler on each retry
}

Conveyor QC stations (site.json: "conveyor" on the QC location). The part
cannot be inspected in the gripper: the carrier places it on the belt
("load"), the conveyor runs it past the camera and publishes the verdict,
and the carrier picks it up again at the belt exit ("unload") before
driving to the success/fail destination. The carrier stays claimed the
whole time, so the robot that loaded the part is the one that unloads it.
The quality_check goes to the conveyor's own station (conveyor.station),
never to another QC robot in the zone, which could not see the belt.

Validation (known locations, QC zone reachable, types) happens BEFORE this
in validation.validate_instruction -- this module assumes clean input.
"""

import uuid
from typing import Any, Dict, List, Optional


def decompose_pick_qc_route(instruction: Dict[str, Any],
                            conveyor: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Handles: { product, source, quality_check, success_dest, fail_dest }
    Produces: navigate -> pick -> navigate -> quality_check -> (branch) ->
              navigate -> place
    With a conveyor at the QC location (conveyor = its site.json settings):
              navigate -> pick -> navigate -> place (load belt) -> quality_check
              -> [navigate to exit_location] -> pick (unload belt) -> (branch) ->
              navigate -> place

    navigate/pick/navigate/[navigate/place per branch] are all "carrier" --
    one mobile-manipulator carries the product the whole way and only frees
    up after the final place. quality_check is "inspector" -- a separate,
    scarcer QC station that's free again the moment it reports a result.
    """
    task_id = uuid.uuid4().hex[:12]
    product = instruction["product"]
    source = instruction["source"]
    qc_zone = instruction["quality_check"]
    success_dest = instruction["success_dest"]
    fail_dest = instruction["fail_dest"]

    subtasks = []

    def add(action, target, zone, requires, manipulator=None, branch=None,
             group="carrier", releases_robot=False, subtask_id=None, handover=None, station=None):
        sid = subtask_id or f"{task_id}-{len(subtasks) + 1}"
        subtasks.append({
            "subtask_id": sid,
            "task_id": task_id,
            "action": action,
            "target": target,
            "zone": zone,
            "requires": requires,
            "require_manipulator": manipulator,
            "depends_on": subtasks[-1]["subtask_id"] if subtasks else None,
            "branch": branch,
            "group": group,
            "releases_robot": releases_robot,
            "handover": handover,
            "station": station,
            "attempt": 1,
        })

    add("navigate", product, source, ["navigate"])
    add("pick", product, source, ["pick"], manipulator=True)
    add("navigate", product, qc_zone, ["navigate"])
    station = conveyor.get("station") if conveyor is not None else None
    if conveyor is not None:
        add("place", product, qc_zone, ["place"], manipulator=True, handover="load", station=station)
    add("quality_check", product, qc_zone, ["quality_check"],
        group="inspector", releases_robot=True, station=station)
    if conveyor is not None:
        exit_zone = conveyor.get("exit_location") or qc_zone
        if exit_zone != qc_zone:
            add("navigate", product, exit_zone, ["navigate"])
        add("pick", product, exit_zone, ["pick"], manipulator=True, handover="unload")

    # both branches hang off the last common step (the QC, or the unload pick)
    qc_subtask_id = subtasks[-1]["subtask_id"]

    for branch, dest in (("success", success_dest), ("fail", fail_dest)):
        add("navigate", product, dest, ["navigate"], branch=branch,
            subtask_id=f"{task_id}-nav-{branch}")
        # override depends_on: both branches hang off the common step, not each other
        subtasks[-1]["depends_on"] = qc_subtask_id
        add("place", product, dest, ["place"], manipulator=True, branch=branch,
            releases_robot=True, subtask_id=f"{task_id}-place-{branch}")

    return subtasks


def decompose(instruction: Dict[str, Any], site=None) -> List[Dict[str, Any]]:
    """Dispatch to the right decomposition strategy based on instruction shape.
    Extend this as your reasoning agent starts emitting other instruction types.
    site (validation.SiteConfig) says which QC locations are conveyors."""
    keys = set(instruction.keys())
    if {"product", "source", "quality_check", "success_dest", "fail_dest"} <= keys:
        conveyor = site.conveyor_for(instruction["quality_check"]) if site is not None else None
        return decompose_pick_qc_route(instruction, conveyor)
    raise ValueError(f"No decomposition strategy matches instruction keys: {keys}")
