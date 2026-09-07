"""
test_scheduler_offline.py
--------------------------
Exercises the full decompose -> allocate -> dispatch -> ack -> branch ->
complete flow with a fake MQTT client, so you can validate the scheduling
logic before wiring up ROS2 + a real broker.

Run: python3 test_scheduler_offline.py
"""

import logging
from robot_registry import RobotRegistry
from scheduler import TaskScheduler

logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")


class FakeMQTTClient:
    """Records publishes and lets the test drive fake robot acks."""
    def __init__(self):
        self.published = []  # list of (robot_id, subtask)

    def publish_task(self, robot_id, subtask, qos=1):
        self.published.append((robot_id, subtask))
        print(f"  -> dispatched '{subtask['action']}' on {robot_id} "
              f"({subtask['subtask_id']})")


def main():
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

    # Drive the fake robots through their acks, in the order they were dispatched
    def ack_next(state="done", result=None):
        robot_id, subtask = mqtt.published[-1]
        payload = {"subtask_id": subtask["subtask_id"], "state": state}
        if result is not None:
            payload["result"] = result
        print(f"  <- ack from {robot_id}: {payload}")
        scheduler.on_robot_status(robot_id, payload)

    ack_next()                                    # navigate to source
    ack_next()                                    # pick
    ack_next()                                    # navigate to quality station
    ack_next(result={"pass": True})               # quality_check -> resolves "success" branch
    ack_next()                                    # navigate to package_area (success branch)
    ack_next()                                    # place (success branch)

    print("\nTask complete:", scheduler._task_complete(task_id))
    print("Total MQTT publishes:", len(mqtt.published))
    assert scheduler._task_complete(task_id)
    # fail-branch steps should never have been dispatched
    dispatched_ids = {s["subtask_id"] for _, s in mqtt.published}
    assert not any(sid.endswith("-fail") or "-place-fail" in sid for sid in dispatched_ids)
    print("OK: fail-branch steps were correctly skipped.")


if __name__ == "__main__":
    main()
