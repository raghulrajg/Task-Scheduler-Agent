"""MQTT-only QC scanner: drivers/mqtt_inspection.py <-> tools/scanner_bridge.py over a real broker."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

import config
from drivers import create_driver
from mqtt_common import make_client
from mqtt_fixtures import broker, wait_for  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def start_bridge(broker, *args):
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "scanner_bridge.py"),
                             "--station", "qc_01", "--heartbeat", "1"] + list(args),
                            env=broker.env(), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc


def stop_bridge(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def make_driver(broker, **extra):
    cfg = {"type": "mqtt_inspection", "station": "qc_01", "timeout_s": 8, "health_timeout_s": 5,
           "_mqtt_host": broker.host, "_mqtt_port": broker.port}
    cfg.update(extra)
    drv = create_driver(cfg)
    assert wait_for(lambda: drv.connected, 5)
    return drv


def inspect(drv, attempt=1):
    return drv.execute({"subtask_id": "t-4", "attempt": attempt, "action": "quality_check",
                        "zone": "quality_station", "target": "ABCD"}, threading.Event())


@pytest.mark.parametrize("rate,expected", [(1.0, True), (0.0, False)])
def test_simulated_scanner_verdicts(broker, rate, expected):
    bridge = start_bridge(broker, "--simulate", str(rate))
    drv = make_driver(broker)
    try:
        assert wait_for(lambda: drv.get_state()["ready"], 10), drv.get_state()
        res = inspect(drv)
        assert res["state"] == "done" and res["result"]["pass"] is expected
    finally:
        drv.close()
        stop_bridge(bridge)


@pytest.mark.parametrize("command,state,passed", [
    ("echo PASS", "done", True),
    ("echo NG scratch", "done", False),
    ('''python3 -c "import json,os; print(json.dumps({'pass': os.environ['QC_PRODUCT']=='ABCD'}))"''', "done", True),
    ("echo maybe", "failed", None),
    ("false", "failed", None),
])
def test_command_scanner(broker, command, state, passed):
    bridge = start_bridge(broker, "--command", command)
    drv = make_driver(broker)
    try:
        assert wait_for(lambda: drv.get_state()["ready"], 10)
        res = inspect(drv)
        assert res["state"] == state, res
        if passed is not None:
            assert res["result"]["pass"] is passed
        else:
            assert "scanner error" in res["error"]
    finally:
        drv.close()
        stop_bridge(bridge)


def test_no_scanner_times_out_and_reports_why(broker):
    drv = make_driver(broker, timeout_s=1.5)
    try:
        st = drv.get_state()
        assert st["ready"] is False and "no scanner heartbeat" in st["detail"]
        res = inspect(drv)
        assert res["state"] == "failed" and "no result from the scanner" in res["error"]
        assert "no scanner heartbeat" in res["error"]
    finally:
        drv.close()


def test_scanner_going_offline_is_seen(broker):
    bridge = start_bridge(broker, "--simulate", "1")
    drv = make_driver(broker)
    try:
        assert wait_for(lambda: drv.get_state()["ready"], 10)
        stop_bridge(bridge)
        assert wait_for(lambda: not drv.get_state()["ready"], 10)
        assert "offline" in drv.get_state()["detail"]
    finally:
        drv.close()


def test_foreign_and_late_results_are_ignored_and_cancel_works(broker):
    drv = make_driver(broker, timeout_s=5, health_topic=None)
    pub = make_client("pub_test", clean_session=True)
    pub.connect(broker.host, broker.port)
    pub.loop_start()
    try:
        cancel = threading.Event()
        result = {}
        t = threading.Thread(target=lambda: result.update(drv.execute(
            {"subtask_id": "t-9", "attempt": 2, "action": "quality_check", "zone": "q", "target": "X"}, cancel)))
        t.start()
        time.sleep(0.5)
        topic = config.topic("qc/qc_01/result")
        pub.publish(topic, json.dumps({"request_id": "t-9#1", "pass": True}), qos=1)      # old attempt
        pub.publish(topic, json.dumps({"request_id": "other#1", "pass": True}), qos=1)    # someone else
        time.sleep(0.5)
        assert t.is_alive(), "must still be waiting for its own answer"
        cancel.set()
        t.join(3)
        assert result["state"] == "cancelled"
    finally:
        pub.loop_stop()
        pub.disconnect()
        drv.close()
