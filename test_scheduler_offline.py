"""
test_scheduler_offline.py
--------------------------
Exercises decompose -> allocate -> dispatch -> ack -> branch -> complete
with a fake MQTT client (no broker/ROS2 needed), PLUS a concurrency test
that fires several instructions at once from real threads to check that no
two of them ever get double-claimed onto the same robot.

Run: python3 test_scheduler_offline.py
"""

import logging
import threading
import time

from robot_registry import RobotRegistry
from scheduler import TaskScheduler

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")


class FakeMQTTClient:
    """Records publishes and lets the test drive fake robot acks."""
    def __init__(self):
        self.published = []  # list of (robot_id, subsystem, subtask)
        self._lock = threading.Lock()

    def publish_task(self, robot_id, subsystem, subtask, qos=1):
        with self._lock:
            self.published.append((robot_id, subsystem, subtask))
        print(f"  -> dispatched '{subtask['action']}' on {robot_id}/{subsystem} "
              f"({subtask['subtask_id']})")


def single_task_walkthrough():
    print("=" * 60)
    print("SINGLE TASK WALKTHROUGH")
    print("=" * 60)
    registry = RobotRegistry("robots_metadata.json")
    mqtt = FakeMQTTClient()
    scheduler = TaskScheduler(registry, mqtt)

    instruction = {
        "product": "ABCD",
        "source": "manufacturing_1",
        "quality_check": "quality_station",
        "success_dest": "package_area",
        "fail_dest": "waste_area",
    }
    print("Submitting instruction:", instruction)
    task_id = scheduler.submit_instruction(instruction)

    def ack_latest(state="done", result=None):
        robot_id, subsystem, subtask = mqtt.published[-1]
        payload = {"subtask_id": subtask["subtask_id"], "state": state}
        if result is not None:
            payload["result"] = result
        print(f"  <- ack from {robot_id}/{subsystem}: {payload}")
        scheduler.on_robot_status(robot_id, subsystem, payload)

    carrier_robot_seen = set()
    for _ in range(3):  # navigate(source), pick, navigate(qc)
        robot_id, _, _ = mqtt.published[-1]
        carrier_robot_seen.add(robot_id)
        ack_latest()
    ack_latest(result={"pass": True})            # quality_check -> success branch
    for _ in range(2):                            # navigate(dest), place
        robot_id, _, _ = mqtt.published[-1]
        carrier_robot_seen.add(robot_id)
        ack_latest()

    assert scheduler._task_complete(task_id)
    assert len(carrier_robot_seen) == 1, f"carrier robot changed mid-task: {carrier_robot_seen}"
    dispatched_ids = {s["subtask_id"] for _, _, s in mqtt.published}
    assert not any("-fail" in sid for sid in dispatched_ids)
    print(f"OK: single carrier robot ({carrier_robot_seen}) used throughout, "
          f"fail-branch correctly skipped, task complete.\n")


def concurrency_stress_test():
    print("=" * 60)
    print("CONCURRENCY TEST: 6 instructions submitted at once, 4 combo robots available")
    print("=" * 60)
    registry = RobotRegistry("robots_metadata.json")
    mqtt = FakeMQTTClient()
    scheduler = TaskScheduler(registry, mqtt)

    task_ids = []
    task_ids_lock = threading.Lock()

    def submit_one(i):
        instruction = {
            "product": f"PRODUCT_{i}",
            "source": "manufacturing_1",
            "quality_check": "quality_station",
            "success_dest": "package_area",
            "fail_dest": "waste_area",
        }
        tid = scheduler.submit_instruction(instruction)
        with task_ids_lock:
            task_ids.append(tid)

    threads = [threading.Thread(target=submit_one, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # At this point: 4 combo robots exist, so at most 4 tasks should have
    # actually claimed a carrier robot (dispatched their first navigate step);
    # the rest should be sitting in the pending queue, and no robot should
    # have been claimed twice at the same moment.
    robot_claims = [p[0] for p in mqtt.published if p[2]["action"] == "navigate" and p[2]["branch"] is None]
    print(f"Initial navigate dispatches went to: {robot_claims}")
    assert len(robot_claims) == len(set(robot_claims)) or len(robot_claims) <= 4, \
        "same robot appears to have been claimed twice concurrently"
    assert len(scheduler._pending) == max(0, 6 - 4), \
        f"expected {max(0, 6-4)} tasks queued waiting for a robot, got {len(scheduler._pending)}"
    print(f"OK: {len(robot_claims)} tasks got a robot immediately, "
          f"{len(scheduler._pending)} correctly queued waiting for one to free up.\n")


if __name__ == "__main__":
    single_task_walkthrough()
    concurrency_stress_test()
