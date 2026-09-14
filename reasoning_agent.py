"""
reasoning_agent.py
--------------------
Converted from your agent_llm_node.py ROS2 node. Same job, no rclpy:

    HMI / mobile app  --(MQTT: hmi/user_prompt)-->  this agent
                                                          |
                                                    calls Ollama
                                                          |
    this agent  --(MQTT: scheduler/instructions)-->  scheduler_service.py

Where it maps onto the old node:
  - self.subscription on '/hmi/user_prompt'  -> MQTT subscribe on
    config.HMI_PROMPT_TOPIC
  - self.publisher_ on 'robot_task_queue'    -> MQTT publish on
    config.INSTRUCTION_TOPIC (scheduler_service.py is already listening here)
  - parse_user_intent()                       -> unchanged logic, same Ollama
    call, same system prompt; just points at config.OLLAMA_HOST instead of
    the hardcoded 172.16.13.90

Run:
    python3 reasoning_agent.py
"""

import json
import logging
import time
import urllib.request

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
logger = logging.getLogger("reasoning_agent")

SYSTEM_PROMPT = """
You are a strict factory automation AI. Extract the job details from the user prompt into JSON.
CRITICAL RULE: For locations, you MUST ONLY choose from this exact list:
["manufacturing_1", "quality_station", "package_area", "waste_area", "rest_position"]

Required JSON keys:
- "product": (string) name of product
- "source": (string) select one valid location
- "quality_check": (string) select one valid location (default to 'quality_station' if implied)
- "success_dest": (string) select one valid location
- "fail_dest": (string) select one valid location (default to 'waste_area' if not specified)
"""


def parse_user_intent(prompt_text: str) -> str:
    """Calls Ollama's /api/generate with format=json (unchanged from the
    original node), returns the raw JSON string response, or None on error."""
    url = f"http://{config.OLLAMA_HOST}:{config.OLLAMA_PORT}/api/generate"

    data = json.dumps({
        "model": config.OLLAMA_MODEL,
        "prompt": prompt_text,
        "system": SYSTEM_PROMPT,
        "stream": False,
        "format": "json",
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
            return result.get("response", "{}")
    except Exception as e:
        logger.error("Ollama API error: %s", e)
        return None


class ReasoningAgent:
    def __init__(self):
        import paho.mqtt.client as mqtt  # lazy import, same pattern as the rest of the package
        self._client = mqtt.Client(client_id="reasoning_agent", clean_session=True)
        if config.MQTT_USERNAME:
            self._client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc):
        logger.info("Connected to MQTT broker (rc=%s)", rc)
        client.subscribe(config.HMI_PROMPT_TOPIC, qos=1)
        logger.info("Agent ready. Listening on '%s' for prompts...", config.HMI_PROMPT_TOPIC)

    def _on_message(self, client, userdata, msg):
        user_text = msg.payload.decode("utf-8")
        logger.info("Received prompt: '%s'", user_text)

        parsed_json = parse_user_intent(user_text)
        if not parsed_json:
            return

        client.publish(config.INSTRUCTION_TOPIC, parsed_json, qos=1)
        logger.info("Published instruction to %s: %s", config.INSTRUCTION_TOPIC, parsed_json)

    def run_forever(self):
        self._client.connect(config.MQTT_BROKER_HOST, config.MQTT_BROKER_PORT, keepalive=60)
        self._client.loop_start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self._client.loop_stop()
            self._client.disconnect()


def main():
    ReasoningAgent().run_forever()


if __name__ == "__main__":
    main()
