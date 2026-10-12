"""
Location coordinates end to end, without a broker:
  * the scheduler refuses jobs whose locations no base has coordinates for;
  * a carrier job only goes to a robot whose base covers all its locations;
  * the arm refuses to pick/place unless its base reports it is at the zone;
  * the ONIX driver's heartbeat reports the verified arrival location.
"""
import json
import math

import pytest

from conftest import INSTRUCTION, FakeMQTT
from drivers import create_driver
from robot_agent import RobotAgent
from scheduler import TaskScheduler
from test_robot_agent import SlowDriver, statuses, task, wait_for
from tools.onix_simulator import OnixSimulator
from validation import InstructionError

ALL = ["manufacturing_1", "package_area", "quality_station", "rest_position", "waste_area"]


def make(site, registry, clock):
    mqtt = FakeMQTT()
    return TaskScheduler(registry, mqtt, site, clock=clock), mqtt


def report(registry, clock, robot_id, locations, **extra):
    registry.update_health(robot_id, "amr", {"online": True, "ready": True, "battery": 90.0,
                                             "locations": locations, **extra}, now=clock())


# ---------------------------------------------------------------- registry / scheduler
def test_no_base_reporting_means_no_restriction(site, registry, clock):
    assert registry.reachable_locations() is None
    sched, mqtt = make(site, registry, clock)
    sched.submit_instruction(INSTRUCTION)
    assert mqtt.tasks


def test_job_refused_when_no_base_has_coordinates(site, registry, clock):
    report(registry, clock, "robot_01", ["manufacturing_1", "quality_station"])
    sched, mqtt = make(site, registry, clock)
    with pytest.raises(InstructionError, match=r"no mobile base has coordinates for \['package_area', "
                                               r"'waste_area'\].*amr_stations"):
        sched.submit_instruction(INSTRUCTION)
    assert mqtt.tasks == []


def test_carrier_goes_to_a_robot_whose_base_covers_every_location(site, registry, clock):
    report(registry, clock, "robot_01", ["manufacturing_1", "quality_station"])  # partial
    report(registry, clock, "robot_02", ALL)
    for rid in ("robot_03", "robot_04"):
        report(registry, clock, rid, [])
    sched, mqtt = make(site, registry, clock)
    sched.submit_instruction(INSTRUCTION)
    robot, sub, msg = mqtt.tasks[-1]
    assert (robot, sub, msg["action"]) == ("robot_02", "amr", "navigate")


def test_job_waits_until_the_location_is_taught(site, registry, clock):
    report(registry, clock, "robot_01", ALL)
    for rid in ("robot_02", "robot_03", "robot_04"):
        report(registry, clock, rid, [])
    registry.set_status("robot_01", "maintenance")           # the only capable robot is out
    sched, mqtt = make(site, registry, clock)
    sched.submit_instruction(INSTRUCTION)
    assert mqtt.tasks == [] and len(sched.pending()) == 1
    # robot_02's base gets the missing locations taught -> heartbeat change drains the queue
    sched.on_robot_heartbeat("robot_02", "amr", {"online": True, "ready": True, "battery": 90.0,
                                                 "locations": ALL})
    assert mqtt.tasks and mqtt.tasks[-1][0] == "robot_02"


def test_locations_survive_a_last_will_and_show_in_fleet_view(registry, clock):
    report(registry, clock, "robot_01", ALL, at_location="manufacturing_1")
    view = {r["id"]: r for r in registry.fleet_view()}["robot_01"]["subsystems"]["amr"]
    assert view["at_location"] == "manufacturing_1" and view["locations"] == ALL
    registry.update_health("robot_01", "amr", {"online": False}, now=clock())
    h = registry.get("robot_01").health["amr"]
    assert h.locations == ALL and h.at_location is None
    assert registry.reachable_locations() == set(ALL)


# ---------------------------------------------------------------- arm interlock
class ArmDriver(SlowDriver):
    actions = ("pick", "place")


def arm_agent():
    agent = RobotAgent("robot_01", "arm", ArmDriver(0.01), interlock_with=("amr",),
                       require_base_at_zone="amr", client_id="t_arm")
    pub = []
    agent._client.publish = lambda topic, payload, qos=0, retain=False: pub.append(
        (topic, json.loads(payload), retain))
    return agent, pub


def last_status(pub):
    return [p for t, p, _ in pub if t.endswith("/status")][-1]


