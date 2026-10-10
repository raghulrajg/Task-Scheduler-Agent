"""Capture-analysis tools."""
import json
import os
import subprocess
import sys

from tools.api_watch import diff, flatten
from tools.har_summary import redact_text, summarize

SECRET_PW, SECRET_TOKEN = "Sup3rSecret!", "eyJhbGciOiJIUzI1NiJ9.abc.def"


def entry(method, url, req=None, resp=None, status=200, rtype="xhr", headers=(), t="2026-10-09T10:00:00.000Z"):
    e = {"startedDateTime": t, "_resourceType": rtype,
         "request": {"method": method, "url": url, "headers": [{"name": k, "value": v} for k, v in headers]},
         "response": {"status": status, "content": {"mimeType": "application/json", "text": resp or ""}}}
    if req is not None:
        e["request"]["postData"] = {"mimeType": "application/json", "text": req}
    return e


def har():
    auth = [("Authorization", f"Bearer {SECRET_TOKEN}")]
    entries = [
        entry("GET", "http://192.168.10.10/static/app.js", rtype="script"),
        entry("POST", "http://192.168.10.10/api/login", json.dumps({"username": "admin", "password": SECRET_PW}),
              json.dumps({"code": 0, "data": {"token": SECRET_TOKEN}})),
    ]
    for i in range(5):
        entries.append(entry("GET", "http://192.168.10.10/api/robot/status", None,
                             json.dumps({"data": {"nav": {"state": "IDLE"}, "battery": 0.9}}), headers=auth))
    entries.append(entry("POST", "http://192.168.10.10/api/nav/goto", json.dumps({"point": "station_A"}),
                         json.dumps({"code": 0}), headers=auth, t="2026-10-09T10:00:05.123Z"))
    ws = entry("GET", "ws://192.168.10.10:9090/", rtype="websocket")
    ws["_webSocketMessages"] = [
        {"type": "send", "data": json.dumps({"op": "subscribe", "topic": "/battery_state"})},
        {"type": "receive", "data": json.dumps({"op": "publish", "topic": "/battery_state", "msg": {"percentage": 0.9}})},
    ]
    entries.append(ws)
    return {"log": {"entries": entries}}


def test_har_summary_finds_commands_status_and_rosbridge():
    text = summarize(har())
    assert "POST http://192.168.10.10/api/nav/goto" in text
    assert '{"point": "station_A"}' in text
    assert "GET http://192.168.10.10/api/robot/status    x5" in text
    assert "rosbridge" in text
    assert "app.js" not in text


def test_har_summary_redacts_secrets():
    text = summarize(har())
    assert SECRET_PW not in text and SECRET_TOKEN not in text
    assert '"password": "***"' in text and '"token": "***"' in text


def test_redact_text_forms_and_bearer():
    assert "hunter2" not in redact_text("user=bob&password=hunter2")
    assert SECRET_TOKEN not in redact_text(f"Authorization: Bearer {SECRET_TOKEN}")


def test_har_summary_cli(tmp_path):
    path = tmp_path / "c.har"
    path.write_text(json.dumps(har()))
    out = tmp_path / "s.txt"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([sys.executable, os.path.join(root, "tools", "har_summary.py"), str(path), "-o", str(out)],
                   check=True, capture_output=True)
    assert "api/nav/goto" in out.read_text()


def test_api_watch_flatten_and_diff():
    a = flatten({"data": {"nav": {"state": "IDLE"}, "pts": [1, 2]}})
    b = flatten({"data": {"nav": {"state": "RUNNING"}, "pts": [1, 2]}})
    assert a == {"data.nav.state": "IDLE", "data.pts.0": 1, "data.pts.1": 2}
    assert list(diff(a, b)) == [("data.nav.state", "IDLE", "RUNNING")]


def test_offline_demo_runs_without_broker():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "MQTT_BROKER_HOST": "203.0.113.1"}   # unreachable on purpose
    out = subprocess.run([sys.executable, os.path.join(root, "offline_demo.py"), "--jobs", "5",
                          "--speed", "20", "--qc-pass-rate", "1", "--seed", "1"],
                         capture_output=True, text=True, timeout=60, cwd=root, env=env)
    assert out.returncode == 0, out.stderr
    assert out.stdout.count("DONE -> good part to package") == 5
    assert out.stdout.count("waiting for a free robot") == 1
