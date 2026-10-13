"""Shared fixtures. No broker or hardware needed except where a test says so."""
import os
import sys

# Never pick up a developer's or robot PC's real .env during tests (e.g. its
# REQUIRE_CONFIRMATION=1 would hold back every job the tests submit).
os.environ["ENV_FILE"] = os.devnull
os.environ.pop("REQUIRE_CONFIRMATION", None)
for k in ("MQTT_USERNAME", "MQTT_PASSWORD"):
    os.environ[k] = ""
os.environ.setdefault("MQTT_TLS", "0")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import pytest  # noqa: E402

from robot_registry import RobotRegistry  # noqa: E402
from validation import SiteConfig  # noqa: E402


class FakeMQTT:
    """Records what the scheduler publishes."""

    def __init__(self):
        self.tasks = []      # (robot_id, subsystem, message)
        self.cancels = []    # (robot_id, subsystem, subtask_id, attempt, reason)
        self.events = []

    def publish_task(self, robot_id, subsystem, message):
        self.tasks.append((robot_id, subsystem, message))

    def publish_cancel(self, robot_id, subsystem, subtask_id, attempt, reason):
        self.cancels.append((robot_id, subsystem, subtask_id, attempt, reason))

    def publish_event(self, event):
        self.events.append(event)

    def event_names(self):
        return [e["event"] for e in self.events]


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


INSTRUCTION = {"product": "ABCD", "source": "manufacturing_1", "quality_check": "quality_station",
               "success_dest": "package_area", "fail_dest": "waste_area"}


@pytest.fixture
def site():
    """site.json with the QC location as a plain camera station: the part is
    inspected while the robot holds it (no conveyor load/unload steps)."""
    s = SiteConfig.load("site.json")
    for meta in s.locations.values():
        meta.pop("conveyor", None)
    return s


@pytest.fixture
def conveyor_site():
    """site.json exactly as shipped: quality_station is a conveyor."""
    s = SiteConfig.load("site.json")
    assert s.conveyor_for("quality_station") is not None
    return s


@pytest.fixture
def clock():
    return Clock()


def healthy_registry(clock, battery=90.0):
    reg = RobotRegistry("robots_metadata.json", require_heartbeat=True,
                        heartbeat_timeout_s=15, min_battery_percent=25, clock=clock)
    for r in reg.all():
        for topic in r.topics():
            reg.update_health(r.id, topic, {"online": True, "ready": True, "battery": battery}, now=clock())
    return reg


@pytest.fixture
def registry(clock):
    return healthy_registry(clock)


def beat(reg, clock, overrides=None):
    """Refresh every heartbeat at the current fake time."""
    for r in reg.all():
        for topic in r.topics():
            payload = {"online": True, "ready": True, "battery": 90.0}
            payload.update((overrides or {}).get((r.id, topic), {}))
            reg.update_health(r.id, topic, payload, now=clock())
