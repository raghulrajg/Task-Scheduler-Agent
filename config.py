"""
config.py
---------
Deployment settings, read from environment variables (or a local .env file).

Loading order: real environment variables > .env file (or the file named by
ENV_FILE) > the defaults below. RUN_MODE (real/test) is usually set by the
--test flag or launch.py rather than by hand.

There are deliberately NO credential defaults in this file. Broker username
and password must come from the environment / .env on each machine, and .env
must never be committed (see .gitignore and .env.example).
"""

import os

try:
    from dotenv import load_dotenv
    # ENV_FILE picks another settings file instead of .env (optional).
    _env_file = os.environ.get("ENV_FILE")
    if _env_file and not os.path.isabs(_env_file):
        _env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), _env_file)
    load_dotenv(_env_file or None)  # no-op if the file doesn't exist
except ImportError:
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---- Run mode -----------------------------------------------------------------
# real : drive the real robots.
# test : simulated robots on the SAME broker (your EMQX), fully isolated from
#        the real fleet: every topic lives under MQTT_TOPIC_PREFIX (default
#        "sim/"), every client id gets a "-test" suffix, and the scheduler keeps
#        a separate state file. Real agents never see test traffic and vice versa.
# Select with RUN_MODE=test, or the --test flag on any program, or
# `python3 launch.py test`.
RUN_MODE = os.environ.get("RUN_MODE", "real").strip().lower()
if RUN_MODE not in ("real", "test"):
    raise SystemExit(f"RUN_MODE must be 'real' or 'test', not {RUN_MODE!r}")
TEST_MODE = RUN_MODE == "test"
TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "sim/" if TEST_MODE else "")
if TEST_MODE and not TOPIC_PREFIX:
    raise SystemExit("test mode needs a non-empty MQTT_TOPIC_PREFIX (it keeps simulated robots off real topics)")
if TOPIC_PREFIX and not TOPIC_PREFIX.endswith("/"):
    TOPIC_PREFIX += "/"
CLIENT_ID_SUFFIX = os.environ.get("MQTT_CLIENT_ID_SUFFIX", "-test" if TEST_MODE else "")


def topic(name: str) -> str:
    """Full MQTT topic for a base name, inside this run mode's namespace."""
    return TOPIC_PREFIX + name


def describe_mode() -> str:
    if TEST_MODE:
        return (f"TEST mode: simulated robots, topics under '{TOPIC_PREFIX}', "
                f"client ids end with '{CLIENT_ID_SUFFIX}', state in {SCHEDULER_STATE_PATH}")
    return f"REAL mode: real robots, topics {'under ' + repr(TOPIC_PREFIX) if TOPIC_PREFIX else 'at the root'}"


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
SCHEDULER_STATE_PATH = os.environ.get(
    "SCHEDULER_STATE_PATH", "state/test_scheduler_state.json" if TEST_MODE else "state/scheduler_state.json")

# ---- Topics (base names; use topic(NAME) to get the full, namespaced topic) -----
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
