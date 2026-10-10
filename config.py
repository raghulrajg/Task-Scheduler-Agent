"""
config.py
---------
Deployment settings, read from environment variables (or a local .env file).

Loading order: real environment variables > .env file (if python-dotenv is
installed) > the defaults below.

There are deliberately NO credential defaults in this file. Broker username
and password must come from the environment / .env on each machine, and .env
must never be committed (see .gitignore and .env.example).
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # no-op if there's no .env file
except ImportError:
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---- MQTT broker ----------------------------------------------------------
MQTT_BROKER_HOST = os.environ.get("MQTT_BROKER_HOST", "localhost")
MQTT_BROKER_PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME") or None
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD") or None
MQTT_KEEPALIVE_S = int(os.environ.get("MQTT_KEEPALIVE_S", "30"))

# TLS. Strongly recommended anywhere the broker is reachable from Wi-Fi.
# Typical broker port for TLS is 8883.
MQTT_TLS = _bool("MQTT_TLS", False)
MQTT_TLS_CA_CERT = os.environ.get("MQTT_TLS_CA_CERT") or None     # path to CA .crt
MQTT_TLS_CLIENT_CERT = os.environ.get("MQTT_TLS_CLIENT_CERT") or None
MQTT_TLS_CLIENT_KEY = os.environ.get("MQTT_TLS_CLIENT_KEY") or None

# ---- Files ----------------------------------------------------------------
ROBOT_METADATA_PATH = os.environ.get("ROBOT_METADATA_PATH", "robots_metadata.json")
SITE_CONFIG_PATH = os.environ.get("SITE_CONFIG_PATH", "site.json")
# Scheduler state is persisted here so a restart doesn't forget which robot
# is physically carrying which product.
SCHEDULER_STATE_PATH = os.environ.get("SCHEDULER_STATE_PATH", "state/scheduler_state.json")

# ---- Topics -----------------------------------------------------------------
INSTRUCTION_TOPIC = os.environ.get("INSTRUCTION_TOPIC", "scheduler/instructions")
COMMAND_TOPIC = os.environ.get("COMMAND_TOPIC", "scheduler/commands")      # operator commands
EVENT_TOPIC = os.environ.get("EVENT_TOPIC", "scheduler/events")            # task lifecycle events
FLEET_STATUS_TOPIC = os.environ.get("FLEET_STATUS_TOPIC", "scheduler/status")  # retained snapshot
HMI_PROMPT_TOPIC = os.environ.get("HMI_PROMPT_TOPIC", "hmi/user_prompt")
HMI_FEEDBACK_TOPIC = os.environ.get("HMI_FEEDBACK_TOPIC", "hmi/feedback")
HMI_CONFIRM_TOPIC = os.environ.get("HMI_CONFIRM_TOPIC", "hmi/confirm")

# ---- Scheduler behaviour ------------------------------------------------------
# When True (production), a robot is only allocated if every one of its
# subsystems has sent a recent heartbeat saying it is online and ready.
REQUIRE_HEARTBEAT = _bool("REQUIRE_HEARTBEAT", True)

# ---- Reasoning agent (Ollama) -------------------------------------------------
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "localhost")
OLLAMA_PORT = int(os.environ.get("OLLAMA_PORT", "11434"))
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3:8b")
OLLAMA_TIMEOUT_S = int(os.environ.get("OLLAMA_TIMEOUT_S", "60"))
# When True, a parsed instruction is NOT sent to the scheduler until an
# operator confirms it on HMI_CONFIRM_TOPIC. Recommended while commissioning.
REQUIRE_CONFIRMATION = _bool("REQUIRE_CONFIRMATION", False)
