"""
scheduler_node.py
------------------
ROS2 node for the task planning & scheduling agent. Subscribes to the same
task-queue topic your agent_llm_node publishes structured instructions to
(std_msgs/String, JSON payload — matches the "Published automated task: {...}"
log line from your reasoning agent), runs it through TaskScheduler, and lets
the scheduler talk to individual robots over MQTT.

Configuration (broker host/port/credentials, the reasoning-agent topic name,
metadata file path) all comes from config.py, which reads environment
variables with sensible defaults -- see .env.example. Nothing here needs
editing to move between machines; set the env vars instead.

Run:
    ros2 run task_scheduler_pkg scheduler_node
or directly:
    python3 scheduler_node.py
"""

import json
import logging

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

import config
from robot_registry import RobotRegistry
from mqtt_client import SchedulerMQTTClient
from scheduler import TaskScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler_node")


class SchedulerNode(Node):
    def __init__(self):
        super().__init__("task_scheduler_node")

        self.registry = RobotRegistry(config.ROBOT_METADATA_PATH)
        self.mqtt_client = SchedulerMQTTClient(
            broker_host=config.MQTT_BROKER_HOST,
            broker_port=config.MQTT_BROKER_PORT,
            client_id="task_scheduler_node",
            username=config.MQTT_USERNAME,
            password=config.MQTT_PASSWORD,
            on_status=self._on_robot_status,
        )
        self.scheduler = TaskScheduler(self.registry, self.mqtt_client)
        self.mqtt_client.connect()

        self.subscription = self.create_subscription(
            String, config.REASONING_AGENT_TOPIC, self._on_instruction, 10
        )
        self.get_logger().info(
            f"Task scheduler ready, listening on '{config.REASONING_AGENT_TOPIC}'"
        )

    def _on_instruction(self, msg: String):
        try:
            instruction = json.loads(msg.data)
        except json.JSONDecodeError:
            self.get_logger().error(f"Bad JSON on {config.REASONING_AGENT_TOPIC}: {msg.data}")
            return
        task_id = self.scheduler.submit_instruction(instruction)
        self.get_logger().info(f"Submitted task {task_id}: {instruction}")

    def _on_robot_status(self, robot_id: str, subsystem: str, payload: dict):
        # bridge MQTT callback (runs on paho's thread) into the scheduler
        self.scheduler.on_robot_status(robot_id, subsystem, payload)

    def destroy_node(self):
        self.mqtt_client.disconnect()
        super().destroy_node()


def main():
    rclpy.init()
    node = SchedulerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
