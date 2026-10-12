"""Driver tests against local fakes of the vendor interfaces (no hardware)."""
import base64
import hashlib
import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from drivers import create_driver
from fake_ur import FakeUR


# ============================================================== UR
@pytest.fixture
def fake_ur():
    ur = FakeUR()
    yield ur
    ur.close()


def ur_driver(ur, **extra):
    cfg = {"type": "ur", "host": "127.0.0.1", "dashboard_port": ur.dash_port, "modbus_port": ur.modbus_port,
           "programs": {"pick": {"manufacturing_1": "pick_mfg.urp"}, "place": {"package_area": "place_pkg.urp"}},
           "result_register": 128, "poll_interval_s": 0.05, "program_timeout_s": 5}
    cfg.update(extra)
    return create_driver(cfg)


def run(drv, action="pick", zone="manufacturing_1", cancel=None):
    return drv.execute({"subtask_id": "s1", "action": action, "zone": zone}, cancel or threading.Event())


def test_ur_success(fake_ur):
    res = run(ur_driver(fake_ur))
    assert res["state"] == "done" and res["result"]["verified"]
    assert "load pick_mfg.urp" in fake_ur.commands and "play" in fake_ur.commands


def test_ur_program_reports_failure(fake_ur):
    fake_ur.outcome = 2
    res = run(ur_driver(fake_ur))
    assert res["state"] == "failed" and "reported failure" in res["error"]


def test_ur_program_ends_without_result(fake_ur):
    fake_ur.outcome = 0
    assert run(ur_driver(fake_ur))["state"] == "failed"


def test_ur_protective_stop_during_program(fake_ur):
    fake_ur.safety_on_finish = "PROTECTIVE_STOP"
    res = run(ur_driver(fake_ur))
    assert res["state"] == "failed" and "PROTECTIVE_STOP" in res["error"]


@pytest.mark.parametrize("attr,value,needle", [
    ("remote", False, "Remote Control"),
    ("robotmode", "POWER_OFF", "POWER_OFF"),
    ("safety", "ROBOT_EMERGENCY_STOP", "ROBOT_EMERGENCY_STOP"),
    ("program_state", "PLAYING", "another program"),
])
def test_ur_preflight_refuses(fake_ur, attr, value, needle):
    setattr(fake_ur, attr, value)
    res = run(ur_driver(fake_ur))
    assert res["state"] == "failed" and needle in res["error"]
    assert "play" not in fake_ur.commands


def test_ur_unknown_zone_and_missing_program(fake_ur):
    assert run(ur_driver(fake_ur), zone="nowhere")["state"] == "failed"
    drv = ur_driver(fake_ur, programs={"pick": {"manufacturing_1": "missing.urp"}})
    assert "File not found" in run(drv)["error"]


def test_ur_protective_stop_pauses_program_fails_fast(fake_ur):
    fake_ur.run_time = 30
    drv = ur_driver(fake_ur, program_timeout_s=20)

    def trip():
        fake_ur.program_state = "PAUSED"
        fake_ur.safety = "PROTECTIVE_STOP"
    threading.Timer(0.3, trip).start()
    t0 = time.time()
    res = run(drv)
    assert res["state"] == "failed" and "PROTECTIVE_STOP" in res["error"]
    assert time.time() - t0 < 3


def test_ur_cancel_stops_program(fake_ur):
    fake_ur.run_time = 10
    drv = ur_driver(fake_ur)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    res = run(drv, cancel=cancel)
    assert res["state"] == "cancelled" and "stop" in fake_ur.commands


def test_ur_timeout(fake_ur):
    fake_ur.run_time = 10
    res = run(ur_driver(fake_ur, program_timeout_s=0.4))
    assert res["state"] == "failed" and "did not finish" in res["error"]
    assert "stop" in fake_ur.commands


def test_ur_state(fake_ur):
    drv = ur_driver(fake_ur)
    assert drv.get_state()["ready"]
    fake_ur.safety = "SYSTEM_EMERGENCY_STOP"
    st = drv.get_state()
    assert not st["ready"] and st["estop"]


def test_ur_unreachable():
    drv = create_driver({"type": "ur", "host": "127.0.0.1", "dashboard_port": 1, "modbus_port": 1,
                         "programs": {"pick": {"z": "p.urp"}}, "request_timeout_s": 0.5})
    assert drv.get_state()["ready"] is False
    assert run(drv, zone="z")["state"] == "failed"


