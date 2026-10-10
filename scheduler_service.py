"""
scheduler_service.py
--------------------
Production entry point for the scheduler. MQTT is the only transport.

    scheduler/instructions   (sub)  new tasks from the reasoning agent / HMI
    scheduler/commands       (sub)  operator commands
    robots/+/+/status        (sub)  task acks from robot agents
    robots/+/+/state         (sub)  heartbeats (retained) + last-will
    robots/<id>/<sub>/task   (pub)  tasks and cancels
    scheduler/events         (pub)  lifecycle events (accepted/rejected/completed/failed/...)
    scheduler/status         (pub)  retained fleet + task snapshot every few seconds

Run only ONE scheduler per site (a lock file next to the state file
enforces this per host).

Run:
    python3 scheduler_service.py
"""

import fcntl
import logging
import os
import signal
import threading
import time

import config
from mqtt_client import SchedulerMQTTClient
from robot_registry import RobotRegistry
from scheduler import TaskScheduler
from validation import InstructionError, SiteConfig

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("scheduler_service")

STATUS_PERIOD_S = 5.0


def acquire_single_instance_lock(state_path: str):
    directory = os.path.dirname(state_path) or "."
    os.makedirs(directory, exist_ok=True)
    lock_file = open(state_path + ".lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"Another scheduler is already running (lock {state_path}.lock)")
    return lock_file


def main():
    lock = acquire_single_instance_lock(config.SCHEDULER_STATE_PATH)  # noqa: F841 (held for process life)
    site = SiteConfig.load(config.SITE_CONFIG_PATH)
    registry = RobotRegistry(config.ROBOT_METADATA_PATH,
                             require_heartbeat=config.REQUIRE_HEARTBEAT,
                             heartbeat_timeout_s=site.heartbeat_timeout_s,
                             min_battery_percent=site.min_battery_percent)
    if not config.REQUIRE_HEARTBEAT:
        logger.warning("REQUIRE_HEARTBEAT is off: robots will be allocated without liveness checks")

    holder = {}

    def on_instruction(instruction):
        sched = holder["scheduler"]
        try:
            task_id = sched.submit_instruction(instruction)
            logger.info("Submitted task %s", task_id)
        except InstructionError as e:
            logger.error("Rejected instruction %r: %s", instruction, e)
            request_id = instruction.get("request_id") if isinstance(instruction, dict) else None
            mqtt_client.publish_event({"event": "task_rejected", "ts": time.time(),
                                       "request_id": request_id, "error": str(e),
                                       "instruction": instruction})

    def on_command(cmd):
        result = holder["scheduler"].handle_command(cmd)
        if "snapshot" in result:
            mqtt_client.publish_fleet_status(result.pop("snapshot"))
        mqtt_client.publish_event({"event": "command_result", "ts": time.time(),
                                   "command": cmd, **result})

    mqtt_client = SchedulerMQTTClient(
        client_id="task_scheduler_service",
        on_status=lambda r, s, p: holder["scheduler"].on_robot_status(r, s, p),
        on_state=lambda r, s, p: holder["scheduler"].on_robot_heartbeat(r, s, p),
        on_instruction=on_instruction,
        on_command=on_command,
    )
    scheduler = TaskScheduler(registry, mqtt_client, site, state_path=config.SCHEDULER_STATE_PATH)
    holder["scheduler"] = scheduler

    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())

    mqtt_client.connect()
    logger.info("Scheduler running. Instructions on '%s', commands on '%s'.",
                config.INSTRUCTION_TOPIC, config.COMMAND_TOPIC)

    last_status = 0.0
    try:
        while not stop_event.wait(1.0):
            try:
                scheduler.tick()
                now = time.time()
                if now - last_status >= STATUS_PERIOD_S and mqtt_client.connected:
                    mqtt_client.publish_fleet_status(scheduler.snapshot())
                    last_status = now
            except Exception:
                logger.exception("Watchdog tick failed")
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Shutting down")
        mqtt_client.disconnect()


if __name__ == "__main__":
    main()
