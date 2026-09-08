"""
scheduler.py
------------
The task planning and scheduling agent itself.

Three things this version specifically handles, per your fleet's real shape:

1. STICKY CARRIER ROBOT. A mobile-manipulator's AMR and arm are two
   subsystems of the same physical unit. The robot that picks up a product
   must be the one that later places it -- so once a task's first "carrier"
   subtask (navigate to source) claims a robot, every later carrier subtask
   in that same task (pick, navigate to dest, place) reuses that exact robot
   instead of being independently re-allocated. See self._carrier_robot.

2. SCARCE INSPECTOR CAPACITY. Only 1-2 QC stations exist. quality_check
   subtasks are allocated normally (first idle match) and release the
   moment they report a result -- no stickiness needed there, but expect
   contention, which is why...

3. RETRY QUEUE + ATOMIC CLAIMS. If nothing is idle right now, the subtask
   goes on a pending queue instead of being dropped, and gets retried every
   time ANY robot frees up. The actual "is this robot idle -> mark busy"
   check is atomic inside RobotRegistry.try_claim, so two subtasks arriving
   at the same instant (two instructions submitted concurrently, or two
   ROS2/MQTT callback threads firing together) can't both claim the same
   robot. A single lock around the scheduler's own bookkeeping (the task
   graphs, the pending queue, the sticky-robot map) makes the rest of this
   class safe under concurrent submit_instruction() / on_robot_status() calls
   too.
"""

import logging
import threading
from typing import Dict, Any, List, Optional, Tuple

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
        self._lock = threading.RLock()

        self._graphs: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._completed: Dict[str, set] = {}
        self._resolved_branch: Dict[str, str] = {}
        self._in_flight: Dict[str, Tuple[str, str]] = {}   # subtask_id -> (robot_id, subsystem)
        self._carrier_robot: Dict[str, str] = {}            # task_id -> robot_id
        self._pending: List[Tuple[str, str]] = []           # (task_id, subtask_id) waiting for a free robot

    # ---- entry point: new instruction arrived from the reasoning agent ----
    def submit_instruction(self, instruction: Dict[str, Any]) -> str:
        subtasks = decompose(instruction)
        task_id = subtasks[0]["task_id"]
        with self._lock:
            self._graphs[task_id] = {s["subtask_id"]: s for s in subtasks}
            self._completed[task_id] = set()
            logger.info("New task %s decomposed into %d subtasks", task_id, len(subtasks))
            for s in subtasks:
                if s["depends_on"] is None:
                    self._try_dispatch(task_id, s["subtask_id"])
        return task_id

    # ---- called by the MQTT status callback when a robot subsystem finishes ----
    def on_robot_status(self, robot_id: str, subsystem: str, status_payload: Dict[str, Any]):
        subtask_id = status_payload.get("subtask_id")
        state = status_payload.get("state")
        if subtask_id is None:
            logger.warning("Status from %s/%s missing subtask_id: %s", robot_id, subsystem, status_payload)
            return

        with self._lock:
            task_id = self._task_id_for(subtask_id)
            if task_id is None:
                logger.warning("Status for unknown subtask %s", subtask_id)
                return

            self._in_flight.pop(subtask_id, None)

            if state != "done":
                logger.error("Subtask %s failed on %s/%s: %s", subtask_id, robot_id, subsystem, status_payload)
                # release the robot so it isn't stuck busy forever on a failure
                if self._graphs[task_id][subtask_id].get("releases_robot", True):
                    self.registry.mark_idle(robot_id)
                    self._carrier_robot.pop(task_id, None)
                self._drain_pending()
                return

            subtask = self._graphs[task_id][subtask_id]
            self._completed[task_id].add(subtask_id)

            if subtask.get("releases_robot"):
                self.registry.mark_idle(robot_id)
                if subtask.get("group") == "carrier":
                    self._carrier_robot.pop(task_id, None)

            if subtask["action"] == "quality_check":
                result = status_payload.get("result", {})
                branch = "success" if result.get("pass", True) else "fail"
                self._resolved_branch[task_id] = branch
                logger.info("Task %s quality_check resolved -> branch=%s", task_id, branch)

            for candidate in self._graphs[task_id].values():
                if candidate["depends_on"] != subtask_id:
                    continue
                cand_branch = candidate.get("branch")
                if cand_branch is not None and self._resolved_branch.get(task_id) != cand_branch:
                    continue  # the other branch -- never dispatched
                self._try_dispatch(task_id, candidate["subtask_id"])

            self._drain_pending()

            if self._task_complete(task_id):
                logger.info("Task %s complete", task_id)

    # ---- internals (all called with self._lock already held) ----
    def _try_dispatch(self, task_id: str, subtask_id: str):
        subtask = self._graphs[task_id][subtask_id]

        if subtask.get("group") == "carrier" and task_id in self._carrier_robot:
            # sticky continuation: reuse the robot already claimed for this task
            robot_id = self._carrier_robot[task_id]
            robot = self.registry.get(robot_id)
            if robot is None:
                logger.error("Sticky carrier robot %s for task %s vanished from registry", robot_id, task_id)
                return
        else:
            try:
                robot = self.allocator.allocate(subtask)
            except AllocationError as e:
                logger.info("%s -- queued for retry when a robot frees up", e)
                if (task_id, subtask_id) not in self._pending:
                    self._pending.append((task_id, subtask_id))
                return
            if subtask.get("group") == "carrier":
                self._carrier_robot[task_id] = robot.id

        subsystem = robot.subsystem_for_capability(subtask["requires"][0])
        self._in_flight[subtask_id] = (robot.id, subsystem)
        self.mqtt.publish_task(robot.id, subsystem, subtask)

    def _drain_pending(self):
        """Retry every queued subtask now that a robot may have freed up.
        Runs until a full pass makes no progress, so one freed robot can
        unblock several pending subtasks in one go if the fleet allows it."""
        progressed = True
        while progressed and self._pending:
            progressed = False
            still_pending = []
            for task_id, subtask_id in self._pending:
                before = len(self._in_flight)
                self._try_dispatch(task_id, subtask_id)
                if len(self._in_flight) > before:
                    progressed = True
                else:
                    still_pending.append((task_id, subtask_id))
            self._pending = still_pending

    def _task_id_for(self, subtask_id: str) -> Optional[str]:
        for task_id, graph in self._graphs.items():
            if subtask_id in graph:
                return task_id
        return None

    def _task_complete(self, task_id: str) -> bool:
        branch = self._resolved_branch.get(task_id)
        graph = self._graphs[task_id]
        completed = self._completed[task_id]
        for s in graph.values():
            if s.get("branch") is not None and s["branch"] != branch:
                continue
            if s["subtask_id"] not in completed:
                return False
        return True
