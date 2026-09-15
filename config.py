"""
config.py
---------
Single source of truth for deployment settings, read from environment
variables (with the values you gave as defaults, so it still runs out of
the box). This is what makes the package deployable across machines without
editing source: each machine (scheduler host, each robot's onboard PC) gets
its own .env instead of hardcoded values baked into the scripts.

Loading order: real environment variables > .env file (if python-dotenv is
installed and a .env file exists) > the defaults below.
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # no-op if there's no .env file; never errors
except ImportError:
    pass  # python-dotenv not installed -- real env vars still work fine

MQTT_BROKER_HOST = os.environ.get("MQTT_BROKER_HOST", "172.25.200.57")
MQTT_BROKER_PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "raghulrajg")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "Gr2_nemam")

ROBOT_METADATA_PATH = os.environ.get("ROBOT_METADATA_PATH", "robots_metadata.json")

# The reasoning agent publishes its JSON instruction as a plain MQTT message
# on this topic instead of a ROS2 std_msgs/String topic.
INSTRUCTION_TOPIC = os.environ.get("INSTRUCTION_TOPIC", "scheduler/instructions")

# The reasoning agent (reasoning_agent.py) listens for raw user prompts (from
# an HMI / mobile app / voice interface) on this MQTT topic, and turns each
# one into an instruction it publishes on INSTRUCTION_TOPIC above.
HMI_PROMPT_TOPIC = os.environ.get("HMI_PROMPT_TOPIC", "hmi/user_prompt")

# Ollama runs on its own host, separate from the MQTT broker.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "172.16.13.90")
OLLAMA_PORT = int(os.environ.get("OLLAMA_PORT", "11434"))
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3:8b")
