"""
scheduler.py
------------
The task planning and scheduling agent itself.

Flow for each new instruction from the reasoning agent:
  1. decompose()  -> ordered subtask graph (task_decomposer.py)
  2. allocate()   -> pick a robot per subtask by capability match (allocator.py)
  3. dispatch      -> publish the first ready subtask(s) over MQTT
  4. on ack        -> mark robot idle again, resolve the next ready subtask(s)
                       (including picking the correct success/fail branch),
                       allocate + dispatch, repeat until the task graph is done

Subtasks are only dispatched when their `depends_on` predecessor has
completed — this is what lets a pick step run on a manipulator, a QC step
run on a vision station, and the place step run on a (possibly different)
manipulator, in the right order, over independent MQTT connections.
"""

import logging
from typing import Dict, Any, List, Optional

from robot_registry import RobotRegistry
from allocator import TaskAllocator, AllocationError
from task_decomposer import decompose
from mqtt_client import SchedulerMQTTClient

logger = logging.getLogger("scheduler")


class TaskScheduler:
    def __init__(self, registry: RobotRegistry, mqtt_client: SchedulerMQTTClient):
        self.registry = registry
        self.allocator = TaskAllocator(registry)
        self.mqtt = mqtt_client
        # task_id -> {subtask_id: subtask}
        self._graphs: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # subtask_id -> robot_id, for the currently in-flight subtasks
        self._in_flight: Dict[str, str] = {}
        # task_id -> resolved branch ("success" | "fail"), once known
        self._resolved_branch: Dict[str, str] = {}
        # task_id -> set of subtask_ids that have completed
        self._completed: Dict[str, set] = {}

    # ---- entry point: new instruction arrived from the reasoning agent ----
    def submit_instruction(self, instruction: Dict[str, Any]) -> str:
        subtasks = decompose(instruction)
        task_id = subtasks[0]["task_id"]
        self._graphs[task_id] = {s["subtask_id"]: s for s in subtasks}
        self._completed[task_id] = set()
        logger.info("New task %s decomposed into %d subtasks", task_id, len(subtasks))

        # dispatch every subtask whose dependency is already satisfied (i.e. None)
        for s in subtasks:
            if s["depends_on"] is None:
                self._try_dispatch(task_id, s["subtask_id"])
        return task_id

    # ---- called by the MQTT status callback when a robot finishes a subtask ----
    def on_robot_status(self, robot_id: str, status_payload: Dict[str, Any]):
        subtask_id = status_payload.get("subtask_id")
        state = status_payload.get("state")
        if subtask_id is None:
            logger.warning("Status from %s missing subtask_id: %s", robot_id, status_payload)
            return

        self.registry.mark_idle(robot_id)
        self._in_flight.pop(subtask_id, None)

        task_id = self._task_id_for(subtask_id)
        if task_id is None:
            logger.warning("Status for unknown subtask %s", subtask_id)
            return

        if state != "done":
            logger.error("Subtask %s failed on %s: %s", subtask_id, robot_id, status_payload)
            return

        subtask = self._graphs[task_id][subtask_id]
        self._completed[task_id].add(subtask_id)

        # If this was the quality_check step, its result decides the branch.
        if subtask["action"] == "quality_check":
            result = status_payload.get("result", {})
            branch = "success" if result.get("pass", True) else "fail"
            self._resolved_branch[task_id] = branch
            logger.info("Task %s quality_check resolved -> branch=%s", task_id, branch)

        # dispatch any subtask whose dependency was this one and whose
        # branch (if any) matches the resolved branch (or has no branch)
        for candidate in self._graphs[task_id].values():
            if candidate["depends_on"] != subtask_id:
                continue
            branch = candidate.get("branch")
            if branch is not None and self._resolved_branch.get(task_id) != branch:
                continue  # the other branch — skip permanently
            self._try_dispatch(task_id, candidate["subtask_id"])

        if self._task_complete(task_id):
            logger.info("Task %s complete", task_id)

    # ---- internals ----
    def _try_dispatch(self, task_id: str, subtask_id: str):
        subtask = self._graphs[task_id][subtask_id]
        try:
            robot = self.allocator.allocate(subtask)
        except AllocationError as e:
            logger.error(str(e))
            return
        self.registry.mark_busy(robot.id)
        self._in_flight[subtask_id] = robot.id
        self.mqtt.publish_task(robot.id, subtask)

    def _task_id_for(self, subtask_id: str) -> Optional[str]:
        for task_id, graph in self._graphs.items():
            if subtask_id in graph:
                return task_id
        return None

    def _task_complete(self, task_id: str) -> bool:
        """A task is complete once every subtask on the *taken* branch
        (steps with branch=None always count) has completed."""
        branch = self._resolved_branch.get(task_id)
        graph = self._graphs[task_id]
        completed = self._completed[task_id]
        for s in graph.values():
            if s.get("branch") is not None and s["branch"] != branch:
                continue  # the branch not taken — never needs to complete
            if s["subtask_id"] not in completed:
                return False
        return True
