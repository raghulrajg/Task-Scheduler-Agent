"""
scheduler_service.py
----------------------
The production entry point. No ROS2, no rclpy -- MQTT is the only
transport, for the instruction intake as well as robot dispatch:

    scheduler/instructions   (subscribe)  <- your reasoning agent publishes
                                              its JSON instruction here
    robots/+/+/status        (subscribe)  <- robot subsystems ack here
    robots/<id>/<sub>/task   (publish)    -> scheduler dispatches here

Your Ollama-based reasoning agent needs to publish its instruction JSON as
a plain MQTT message on config.INSTRUCTION_TOPIC (default
"scheduler/instructions") instead of a ROS2 topic. That's the one change
needed on that side to drop ROS2 from this pipeline entirely.

Run:
    python3 scheduler_service.py
"""

import logging
import time

import config
from robot_registry import RobotRegistry
from mqtt_client import SchedulerMQTTClient
from scheduler import TaskScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
logger = logging.getLogger("scheduler_service")


def main():
    registry = RobotRegistry(config.ROBOT_METADATA_PATH)

    def on_instruction(instruction: dict):
        try:
            task_id = scheduler.submit_instruction(instruction)
            logger.info("Submitted task %s from instruction: %s", task_id, instruction)
        except ValueError as e:
            logger.error("Could not decompose instruction %s: %s", instruction, e)

    def on_status(robot_id, subsystem, payload):
        scheduler.on_robot_status(robot_id, subsystem, payload)

    mqtt_client = SchedulerMQTTClient(
        broker_host=config.MQTT_BROKER_HOST,
        broker_port=config.MQTT_BROKER_PORT,
        client_id="task_scheduler_service",
        username=config.MQTT_USERNAME,
        password=config.MQTT_PASSWORD,
        on_status=on_status,
        on_instruction=on_instruction,
        instruction_topic=config.INSTRUCTION_TOPIC,
    )
    scheduler = TaskScheduler(registry, mqtt_client)

    logger.info("Connecting to %s:%s as %s ...", config.MQTT_BROKER_HOST,
                config.MQTT_BROKER_PORT, config.MQTT_USERNAME)
    mqtt_client.connect()
    logger.info("Ready. Listening on '%s' for instructions, "
                "'robots/+/+/status' for robot acks.", config.INSTRUCTION_TOPIC)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        mqtt_client.disconnect()


if __name__ == "__main__":
    main()