# ============================================================== MiR (mock)
class FakeMiR:
    """Mock of the MiR REST API v2.0.0 subset the driver uses."""

    def __init__(self, user="distributor", password="distributor", mission_s=0.4):
        self.expected_auth = "Basic " + base64.b64encode(
            f"{user}:{hashlib.sha256(password.encode()).hexdigest()}".encode()).decode()
        self.state_id, self.state_text = 3, "Ready"
        self.battery = 88.0
        self.missions = {"guid-mfg": "Go to manufacturing_1"}
        self.queue = {}
        self.mission_s = mission_s
        self.final_state = "Done"
        self.deleted = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=None):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _auth_ok(self):
                if self.headers.get("Authorization") != fake.expected_auth:
                    self._send(401, {"error_human": "unauthorized"})
                    return False
                return True

            def do_GET(self):
                if not self._auth_ok():
                    return
                path = self.path.replace("/api/v2.0.0", "")
                if path == "/status":
                    return self._send(200, {"state_id": fake.state_id, "state_text": fake.state_text,
                                            "battery_percentage": fake.battery, "errors": []})
                if path == "/missions":
                    return self._send(200, [{"guid": g, "name": n} for g, n in fake.missions.items()])
                if path.startswith("/mission_queue/"):
                    qid = int(path.rsplit("/", 1)[1])
                    entry = fake.queue.get(qid)
                    if not entry:
                        return self._send(404, {"error_human": "Not found"})
                    if entry["state"] == "Executing" and time.time() - entry["t"] > fake.mission_s:
                        entry["state"] = fake.final_state
                    return self._send(200, {"id": qid, "state": entry["state"], "message": ""})
                self._send(404)

            def do_POST(self):
                if not self._auth_ok():
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if body.get("mission_id") not in fake.missions:
                    return self._send(400, {"error_human": "mission_id does not match an existing mission"})
                qid = len(fake.queue) + 1
                fake.queue[qid] = {"state": "Executing", "t": time.time()}
                self._send(201, {"id": qid, "state": "Pending"})

            def do_DELETE(self):
                if not self._auth_ok():
                    return
                qid = int(self.path.rsplit("/", 1)[1])
                fake.deleted.append(qid)
                if qid in fake.queue:
                    fake.queue[qid]["state"] = "Aborted"
                self._send(204)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake_mir():
    m = FakeMiR()
    yield m
    m.close()


def mir_driver(m, **extra):
    cfg = {"type": "mir", "host": "127.0.0.1", "port": m.port, "username": "distributor",
           "password": "distributor", "poll_interval_s": 0.05, "ready_wait_s": 0.3,
           "locations": {"manufacturing_1": {"mission_guid": "guid-mfg"},
                         "bogus": {"mission_guid": "guid-unknown"}}}
    cfg.update(extra)
    return create_driver(cfg)


def test_mir_navigate_success_with_correct_auth(fake_mir):
    res = run(mir_driver(fake_mir), action="navigate")
    assert res["state"] == "done"


def test_mir_wrong_password(fake_mir):
    res = run(mir_driver(fake_mir, password="wrong"), action="navigate")
    assert res["state"] == "failed"


def test_mir_mission_aborted(fake_mir):
    fake_mir.final_state = "Aborted"
    res = run(mir_driver(fake_mir), action="navigate")
    assert res["state"] == "failed" and "aborted" in res["error"]


@pytest.mark.parametrize("state_id,text", [(10, "EmergencyStop"), (12, "Error"), (4, "Pause")])
def test_mir_not_ready(fake_mir, state_id, text):
    fake_mir.state_id, fake_mir.state_text = state_id, text
    res = run(mir_driver(fake_mir), action="navigate")
    assert res["state"] == "failed" and text in res["error"]
    assert fake_mir.queue == {}, "must not queue a mission on a robot that isn't Ready"


def test_mir_unknown_zone_and_unknown_mission(fake_mir):
    assert run(mir_driver(fake_mir), action="navigate", zone="nowhere")["state"] == "failed"
    res = run(mir_driver(fake_mir), action="navigate", zone="bogus")
    assert res["state"] == "failed" and "rejected" in res["error"]


def test_mir_cancel_deletes_queue_entry(fake_mir):
    fake_mir.mission_s = 10
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    res = run(mir_driver(fake_mir), action="navigate", cancel=cancel)
    assert res["state"] == "cancelled" and fake_mir.deleted == [1]


def test_mir_estop_mid_mission_fails_fast_and_aborts(fake_mir):
    fake_mir.mission_s = 30

    def estop():
        fake_mir.state_id, fake_mir.state_text = 10, "EmergencyStop"
    threading.Timer(0.3, estop).start()
    t0 = time.time()
    res = run(mir_driver(fake_mir), action="navigate")
    assert res["state"] == "failed" and "EmergencyStop" in res["error"]
    assert fake_mir.deleted == [1] and time.time() - t0 < 3


def test_mir_state(fake_mir):
    drv = mir_driver(fake_mir)
    st = drv.get_state()
    assert st["ready"] and st["battery"] == 88.0
    fake_mir.state_id, fake_mir.state_text = 10, "EmergencyStop"
    st = drv.get_state()
    assert not st["ready"] and st["estop"]


# ============================================================== config
def test_env_substitution(monkeypatch):
    monkeypatch.setenv("SECRET_PW", "s3cret")
    drv = create_driver({"type": "mir", "host": "h", "username": "u", "password": "${SECRET_PW}"})
    expected = base64.b64encode(f"u:{hashlib.sha256(b's3cret').hexdigest()}".encode()).decode()
    assert drv._auth == f"Basic {expected}"


def test_env_substitution_missing(monkeypatch):
    monkeypatch.delenv("NOPE_NOT_SET", raising=False)
    with pytest.raises(KeyError):
        create_driver({"type": "mir", "host": "h", "username": "u", "password": "${NOPE_NOT_SET}"})
