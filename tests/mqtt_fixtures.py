"""Broker fixture shared by tests that need a real MQTT broker (EMQX via TEST_MQTT_*, else local mosquitto)."""
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass

import pytest

import config
from mqtt_common import make_client


def wait_for(pred, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


MOSQUITTO = shutil.which("mosquitto") or ("/usr/sbin/mosquitto" if os.path.exists("/usr/sbin/mosquitto") else None)


@dataclass
class Broker:
    host: str
    port: int
    prefix: str = ""

    def env(self):
        """Environment for a subprocess that must talk to this broker in this test's namespace."""
        e = dict(os.environ, MQTT_BROKER_HOST=self.host, MQTT_BROKER_PORT=str(self.port), MQTT_TLS="0",
                 MQTT_TOPIC_PREFIX=self.prefix, RUN_MODE="real")
        e["MQTT_USERNAME"] = config.MQTT_USERNAME or ""
        e["MQTT_PASSWORD"] = config.MQTT_PASSWORD or ""
        return e



@pytest.fixture
def broker(monkeypatch):
    prefix = f"pytest/{uuid.uuid4().hex[:10]}/"
    monkeypatch.setattr(config, "TOPIC_PREFIX", prefix)       # isolate this test's traffic
    host = os.environ.get("TEST_MQTT_HOST")
    if host:
        monkeypatch.setattr(config, "MQTT_USERNAME", os.environ.get("TEST_MQTT_USERNAME") or None)
        monkeypatch.setattr(config, "MQTT_PASSWORD", os.environ.get("TEST_MQTT_PASSWORD") or None)
        b = Broker(host, int(os.environ.get("TEST_MQTT_PORT", "1883")), prefix)
        yield b
        clear_retained(b, prefix)
        return
    if not MOSQUITTO:
        pytest.skip("no test broker: set TEST_MQTT_HOST/PORT/USERNAME/PASSWORD (e.g. your EMQX)")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    d = tempfile.mkdtemp()
    conf = os.path.join(d, "m.conf")
    with open(conf, "w") as f:
        f.write(f"listener {port} 127.0.0.1\nallow_anonymous true\n")
    proc = subprocess.Popen([MOSQUITTO, "-c", conf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_for(lambda: _port_open("127.0.0.1", port), timeout=5)
    yield Broker("127.0.0.1", port, prefix)
    proc.terminate()
    proc.wait(5)


def _port_open(host, port):
    try:
        socket.create_connection((host, port), timeout=0.2).close()
        return True
    except OSError:
        return False


def clear_retained(b, prefix):
    """Remove every retained message this test left under its namespace."""
    seen = set()
    c = make_client(f"cleanup_{uuid.uuid4().hex[:8]}", clean_session=True)
    c.on_connect = lambda cl, u, f, rc, p: cl.subscribe(prefix + "#", qos=1)
    c.on_message = lambda cl, u, m: seen.add(m.topic) if m.retain else None
    c.connect(b.host, b.port)
    c.loop_start()
    time.sleep(1.0)
    for t in seen:
        c.publish(t, b"", qos=1, retain=True).wait_for_publish(3)
    c.loop_stop()
    c.disconnect()


def purge_sessions(b, client_ids):
    """Drop the persistent sessions test clients left on the broker."""
    threads = [threading.Thread(target=_purge_one, args=(b, cid)) for cid in client_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)


def _purge_one(b, cid):
    c = make_client("", clean_session=True)
    c._client_id = cid if isinstance(cid, bytes) else cid.encode()
    try:
        c.connect(b.host, b.port)
        c.loop_start()
        time.sleep(0.2)
        c.loop_stop()
        c.disconnect()
    except OSError:
        pass


