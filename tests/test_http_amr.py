"""drivers/http_amr.py against a fake web-dashboard robot."""
import threading
import time

import pytest

from drivers import create_driver
from drivers.util import dig, matches, render
from fake_amr import FakeAMR


@pytest.fixture
def amr():
    a = FakeAMR()
    yield a
    a.close()


def driver(fake, monkeypatch, **extra):
    monkeypatch.setenv("TEST_AMR_PW", "pw{1}")      # braces must survive (no templating of login)
    cfg = {
        "type": "http_amr", "base_url": fake.url,
        "login": {"method": "POST", "path": "/api/login",
                  "json": {"username": "admin", "password": "${TEST_AMR_PW}"}, "token_path": "data.token"},
        "locations": {"manufacturing_1": {"id": "station_A"}, "quality_station": {"id": "station_B"},
                      "ghost": {"id": "station_Z"}},
        "navigate": {"method": "POST", "path": "/api/nav/goto", "json": {"point": "{id}"},
                     "ok_path": "code", "ok_values": [0]},
        "cancel": {"method": "POST", "path": "/api/nav/cancel"},
        "status": {"method": "GET", "path": "/api/robot/status"},
        "fields": {"nav_state": "data.nav.state", "running_values": ["RUNNING"],
                   "arrived_values": ["SUCCEEDED"], "failed_values": ["FAILED", "CANCELED"],
                   "target": "data.nav.target", "battery": "data.battery", "battery_scale": 100,
                   "estop": "data.emergency", "estop_values": [True], "error": "data.error_msg"},
        "poll_interval_s": 0.05, "settle_s": 1.0, "start_timeout_s": 2, "mission_timeout_s": 10,
    }
    cfg.update(extra)
    return create_driver(cfg)


def nav(drv, zone="manufacturing_1", cancel=None):
    return drv.execute({"subtask_id": "s1", "action": "navigate", "zone": zone}, cancel or threading.Event())


def test_navigate_success_ignores_stale_success(amr, monkeypatch):
    amr.pickup_delay = 0.4                      # stale "SUCCEEDED" visible for 0.4 s after the goal
    t0 = time.time()
    res = nav(driver(amr, monkeypatch))
    assert res["state"] == "done"
    assert time.time() - t0 >= amr.pickup_delay + amr.drive_time - 0.1, "returned on the stale status"
    assert amr.nav_posts()[0][2] == {"point": "station_A"}


def test_already_at_target(amr, monkeypatch):
    res = nav(driver(amr, monkeypatch), zone="quality_station")    # fake starts at station_B
    assert res["state"] == "done"


def test_navigation_failure_reports_robot_error(amr, monkeypatch):
    amr.final_state = "FAILED"
    res = nav(driver(amr, monkeypatch))
    assert res["state"] == "failed" and "path blocked" in res["error"]


def test_rejected_goal(amr, monkeypatch):
    res = nav(driver(amr, monkeypatch), zone="ghost")
    assert res["state"] == "failed" and "no such point" in res["error"]


def test_unmapped_zone(amr, monkeypatch):
    assert nav(driver(amr, monkeypatch), zone="mars")["state"] == "failed"
    assert amr.nav_posts() == []


def test_estop_before_and_during(amr, monkeypatch):
    drv = driver(amr, monkeypatch)
    amr.emergency = True
    res = nav(drv)
    assert res["state"] == "failed" and "emergency" in res["error"] and amr.nav_posts() == []
    amr.emergency = False
    amr.drive_time = 5
    threading.Timer(0.6, lambda: setattr(amr, "emergency", True)).start()
    res = nav(drv)
    assert res["state"] == "failed" and "emergency" in res["error"]
    assert any(r[1] == "/api/nav/cancel" for r in amr.requests)


def test_never_starts_moving(amr, monkeypatch):
    amr.never_start = True
    res = nav(driver(amr, monkeypatch))
    assert res["state"] == "failed" and "did not start moving" in res["error"]
    assert any(r[1] == "/api/nav/cancel" for r in amr.requests)


def test_cancel(amr, monkeypatch):
    amr.drive_time = 5
    cancel = threading.Event()
    threading.Timer(0.6, cancel.set).start()
    res = nav(driver(amr, monkeypatch), cancel=cancel)
    assert res["state"] == "cancelled" and any(r[1] == "/api/nav/cancel" for r in amr.requests)


def test_refuses_when_robot_already_moving(amr, monkeypatch):
    amr.state = "RUNNING"
    res = nav(driver(amr, monkeypatch))
    assert res["state"] == "failed" and "already executing" in res["error"]


def test_token_expiry_relogin(amr, monkeypatch):
    drv = driver(amr, monkeypatch)
    assert drv.get_state()["ready"]
    amr.token = "tok-2"                         # server-side session expired
    assert nav(drv)["state"] == "done"
    assert sum(1 for r in amr.requests if r[1] == "/api/login") == 2


def test_state_battery_scaling_and_estop(amr, monkeypatch):
    drv = driver(amr, monkeypatch)
    st = drv.get_state()
    assert st["battery"] == 87.0 and st["ready"] and not st["estop"]
    amr.emergency = True
    st = drv.get_state()
    assert st["estop"] and not st["ready"]


def test_unreachable(monkeypatch):
    drv = create_driver({"type": "http_amr", "base_url": "http://127.0.0.1:1", "request_timeout_s": 0.3,
                         "navigate": {"path": "/x"}, "status": {"path": "/s"}, "fields": {"nav_state": "s"},
                         "locations": {"z": {"id": 1}}})
    assert drv.get_state()["ready"] is False
    assert nav(drv, zone="z")["state"] == "failed"


# ---- helpers
def test_util_helpers():
    assert dig({"a": [{"b": 2}]}, "a.0.b") == 2 and dig({"a": 1}, "a.x") is None
    assert matches(1, [1]) and not matches(True, [1]) and not matches("1", [1])
    assert render({"p": "{id}", "q": "to-{id}", "n": ["{n}"]}, {"id": "A", "n": 3}) == {"p": "A", "q": "to-A", "n": [3]}
    with pytest.raises(KeyError):
        render("{typo}", {"id": 1})
