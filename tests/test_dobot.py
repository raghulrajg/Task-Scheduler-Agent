"""drivers/dobot.py against a fake controller that speaks the documented reply format."""
import threading
import time

import pytest

from drivers import create_driver
from fake_dobot import FakeDobot


@pytest.fixture
def arm():
    a = FakeDobot()
    yield a
    a.close()


def driver(fake, **extra):
    cfg = {"type": "dobot", "host": "127.0.0.1", "port": fake.port, "protocol": fake.protocol,
           "projects": {"pick": {"manufacturing_1": "pick_mfg"}, "place": {"package_area": "place_pkg"}},
           "poll_interval_s": 0.05, "project_timeout_s": 5}
    cfg.update(extra)
    return create_driver(cfg)


def run(drv, action="pick", zone="manufacturing_1", cancel=None):
    return drv.execute({"subtask_id": "s1", "action": action, "zone": zone}, cancel or threading.Event())


def test_success_with_result_register(arm):
    res = run(driver(arm))
    assert res["state"] == "done" and res["result"]["verified"]
    assert arm.control_requested
    assert "SetOutputInt(0,0)" in arm.commands and 'RunScript("pick_mfg")' in arm.commands


def test_project_reports_failure(arm):
    arm.outcome = 2
    res = run(driver(arm))
    assert res["state"] == "failed" and "reported failure" in res["error"]


def test_project_ends_without_result(arm):
    arm.outcome = 0
    assert run(driver(arm))["state"] == "failed"


@pytest.mark.parametrize("mode,needle", [(9, "ERROR"), (11, "COLLISION")])
def test_alarm_during_project(arm, mode, needle):
    arm.mode_on_finish = mode
    arm.alarms = "[22]"
    res = run(driver(arm))
    assert res["state"] == "failed" and needle in res["error"] and "[22]" in res["error"]
    assert "Stop()" in arm.commands


def test_disabled_during_project_is_failure(arm):
    arm.mode_on_finish = 4
    res = run(driver(arm))
    assert res["state"] == "failed" and "e-stop" in res["error"]


@pytest.mark.parametrize("mode,needle", [(4, "DISABLED"), (9, "ERROR"), (7, "another project"), (6, "BACKDRIVE")])
def test_preflight_refuses(arm, mode, needle):
    arm.mode = mode
    res = run(driver(arm))
    assert res["state"] == "failed" and needle in res["error"]
    assert not any(c.lower().startswith("runscript") for c in arm.commands)


def test_auto_enable(arm):
    arm.mode = 4
    res = run(driver(arm, auto_enable=True))
    assert res["state"] == "done"
    assert any(c.startswith("EnableRobot") for c in arm.commands)


def test_cancel_stops_project(arm):
    arm.run_time = 10
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    t0 = time.time()
    res = run(driver(arm), cancel=cancel)
    assert res["state"] == "cancelled" and "Stop()" in arm.commands and time.time() - t0 < 2


def test_timeout_stops_project(arm):
    arm.run_time = 10
    res = run(driver(arm, project_timeout_s=0.4))
    assert res["state"] == "failed" and "did not finish" in res["error"] and "Stop()" in arm.commands


def test_unknown_project_and_zone(arm):
    assert run(driver(arm), zone="nowhere")["state"] == "failed"
    res = run(driver(arm, projects={"pick": {"manufacturing_1": "missing"}}))
    assert res["state"] == "failed" and "rejected" in res["error"]


def test_v3_protocol():
    arm = FakeDobot(protocol="v3")
    try:
        drv = driver(arm, result={"kind": "none"})
        res = run(drv)
        assert res["state"] == "done" and res["result"]["verified"] is False
        assert "RunScript(pick_mfg)" in arm.commands
        arm.run_time = 10
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        assert run(drv, cancel=cancel)["state"] == "cancelled"
        assert "StopScript()" in arm.commands
    finally:
        arm.close()


def test_state_and_result_channel_check(arm):
    drv = driver(arm)
    st = drv.get_state()
    assert st["ready"] and "ENABLE" in st["detail"]
    assert drv.check_result_channel() == "ok"
    arm.mode, arm.alarms = 9, "[105]"
    st = drv.get_state()
    assert not st["ready"] and "[105]" in st["detail"]


def test_result_channel_unusable_on_v3_reported():
    arm = FakeDobot(protocol="v3")
    try:
        drv = driver(arm, result={"kind": "output_int", "address": 0})
        assert drv.check_result_channel().startswith("NOT usable")
    finally:
        arm.close()


def test_unreachable_controller():
    drv = create_driver({"type": "dobot", "host": "127.0.0.1", "port": 1, "request_timeout_s": 0.3,
                         "projects": {"pick": {"z": "p"}}})
    assert drv.get_state()["ready"] is False
    assert run(drv, zone="z")["state"] == "failed"


TRAVEL = [0, -30, 120, 0, 90, 0]


def test_travel_pose_required_after_project(arm):
    drv = driver(arm, travel_joints=TRAVEL)
    assert run(drv)["state"] == "done"
    st = drv.get_state()
    assert st["travel_pose"] is True and st["ready"]
    arm.joints = [0, -30, 120, 0, 45, 0]           # project left the wrist turned
    res = run(drv)
    assert res["state"] == "failed" and "travel pose" in res["error"]
    st = drv.get_state()
    assert st["travel_pose"] is False and not st["ready"]


def test_travel_joints_placeholder_rejected(arm):
    with pytest.raises(ValueError, match="6 joint angles"):
        driver(arm, travel_joints="REQUIRED: fill me in")
