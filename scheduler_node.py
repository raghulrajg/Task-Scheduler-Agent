"""
scheduler_node.py
------------------
ROS2 node for the task planning & scheduling agent. Subscribes to the same
task-queue topic your agent_llm_node publishes structured instructions to
(std_msgs/String, JSON payload — matches the "Published automated task: {...}"
log line from your reasoning agent), runs it through TaskScheduler, and lets
the scheduler talk to individual robots over MQTT.

Adjust REASONING_AGENT_TOPIC below to whatever your agent_llm_node actually
publishes on (it wasn't fully visible in your setup — this is the one thing
to confirm).

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

from robot_registry import RobotRegistry
from mqtt_client import SchedulerMQTTClient
from scheduler import TaskScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler_node")

REASONING_AGENT_TOPIC = "robot_task_queue"   # <-- confirm/rename to match agent_llm_node's publisher
ROBOT_METADATA_PATH = "robots_metadata.json"
MQTT_BROKER_HOST = "localhost"
MQTT_BROKER_PORT = 1883


class SchedulerNode(Node):
    def __init__(self):
        super().__init__("task_scheduler_node")

        self.registry = RobotRegistry(ROBOT_METADATA_PATH)
        self.mqtt_client = SchedulerMQTTClient(
            broker_host=MQTT_BROKER_HOST,
            broker_port=MQTT_BROKER_PORT,
            client_id="task_scheduler_node",
            on_status=self._on_robot_status,
        )
        self.scheduler = TaskScheduler(self.registry, self.mqtt_client)
        self.mqtt_client.connect()

        self.subscription = self.create_subscription(
            String, REASONING_AGENT_TOPIC, self._on_instruction, 10
        )
        self.get_logger().info(
            f"Task scheduler ready, listening on '{REASONING_AGENT_TOPIC}'"
        )

    def _on_instruction(self, msg: String):
        try:
            instruction = json.loads(msg.data)
        except json.JSONDecodeError:
            self.get_logger().error(f"Bad JSON on {REASONING_AGENT_TOPIC}: {msg.data}")
            return
        task_id = self.scheduler.submit_instruction(instruction)
        self.get_logger().info(f"Submitted task {task_id}: {instruction}")

    def _on_robot_status(self, robot_id: str, payload: dict):
        # bridge MQTT callback (runs on paho's thread) into the scheduler
        self.scheduler.on_robot_status(robot_id, payload)

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
