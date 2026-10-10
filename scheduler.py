"""
scheduler.py
------------
The task planning and scheduling agent.

What it guarantees, and why each part exists on a real shop floor:

1. STICKY CARRIER ROBOT. The robot that picks a product is the one that
   places it. Once a task's first carrier subtask claims a robot, every later
   carrier subtask in that task reuses it (task["carrier_robot"]).

2. ATOMIC CLAIMS + PENDING QUEUE. RobotRegistry.try_claim() searches and
   marks busy under one lock. If nothing is allocatable the subtask waits in
   a FIFO queue that is retried whenever a robot frees up or a heartbeat
   changes a robot's health.

3. ONLY TRUSTED, EXPECTED ACKS. A status message is applied only if it
   matches the exact (subtask, robot, subsystem, attempt) currently in
   flight. Duplicate deliveries (normal with MQTT QoS 1), late acks from a
   timed-out attempt, and acks from the wrong robot are ignored, so a
   duplicate "done" can never dispatch the next step twice.

4. TIMEOUTS. Every dispatched subtask has a deadline: first for the robot
   to accept it, then for the action itself (site.json timeouts_s). tick()
   cancels and fails anything overdue, so a dead robot can't hang a task.

5. FAILURE HANDLING THAT RESPECTS PHYSICS. Failed subtasks are retried up to
   site.json max_retries. After that:
     - carrier failed while driving to the pick-up (nothing on board yet):
       robot -> error, task is reassigned to another robot (bounded);
     - anything after the pick (product on board), or the pick itself:
       robot -> error, task -> failed. A human must recover the product and
       send 'reset_robot'. The scheduler never silently frees a robot that
       may be holding something.
     - QC station failure after retries: task -> failed, QC station and
       carrier -> error.

6. FAIL-SAFE QC. Only an explicit boolean result.pass routes the part. A
   missing/garbled result is a QC *failure* (retried, then escalated), never
   a silent pass.

7. PERSISTENCE. All task/robot state is written to disk after every change
   and restored on start, so a scheduler restart doesn't forget that a robot
   is mid-task with a product on board.

This class is NOT a safety system. E-stops, safety scanners, speed limits
and arm/base interlocks must be enforced by the robot controllers.
"""

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from allocator import AllocationError, TaskAllocator
from robot_registry import RobotRegistry
from task_decomposer import decompose
from validation import InstructionError, SiteConfig, validate_instruction

logger = logging.getLogger("scheduler")

TERMINAL_STATES = ("done", "failed", "cancelled")
PROGRESS_STATES = ("running", "progress")
STATE_VERSION = 2


