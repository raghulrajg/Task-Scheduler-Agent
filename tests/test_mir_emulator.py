"""
Optional conformance test of drivers/mir.py against a spec-based MiR REST
emulator (https://github.com/JimothyJohn/amr-emulator), which enforces the
real auth scheme, mission-queue lifecycle and fault states.

    pip install amr-emulator   # or from its repo
    amr-emulator --port 18080 --mission-duration 2 &
    MIR_EMULATOR_URL=http://127.0.0.1:18080 pytest tests/test_mir_emulator.py

Skipped when MIR_EMULATOR_URL is not set.
"""
import json
import os
import threading
import urllib.parse
import urllib.request

import pytest

from drivers import create_driver

URL = os.environ.get("MIR_EMULATOR_URL")
pytestmark = pytest.mark.skipif(not URL, reason="MIR_EMULATOR_URL not set")


def _driver(**extra):
    u = urllib.parse.urlparse(URL)
    drv = create_driver({"type": "mir", "host": u.hostname, "port": u.port, "username": "distributor",
                         "password": "distributor", "poll_interval_s": 0.25, "ready_wait_s": 3,
                         "locations": {}})
    code, missions = drv._request("GET", "/missions")
    assert code == 200 and missions, f"emulator has no missions: {code} {missions}"
    drv._locations = {"manufacturing_1": {"mission_guid": missions[0]["guid"]}}
    for k, v in extra.items():
        setattr(drv, k, v)
    return drv


def _faults(drv, faults):
    """Emulator-only fault injection: PUT /_emulator/faults {"faults": [...]}; [] clears."""
    method, body = ("PUT", json.dumps({"faults": faults}).encode()) if faults else ("DELETE", None)
    req = urllib.request.Request(f"{URL}/_emulator/faults", method=method, data=body,
                                 headers={"Content-Type": "application/json", "Authorization": drv._auth})
    urllib.request.urlopen(req, timeout=5).read()


@pytest.fixture(autouse=True)
def _clear_faults():
    yield
    _faults(_driver(), [])


def test_state_and_auth():
    st = _driver().get_state()
    assert st["ready"] is True and isinstance(st["battery"], float)


def test_navigate_runs_mission_to_done():
    drv = _driver()
    res = drv.execute({"subtask_id": "emu-1", "action": "navigate", "zone": "manufacturing_1"},
                      threading.Event())
    assert res["state"] == "done", res


def test_cancel_aborts_mission():
    drv = _driver()
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    res = drv.execute({"subtask_id": "emu-2", "action": "navigate", "zone": "manufacturing_1"}, cancel)
    assert res["state"] == "cancelled"
    code, queue = drv._request("GET", "/mission_queue")
    assert code == 200
    assert not any(e.get("state") == "Executing" for e in queue)


def test_emergency_stop_reported_and_refused():
    drv = _driver()
    _faults(drv, ["emergency_stop"])
    st = drv.get_state()
    assert st["estop"] is True and st["ready"] is False
    code, before = drv._request("GET", "/mission_queue")
    res = drv.execute({"subtask_id": "emu-3", "action": "navigate", "zone": "manufacturing_1"},
                      threading.Event())
    assert res["state"] == "failed" and "EmergencyStop" in res["error"]
    code, after = drv._request("GET", "/mission_queue")
    assert len(after) == len(before), "must not queue a mission during an e-stop"


def test_mission_failure_mid_drive_is_reported():
    drv = _driver()
    threading.Timer(0.8, lambda: _faults(drv, ["mission_failure"])).start()
    res = drv.execute({"subtask_id": "emu-4", "action": "navigate", "zone": "manufacturing_1"},
                      threading.Event())
    assert res["state"] == "failed", res
    assert "aborted" in res["error"] or "Error" in res["error"], res


def test_estop_mid_drive_fails_fast():
    drv = _driver()
    threading.Timer(0.8, lambda: _faults(drv, ["emergency_stop"])).start()
    res = drv.execute({"subtask_id": "emu-5", "action": "navigate", "zone": "manufacturing_1"},
                      threading.Event())
    assert res["state"] == "failed" and "EmergencyStop" in res["error"], res
