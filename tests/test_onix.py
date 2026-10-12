"""drivers/onix.py against the ONIX simulator (same API as the robot's web app)."""
import math
import threading
import time

import pytest

import stations as st
from drivers import create_driver
from tools.onix_simulator import OnixSimulator

TARGET = {"x": 1.0, "y": 0.5, "theta": math.radians(90)}


@pytest.fixture
def sim():
    s = OnixSimulator(map_body={"header": {"mapName": "t"}, "advancedPointList": [
        {"instanceName": "LM1", "className": "LocationMark", "pos": {"x": 1.0, "y": 0.5}, "dir": math.radians(90)}]},
        speed=3.0, turn_speed=4.0, status_hz=20)
    yield s
    s.close()


@pytest.fixture
def stations_file(tmp_path):
    d = st.empty()
    d["stations"]["LM1"] = {"class": "LocationMark", **TARGET}
    d["locations"]["manufacturing_1"] = {"station": "LM1", **TARGET}
    d["locations"]["package_area"] = {"station": None, "x": None, "y": None, "theta": None}
    path = str(tmp_path / "stations.json")
    st.save(path, d)
    return path


def driver(sim, path, **extra):
    cfg = {"type": "onix", "host": "127.0.0.1", "ports": sim.ports, "stations_file": path,
           "settle_s": 0.2, "start_timeout_s": 1.0, "stall_timeout_s": 1.0, "mission_timeout_s": 10,
           "error_grace_s": 0.3}
    cfg.update(extra)
    return create_driver(cfg)


def nav(drv, zone="manufacturing_1", cancel=None):
    return drv.execute({"subtask_id": "s1", "action": "navigate", "zone": zone}, cancel or threading.Event())


def sent(sim, api):
    return [r for a, r in sim.requests if a == api]


def test_drives_to_coordinates_and_verifies_arrival(sim, stations_file):
    drv = driver(sim, stations_file)
    res = nav(drv)
    assert res["state"] == "done" and res["result"]["verified"]
    assert res["result"]["error_m"] < 0.01
    goal = sent(sim, "4002")[0]["free_nav"]
    assert goal == {"linear_x": 1.0, "linear_y": 0.5, "angular_z": pytest.approx(math.radians(90))}
    state = drv.get_state()
    assert state["at_location"] == "manufacturing_1" and state["ready"]
    assert state["locations"] == ["manufacturing_1"]


def test_station_mode_uses_path_navigation(sim, stations_file):
    res = nav(driver(sim, stations_file, nav_mode="station"))
    assert res["state"] == "done"
    assert sent(sim, "4003")[0]["path_nav"]["target_node"] == "LM1" and not sent(sim, "4002")


def test_coordinates_reread_for_every_move(sim, stations_file):
    drv = driver(sim, stations_file)
    d = st.load(stations_file)
    d["locations"]["manufacturing_1"].update(x=-0.5, y=0.2, theta=0.0, station=None)
    st.save(stations_file, d)
    assert nav(drv)["state"] == "done"
    assert sent(sim, "4002")[-1]["free_nav"]["linear_x"] == -0.5


def test_small_positioning_error_within_tolerance_is_accepted(sim, stations_file):
    sim.stop_offset_m = 0.05
    res = nav(driver(sim, stations_file))
    assert res["state"] == "done" and 0.04 < res["result"]["error_m"] < 0.06


def test_stopping_short_of_the_target_fails_and_cancels(sim, stations_file):
    sim.stop_offset_m = 0.3
    res = nav(driver(sim, stations_file))
    assert res["state"] == "failed" and "stopped 0.30 m" in res["error"]
    assert sent(sim, "4004"), "goal must be cancelled"


def test_wrong_final_heading_fails(sim, stations_file):
    sim.final_yaw_error = math.radians(20)
    res = nav(driver(sim, stations_file))
    assert res["state"] == "failed" and "20.0 deg" in res["error"]


def test_blocked_on_the_way(sim, stations_file):
    sim.block_after_m = 0.4
    res = nav(driver(sim, stations_file))
    assert res["state"] == "failed" and "stopped" in res["error"]


def test_goal_rejected(sim, stations_file):
    sim.reject_goals = True
    res = nav(driver(sim, stations_file))
    assert res["state"] == "failed" and "rejected the goal" in res["error"]


def test_goal_ignored(sim, stations_file):
    sim.ignore_goals = True
    res = nav(driver(sim, stations_file))
    assert res["state"] == "failed" and "did not start moving" in res["error"]


def test_estop_before_start_refuses(sim, stations_file):
    sim.emergency = 1
    res = nav(driver(sim, stations_file))
    assert res["state"] == "failed" and "emergency" in res["error"] and not sent(sim, "4002")
    assert driver(sim, stations_file).get_state()["estop"] is True


def test_estop_during_drive(sim, stations_file):
    sim.speed = 0.5
    threading.Timer(0.5, lambda: setattr(sim, "emergency", 1)).start()
    res = nav(driver(sim, stations_file))
    assert res["state"] == "failed" and "emergency" in res["error"] and sent(sim, "4004")


def test_not_localized_refuses_and_lost_localization_fails(sim, stations_file):
    sim.confidence = 40
    res = nav(driver(sim, stations_file))
    assert res["state"] == "failed" and "not localized" in res["error"]
    sim.confidence = 95
    sim.speed = 0.5
    threading.Timer(0.5, lambda: setattr(sim, "confidence", 30)).start()
    res = nav(driver(sim, stations_file))
    assert res["state"] == "failed" and "localization lost" in res["error"]


def test_persistent_robot_error_fails(sim, stations_file):
    sim.speed = 0.3
    sim.errors = ["motor overcurrent"]
    threading.Timer(0.3, lambda: setattr(sim, "error_id", 2)).start()
    res = nav(driver(sim, stations_file))
    assert res["state"] == "failed" and "motor overcurrent" in res["error"]


def test_cancel_stops_the_robot(sim, stations_file):
    sim.speed = 0.3
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    res = nav(driver(sim, stations_file), cancel=cancel)
    assert res["state"] == "cancelled" and sent(sim, "4004")


def test_mission_timeout(sim, stations_file):
    sim.speed = 0.05
    res = nav(driver(sim, stations_file, mission_timeout_s=1.0, stall_timeout_s=5))
    assert res["state"] == "failed" and "within 1s" in res["error"]


def test_already_moving_refuses(sim, stations_file):
    sim.speed = 0.2
    sim.set_goal(-3, 0, 0)
    time.sleep(0.2)
    res = nav(driver(sim, stations_file))
    assert res["state"] == "failed" and "already moving" in res["error"]


def test_unassigned_unknown_and_missing_locations(sim, stations_file, tmp_path):
    drv = driver(sim, stations_file)
    res = nav(drv, zone="package_area")
    assert res["state"] == "failed" and "amr_stations.py assign package_area" in res["error"]
    assert "not in the stations file" in nav(drv, zone="mars")["error"]
    assert "not found" in nav(driver(sim, str(tmp_path / "nope.json")))["error"]
    assert not sim.requests


def test_wrong_api_key(sim, stations_file):
    res = nav(driver(sim, stations_file, api_key="wrong"))
    assert res["state"] == "failed"


def test_unreachable_robot(stations_file):
    drv = create_driver({"type": "onix", "host": "127.0.0.1", "ports": {"rest": 1, "control": 1, "status": 1},
                         "stations_file": stations_file, "request_timeout_s": 0.5})
    assert drv.get_state()["ready"] is False
    assert nav(drv)["state"] == "failed"