class TaskScheduler:
    def __init__(self, registry: RobotRegistry, mqtt_client, site: SiteConfig,
                 state_path: Optional[str] = None, clock: Callable[[], float] = time.time):
        self.registry = registry
        self.allocator = TaskAllocator(registry)
        self.mqtt = mqtt_client
        self.site = site
        self._state_path = state_path
        self._clock = clock
        self._lock = threading.RLock()

        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._sub_index: Dict[str, str] = {}            # subtask_id -> task_id
        self._in_flight: Dict[str, Dict[str, Any]] = {}  # subtask_id -> dispatch record
        self._pending: List[Tuple[str, str]] = []        # FIFO of (task_id, subtask_id)
        self._dirty = False

        if state_path:
            self._load_state()

    # =====================================================================
    # Public API
    # =====================================================================
    def submit_instruction(self, instruction: Any) -> str:
        """Validate, decompose and start a task. Raises InstructionError if
        the instruction is invalid (nothing is dispatched in that case)."""
        clean = validate_instruction(
            instruction, self.site,
            inspection_zones=self.registry.zones_with_capability("quality_check"))
        subtasks = decompose(clean)
        task_id = subtasks[0]["task_id"]
        now = self._clock()
        with self._lock:
            self._tasks[task_id] = {
                "task_id": task_id,
                "instruction": clean,
                "request_id": clean.get("request_id"),
                "state": "active",
                "created_at": now,
                "finished_at": None,
                "error": None,
                "subtasks": {s["subtask_id"]: s for s in subtasks},
                "order": [s["subtask_id"] for s in subtasks],
                "completed": [],
                "branch": None,
                "carrier_robot": None,
                "product_on_board": False,
                "reassignments": 0,
            }
            for s in subtasks:
                self._sub_index[s["subtask_id"]] = task_id
            logger.info("Task %s accepted (%s): %d subtasks", task_id, clean["product"], len(subtasks))
            self._event("task_accepted", task_id, instruction=clean)
            self._dispatch_roots(task_id)
            self._commit()
        return task_id

    def on_robot_status(self, robot_id: str, subsystem: str, payload: Any):
        if not isinstance(payload, dict):
            logger.warning("Status from %s/%s is not an object: %r", robot_id, subsystem, payload)
            return
        sid, state = payload.get("subtask_id"), payload.get("state")
        if not isinstance(sid, str) or not isinstance(state, str):
            logger.warning("Status from %s/%s missing subtask_id/state: %r", robot_id, subsystem, payload)
            return
        attempt = payload.get("attempt", 1)

        with self._lock:
            inf = self._in_flight.get(sid)
            if (inf is None or inf["robot_id"] != robot_id or inf["subsystem"] != subsystem
                    or inf["attempt"] != attempt):
                logger.debug("Ignoring stale/duplicate/foreign status %s/%s %s#%s %s",
                             robot_id, subsystem, sid, attempt, state)
                return

            now = self._clock()
            if state == "accepted":
                if inf["accepted_at"] is None:
                    inf["accepted_at"] = now
                    inf["deadline"] = now + self.site.timeout_for(inf["action"])
                    self._commit()
                return
            if state in PROGRESS_STATES:
                return
            if state not in TERMINAL_STATES:
                logger.warning("Unknown state %r from %s/%s for %s", state, robot_id, subsystem, sid)
                return

            del self._in_flight[sid]
            task = self._tasks[inf["task_id"]]
            sub = task["subtasks"][sid]

            if task["state"] != "active":
                self._release_after_inactive(task, sub, robot_id, state)
            elif state == "done":
                self._on_done(task, sub, robot_id, payload)
            else:
                reason = payload.get("error") or f"robot reported {state}"
                self._on_failure(task, sub, robot_id, str(reason))
            self._drain_pending()
            self._commit()

    def on_robot_heartbeat(self, robot_id: str, subsystem: str, payload: Any):
        if not isinstance(payload, dict):
            return
        changed = self.registry.update_health(robot_id, subsystem, payload, now=self._clock())
        if changed:
            with self._lock:
                self._drain_pending()
                self._commit()

    def handle_command(self, cmd: Any) -> Dict[str, Any]:
        """Operator commands (from scheduler/commands)."""
        if not isinstance(cmd, dict) or not isinstance(cmd.get("cmd"), str):
            return {"ok": False, "message": "command must be an object with a 'cmd' string"}
        name = cmd["cmd"]
        with self._lock:
            if name == "cancel_task":
                result = self._cmd_cancel_task(cmd.get("task_id"))
            elif name == "reset_robot":
                result = self._cmd_reset_robot(cmd.get("robot_id"))
            elif name == "set_maintenance":
                result = self._cmd_set_maintenance(cmd.get("robot_id"), bool(cmd.get("on", True)))
            elif name == "status":
                result = {"ok": True, "message": "status published", "snapshot": self.snapshot()}
            else:
                result = {"ok": False, "message": f"unknown command {name!r}"}
            self._drain_pending()
            self._commit()
        logger.info("Command %s -> %s", cmd, result.get("message"))
        return result

    def tick(self, now: Optional[float] = None):
        """Watchdog. Call about once per second."""
        now = self._clock() if now is None else now
        with self._lock:
            for sid, inf in list(self._in_flight.items()):
                if now <= inf["deadline"]:
                    continue
                if inf["accepted_at"] is None:
                    reason = f"no 'accepted' from {inf['robot_id']}/{inf['subsystem']} within " \
                             f"{self.site.timeout_for('accept'):.0f}s"
                else:
                    reason = f"{inf['action']} timed out after {self.site.timeout_for(inf['action']):.0f}s"
                logger.error("Subtask %s#%s: %s", sid, inf["attempt"], reason)
                self.mqtt.publish_cancel(inf["robot_id"], inf["subsystem"], sid, inf["attempt"], reason)
                del self._in_flight[sid]
                task = self._tasks[inf["task_id"]]
                sub = task["subtasks"][sid]
                if task["state"] != "active":
                    self._release_after_inactive(task, sub, inf["robot_id"], "timeout")
                else:
                    self._on_failure(task, sub, inf["robot_id"], reason)
            self._drain_pending()
            self._prune(now)
            self._commit()

    # ---- read-only helpers ------------------------------------------------------
    def task_state(self, task_id: str) -> Optional[str]:
        with self._lock:
            t = self._tasks.get(task_id)
            return t["state"] if t else None

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            t = self._tasks.get(task_id)
            return json.loads(json.dumps(t)) if t else None

    def pending(self) -> List[Tuple[str, str]]:
        with self._lock:
            return list(self._pending)

    def in_flight(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._in_flight.items()}

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = self._clock()
            tasks = []
            for t in self._tasks.values():
                current = [sid for sid, inf in self._in_flight.items() if inf["task_id"] == t["task_id"]]
                tasks.append({
                    "task_id": t["task_id"], "state": t["state"], "product": t["instruction"]["product"],
                    "request_id": t["request_id"], "carrier_robot": t["carrier_robot"],
                    "product_on_board": t["product_on_board"], "branch": t["branch"],
                    "current": [{"subtask_id": s, "action": t["subtasks"][s]["action"],
                                 "zone": t["subtasks"][s]["zone"],
                                 "robot": self._in_flight[s]["robot_id"]} for s in current],
                    "error": t["error"], "created_at": t["created_at"], "finished_at": t["finished_at"],
                })
            tasks.sort(key=lambda x: x["created_at"], reverse=True)
            return {"ts": now, "robots": self.registry.fleet_view(now),
                    "tasks": tasks[:50], "pending": len(self._pending)}

    # =====================================================================
    # Internals (self._lock held)
    # =====================================================================
    def _dispatch_roots(self, task_id: str):
        task = self._tasks[task_id]
        for sid in task["order"]:
            if task["subtasks"][sid]["depends_on"] is None:
                self._try_dispatch(task_id, sid)

    def _try_dispatch(self, task_id: str, sid: str, from_queue: bool = False) -> bool:
        task = self._tasks[task_id]
        if task["state"] != "active" or sid in self._in_flight:
            return False
        sub = task["subtasks"][sid]

        if sub["group"] == "carrier" and task["carrier_robot"]:
            robot = self.registry.get(task["carrier_robot"])
            if robot is None:
                self._fail_task(task, f"carrier robot {task['carrier_robot']} no longer in registry")
                return False
        else:
            try:
                robot = self.allocator.allocate(sub)
            except AllocationError as e:
                if (task_id, sid) not in self._pending:
                    self._pending.append((task_id, sid))
                    if not from_queue:  # announce once, not on every retry pass
                        logger.info("%s -- queued", e)
                        self._event("subtask_queued", task_id, subtask_id=sid, action=sub["action"])
                return False
            if sub["group"] == "carrier":
                task["carrier_robot"] = robot.id

        subsystem = robot.subsystem_for_capability(sub["requires"][0])
        now = self._clock()
        accept_timeout = self.site.timeout_for("accept")
        message = dict(sub)
        message.update(type="task", dispatched_at=now, expires_at=now + accept_timeout)
        self._in_flight[sid] = {
            "task_id": task_id, "robot_id": robot.id, "subsystem": subsystem,
            "attempt": sub["attempt"], "action": sub["action"],
            "dispatched_at": now, "accepted_at": None, "deadline": now + accept_timeout,
        }
        self._dirty = True
        self.mqtt.publish_task(robot.id, subsystem, message)
        self._event("subtask_dispatched", task_id, subtask_id=sid, action=sub["action"],
                    zone=sub["zone"], robot_id=robot.id, attempt=sub["attempt"])
        return True

    def _drain_pending(self):
        if not self._pending:
            return
        queue, self._pending = self._pending, []
        for task_id, sid in queue:
            task = self._tasks.get(task_id)
            if not task or task["state"] != "active" or sid in self._in_flight or sid in task["completed"]:
                continue
            self._try_dispatch(task_id, sid, from_queue=True)  # re-queues itself if still blocked
        self._dirty = True

    def _on_done(self, task, sub, robot_id: str, payload: Dict[str, Any]):
        sid = sub["subtask_id"]
        if sub["action"] == "quality_check":
            result = payload.get("result")
            passed = result.get("pass") if isinstance(result, dict) else None
            if not isinstance(passed, bool):
                self._on_failure(task, sub, robot_id, f"quality_check gave no boolean 'pass' (result={result!r})")
                return
            task["branch"] = "success" if passed else "fail"
            logger.info("Task %s QC -> %s", task["task_id"], task["branch"])
            self._event("qc_result", task["task_id"], passed=passed, robot_id=robot_id)

        task["completed"].append(sid)
        if sub["group"] == "carrier" and sub["action"] == "pick":
            task["product_on_board"] = True
        if sub["releases_robot"]:
            if sub["group"] == "carrier":
                task["product_on_board"] = False
                task["carrier_robot"] = None
            self.registry.mark_idle(robot_id)
        self._dirty = True

        for cand_id in task["order"]:
            cand = task["subtasks"][cand_id]
            if cand["depends_on"] != sid:
                continue
            if cand["branch"] is not None and cand["branch"] != task["branch"]:
                continue
            self._try_dispatch(task["task_id"], cand_id)

        if self._task_complete(task):
            task["state"] = "completed"
            task["finished_at"] = self._clock()
            logger.info("Task %s complete (branch=%s)", task["task_id"], task["branch"])
            self._event("task_completed", task["task_id"], branch=task["branch"])

    def _on_failure(self, task, sub, robot_id: str, reason: str):
        sid, action = sub["subtask_id"], sub["action"]
        task_id = task["task_id"]
        self._dirty = True

        # "attempt" is the dispatch identity robots de-duplicate on, so it only
        # ever increases; "tries" is the retry budget on the current robot.
        tries = sub.get("tries", sub["attempt"])
        if tries <= self.site.retries_for(action):
            sub["attempt"] += 1
            sub["tries"] = tries + 1
            logger.warning("Task %s: %s failed on %s (%s) -- retry #%d",
                           task_id, sid, robot_id, reason, tries)
            self._event("subtask_retry", task_id, subtask_id=sid, action=action,
                        robot_id=robot_id, reason=reason, attempt=sub["attempt"])
            if sub["group"] == "inspector":
                self.registry.mark_idle(robot_id)  # may be re-allocated (possibly the other station)
            self._try_dispatch(task_id, sid)
            return

        logger.error("Task %s: %s failed on %s after retries: %s", task_id, sid, robot_id, reason)
        if sub["group"] == "inspector":
            self.registry.set_status(robot_id, "error", f"QC failed: {reason}")
            if task["carrier_robot"]:
                self.registry.set_status(task["carrier_robot"], "error",
                                         f"holding product of failed task {task_id} at QC")
            self._fail_task(task, f"{action} failed: {reason}")
            return

        # carrier
        pre_pick = not task["product_on_board"] and action == "navigate"
        self.registry.set_status(robot_id, "error", f"{action} failed: {reason}")
        if pre_pick and task["reassignments"] < self.site.max_task_reassignments:
            task["reassignments"] += 1
            task["carrier_robot"] = None
            task["completed"] = []
            for s in task["subtasks"].values():
                s["attempt"] += 1   # new identity: never reuse an id a robot has already seen
                s["tries"] = 1
            logger.warning("Task %s: nothing on board yet, reassigning to another robot (%d/%d)",
                           task_id, task["reassignments"], self.site.max_task_reassignments)
            self._event("task_reassigned", task_id, from_robot=robot_id, reason=reason)
            self._dispatch_roots(task_id)
            return
        if task["product_on_board"] or action in ("pick", "place"):
            reason = f"{reason} (product may be on robot {robot_id}: recover it, then reset_robot)"
        self._fail_task(task, f"{action} failed: {reason}")

    def _fail_task(self, task, error: str, state: str = "failed"):
        task["state"] = state
        task["error"] = error
        task["finished_at"] = self._clock()
        self._pending = [(t, s) for (t, s) in self._pending if t != task["task_id"]]
        for sid, inf in self._in_flight.items():
            if inf["task_id"] == task["task_id"]:
                self.mqtt.publish_cancel(inf["robot_id"], inf["subsystem"], sid, inf["attempt"],
                                         f"task {state}")
        self._dirty = True
        self._event(f"task_{state}", task["task_id"], error=error, carrier_robot=task["carrier_robot"],
                    product_on_board=task["product_on_board"])

    def _release_after_inactive(self, task, sub, robot_id: str, state: str):
        """A terminal status/timeout arrived for a task that is already
        failed/cancelled. Decide whether the robot can go back to idle."""
        if sub["group"] == "inspector":
            self.registry.mark_idle(robot_id)
            return
        action = sub["action"]
        if action == "pick" and state == "done":
            task["product_on_board"] = True
        elif action == "place" and state == "done":
            task["product_on_board"] = False
        unknown_gripper = action in ("pick", "place") and state != "done"
        if task["product_on_board"] or unknown_gripper:
            self.registry.set_status(robot_id, "error",
                                     f"task {task['task_id']} {task['state']} with product possibly on board")
        else:
            self.registry.mark_idle(robot_id)
        task["carrier_robot"] = None
        self._dirty = True

    def _task_complete(self, task) -> bool:
        if task["branch"] is None:
            return False
        done = set(task["completed"])
        return all(s["subtask_id"] in done for s in task["subtasks"].values()
                   if s["branch"] is None or s["branch"] == task["branch"])

    # ---- operator commands ------------------------------------------------------
    def _cmd_cancel_task(self, task_id) -> Dict[str, Any]:
        task = self._tasks.get(task_id) if isinstance(task_id, str) else None
        if task is None:
            return {"ok": False, "message": f"unknown task {task_id!r}"}
        if task["state"] != "active":
            return {"ok": False, "message": f"task {task_id} is already {task['state']}"}
        busy_robots = {inf["robot_id"] for inf in self._in_flight.values() if inf["task_id"] == task_id}
        carrier = task["carrier_robot"]
        self._fail_task(task, "cancelled by operator", state="cancelled")
        # Robots with nothing in flight are released now; the rest when their
        # agent confirms the cancel (or the watchdog times them out).
        if carrier and carrier not in busy_robots:
            if task["product_on_board"]:
                self.registry.set_status(carrier, "error", f"cancelled task {task_id} left product on board")
            else:
                self.registry.mark_idle(carrier)
            task["carrier_robot"] = None
        return {"ok": True, "message": f"task {task_id} cancelled"}

    def _cmd_reset_robot(self, robot_id) -> Dict[str, Any]:
        robot = self.registry.get(robot_id) if isinstance(robot_id, str) else None
        if robot is None:
            return {"ok": False, "message": f"unknown robot {robot_id!r}"}
        if any(inf["robot_id"] == robot_id for inf in self._in_flight.values()):
            return {"ok": False, "message": f"{robot_id} has a subtask in flight; cancel the task first"}
        holders = [t["task_id"] for t in self._tasks.values()
                   if t["state"] == "active" and t["carrier_robot"] == robot_id]
        if holders:
            return {"ok": False, "message": f"{robot_id} is the carrier of active task(s) {holders}; cancel first"}
        self.registry.mark_idle(robot_id)
        self._dirty = True
        self._event("robot_reset", None, robot_id=robot_id)
        return {"ok": True, "message": f"{robot_id} reset to idle"}

    def _cmd_set_maintenance(self, robot_id, on: bool) -> Dict[str, Any]:
        robot = self.registry.get(robot_id) if isinstance(robot_id, str) else None
        if robot is None:
            return {"ok": False, "message": f"unknown robot {robot_id!r}"}
        if on:
            if robot.status == "busy":
                return {"ok": False, "message": f"{robot_id} is busy; cancel its task first"}
            self.registry.set_status(robot_id, "maintenance", "set by operator")
        else:
            if robot.status != "maintenance":
                return {"ok": False, "message": f"{robot_id} is {robot.status}, not in maintenance"}
            self.registry.mark_idle(robot_id)
        self._dirty = True
        return {"ok": True, "message": f"{robot_id} maintenance={'on' if on else 'off'}"}

    # ---- events, persistence, housekeeping ------------------------------------------
    def _event(self, event: str, task_id: Optional[str], **fields):
        payload = {"event": event, "ts": self._clock(), "task_id": task_id}
        if task_id and task_id in self._tasks:
            payload["request_id"] = self._tasks[task_id].get("request_id")
        payload.update(fields)
        try:
            self.mqtt.publish_event(payload)
        except Exception:
            logger.exception("Failed to publish event %s", event)

    def _prune(self, now: float):
        finished = [t for t in self._tasks.values() if t["state"] != "active"]
        excess = len(finished) - self.site.finished_task_retention
        if excess <= 0:
            return
        finished.sort(key=lambda t: t["finished_at"] or 0)
        for t in finished[:excess]:
            if any(inf["task_id"] == t["task_id"] for inf in self._in_flight.values()):
                continue  # still waiting on a cancel confirmation
            for sid in t["subtasks"]:
                self._sub_index.pop(sid, None)
            del self._tasks[t["task_id"]]
        self._dirty = True

    def _commit(self):
        if not self._dirty:
            return
        self._dirty = False
        if self._state_path:
            try:
                self._save_state()
            except Exception:
                logger.exception("Could not persist scheduler state to %s", self._state_path)

    def _save_state(self):
        data = {
            "version": STATE_VERSION,
            "saved_at": self._clock(),
            "tasks": self._tasks,
            "in_flight": self._in_flight,
            "pending": self._pending,
            "robots": self.registry.snapshot_statuses(),
        }
        directory = os.path.dirname(self._state_path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = self._state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._state_path)

    def _load_state(self):
        if not os.path.exists(self._state_path):
            return
        with open(self._state_path, "r") as f:
            data = json.load(f)
        if data.get("version") != STATE_VERSION:
            raise RuntimeError(f"{self._state_path} has state version {data.get('version')}, "
                               f"expected {STATE_VERSION}; move it aside to start fresh")
        self._tasks = data.get("tasks", {})
        self._sub_index = {sid: tid for tid, t in self._tasks.items() for sid in t["subtasks"]}
        self._pending = [tuple(p) for p in data.get("pending", [])]
        self._in_flight = data.get("in_flight", {})
        self.registry.restore_statuses(data.get("robots", {}))
        # Give robots time to reconnect and re-send results before the
        # watchdog starts expiring what was in flight while we were down.
        grace_until = self._clock() + self.site.startup_grace_s
        for inf in self._in_flight.values():
            inf["deadline"] = max(inf["deadline"], grace_until)
        active = sum(1 for t in self._tasks.values() if t["state"] == "active")
        logger.warning("Restored scheduler state: %d active task(s), %d in flight, %d pending",
                       active, len(self._in_flight), len(self._pending))