def test_arm_refuses_unless_base_is_at_the_zone():
    agent, pub = arm_agent()
    agent._siblings["amr"] = {"online": True, "busy": False}                       # old driver: no location
    agent._handle_task(task("a", action="pick", zone="manufacturing_1"))
    assert "does not report its location" in last_status(pub)["error"]
    agent._siblings["amr"].update(at_location="quality_station", at_locations=["quality_station"])
    agent._handle_task(task("b", action="pick", zone="manufacturing_1"))
    assert "not at 'manufacturing_1' (it reports quality_station)" in last_status(pub)["error"]
    agent._siblings["amr"].update(at_location=None, at_locations=[])
    agent._handle_task(task("c", action="pick", zone="manufacturing_1"))
    assert "no known location" in last_status(pub)["error"]
    # two names on one spot: the base lists both
    agent._siblings["amr"].update(at_location="rest_position", at_locations=["rest_position", "manufacturing_1"])
    agent._handle_task(task("d", action="pick", zone="manufacturing_1"))
    assert wait_for(lambda: ("d", 1, "done") in statuses(pub))


def test_require_base_at_zone_must_be_interlocked():
    with pytest.raises(ValueError):
        RobotAgent("robot_01", "arm", ArmDriver(), interlock_with=(), require_base_at_zone="amr")


def test_base_heartbeat_carries_location_and_hides_it_while_driving():
    class Base(SlowDriver):
        def get_state(self):
            return {"ready": True, "detail": "", "estop": False, "battery": 80.0,
                    "at_location": "manufacturing_1", "at_locations": ["manufacturing_1"],
                    "locations": ALL, "pose": {"x": 1, "y": 2, "theta": 0}}
    agent = RobotAgent("robot_01", "amr", Base(0.3), client_id="t_base")
    pub = []
    agent._client.publish = lambda topic, payload, qos=0, retain=False: pub.append(
        (topic, json.loads(payload), retain))
    agent._publish_heartbeat()
    hb = [p for t, p, _ in pub if t.endswith("/state")][-1]
    assert hb["at_location"] == "manufacturing_1" and hb["locations"] == ALL and hb["pose"]["x"] == 1
    agent._handle_task(task("go", zone="quality_station"))
    busy = [p for t, p, _ in pub if t.endswith("/state") and p["busy"]][-1]
    assert busy["at_location"] is None and busy["at_locations"] == []
    assert wait_for(lambda: ("go", 1, "done") in statuses(pub))


# ---------------------------------------------------------------- ONIX heartbeat location
MAP = {"header": {"mapName": "t"}, "advancedPointList": [
    {"instanceName": "LM1", "className": "LocationMark", "pos": {"x": 1.0, "y": 0.0}, "dir": 0.0}]}


@pytest.fixture
def onix(tmp_path):
    sim = OnixSimulator(map_body=MAP, start=(0.0, 0.0, 0.0), status_hz=20, speed=2.0, turn_speed=3.0)
    f = tmp_path / "stations.json"
    f.write_text(json.dumps({"defaults": {"xy_tolerance_m": 0.1, "yaw_tolerance_deg": 5}, "stations": {},
                             "locations": {
                                 "manufacturing_1": {"station": "LM1", "x": 1.0, "y": 0.0, "theta": 0.0},
                                 "rest_position": {"station": "LM1", "x": 1.0, "y": 0.0, "theta": 0.0},
                                 "quality_station": {"x": -1.0, "y": 0.5, "theta": math.pi / 2},
                                 "waste_area": {"x": None, "y": None, "theta": None}}}))
    drv = create_driver({"type": "onix", "host": "127.0.0.1", "ports": sim.ports, "stations_file": str(f),
                         "settle_s": 0.3, "start_timeout_s": 5})
    yield sim, drv
    sim.close()


def test_onix_reports_verified_arrival_even_where_names_overlap(onix):
    import threading
    sim, drv = onix
    st = drv.get_state()
    assert st["at_location"] is None and sorted(st["locations"]) == ["manufacturing_1", "quality_station",
                                                                        "rest_position"]
    res = drv.execute({"subtask_id": "s", "action": "navigate", "zone": "rest_position"}, threading.Event())
    assert res["state"] == "done", res
    st = drv.get_state()
    assert st["at_location"] == "rest_position"                  # the zone we drove to comes first
    assert set(st["at_locations"]) == {"rest_position", "manufacturing_1"}
    res = drv.execute({"subtask_id": "s2", "action": "navigate", "zone": "quality_station"}, threading.Event())
    assert res["state"] == "done", res
    st = drv.get_state()
    assert st["at_location"] == "quality_station" and st["at_locations"] == ["quality_station"]
    sim.confidence = 10                                           # lost localization: position unknown
    assert wait_for(lambda: drv.get_state()["at_location"] is None, 3)
