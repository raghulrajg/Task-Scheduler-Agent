"""
End-to-end tests over a real MQTT broker (mosquitto) with simulated robots:
scheduler + robot agents + reasoning agent, all talking MQTT exactly as in
production. Skipped if the `mosquitto` binary is not installed.
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import pytest

import config
from drivers.sim import SimDriver
from mqtt_client import SchedulerMQTTClient
from mqtt_common import make_client
from reasoning_agent import ReasoningAgent
from robot_agent import RobotAgent
from robot_registry import RobotRegistry
from scheduler import TaskScheduler
from validation import SiteConfig

MOSQUITTO = shutil.which("mosquitto") or ("/usr/sbin/mosquitto" if os.path.exists("/usr/sbin/mosquitto") else None)
pytestmark = pytest.mark.skipif(not MOSQUITTO, reason="mosquitto not installed")

INSTRUCTION = {"product": "ABCD", "source": "manufacturing_1", "quality_check": "quality_station",
               "success_dest": "package_area", "fail_dest": "waste_area"}


def wait_for(pred, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def broker():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    d = tempfile.mkdtemp()
    conf = os.path.join(d, "m.conf")
    with open(conf, "w") as f:
        f.write(f"listener {port} 127.0.0.1\nallow_anonymous true\n")
    proc = subprocess.Popen([MOSQUITTO, "-c", conf], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_for(lambda: _port_open(port), timeout=5)
    yield port
    proc.terminate()
    proc.wait(5)


def _port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


class Listener:
    def __init__(self, port, topics):
        self.messages = []
        self.client = make_client(f"listener_{time.time_ns()}", clean_session=True)
        self.client.on_connect = lambda c, u, f, rc, p: c.subscribe([(t, 1) for t in topics])
        self.client.on_message = lambda c, u, m: self.messages.append((m.topic, m.payload))
        self.client.connect("127.0.0.1", port)
        self.client.loop_start()
        time.sleep(0.3)

    def json(self, topic):
        out = []
        for t, p in self.messages:
            if t == topic:
                try:
                    out.append(json.loads(p))
                except ValueError:
                    pass
        return out

    def publish(self, topic, payload, raw=False):
        self.client.publish(topic, payload if raw else json.dumps(payload), qos=1).wait_for_publish(3)

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


class Stack:
    """Scheduler + one sim agent per subsystem, wired over the broker."""

    def __init__(self, port, tmpdir, durations=0.1, qc_pass_rate=1.0, start_agents=True):
        self.port = port
        site = SiteConfig.load("site.json")
        self.registry = RobotRegistry("robots_metadata.json", require_heartbeat=True,
                                      heartbeat_timeout_s=site.heartbeat_timeout_s,
                                      min_battery_percent=site.min_battery_percent)
        holder = {}
        self.mqtt = SchedulerMQTTClient(
            client_id=f"sched_{time.time_ns()}", host="127.0.0.1", port=port,
            on_status=lambda r, s, p: holder["s"].on_robot_status(r, s, p),
            on_state=lambda r, s, p: holder["s"].on_robot_heartbeat(r, s, p),
            on_instruction=self._on_instruction, on_command=lambda c: holder["s"].handle_command(c))
        self.scheduler = holder["s"] = TaskScheduler(self.registry, self.mqtt, site,
                                                     state_path=os.path.join(tmpdir, "state.json"))
        self.rejected = []
        self._stop = threading.Event()
        self._ticker = threading.Thread(target=self._tick, daemon=True)
        self.mqtt.connect()
        self._ticker.start()
        self.agents = {}
        if start_agents:
            with open("robots_metadata.json") as f:
                robots = json.load(f)["robots"]
            for r in robots:
                topics = [s["topic"] for s in r["subsystems"].values()]
                for sub in r["subsystems"].values():
                    self.add_agent(r["id"], sub["topic"], tuple(t for t in topics if t != sub["topic"]),
                                   durations, qc_pass_rate)
        assert wait_for(lambda: self.mqtt.connected, 5)

    def add_agent(self, robot_id, topic, siblings=(), durations=0.1, qc_pass_rate=1.0):
        drv = SimDriver({"durations_s": {a: durations for a in ("navigate", "pick", "place", "quality_check")},
                         "qc_pass_rate": qc_pass_rate})
        agent = RobotAgent(robot_id, topic, drv, heartbeat_s=1, interlock_with=siblings,
                           client_id=f"agent_{robot_id}_{topic}_{time.time_ns()}", host="127.0.0.1", port=self.port)
        agent.start()
        self.agents[(robot_id, topic)] = agent
        return agent

    def _on_instruction(self, instr):
        try:
            self.scheduler.submit_instruction(instr)
        except ValueError as e:
            self.rejected.append(str(e))

    def _tick(self):
        while not self._stop.wait(0.5):
            self.scheduler.tick()
            for a in list(self.agents.values()):
                if not a._stop.is_set():
                    a._publish_heartbeat()

    def all_healthy(self):
        return all(r["healthy"] for r in self.registry.fleet_view())

    def close(self):
        self._stop.set()
        for a in self.agents.values():
            if not a._stop.is_set():
                a.shutdown()
        self.mqtt.disconnect()


@pytest.fixture
def stack(broker, tmp_path):
    s = Stack(broker, str(tmp_path))
    assert wait_for(s.all_healthy, 10), s.registry.fleet_view()
    yield s
    s.close()


def test_end_to_end_task_over_mqtt(stack, broker):
    ev = Listener(broker, [config.EVENT_TOPIC])
    ev.publish(config.INSTRUCTION_TOPIC, {**INSTRUCTION, "request_id": "e2e-1"})
    assert wait_for(lambda: any(e["event"] == "task_completed" for e in ev.json(config.EVENT_TOPIC)), 20), \
        [e["event"] for e in ev.json(config.EVENT_TOPIC)]
    events = [e for e in ev.json(config.EVENT_TOPIC) if e.get("request_id") == "e2e-1"]
    carriers = {e["robot_id"] for e in events if e["event"] == "subtask_dispatched"
                and e["action"] in ("navigate", "pick", "place")}
    assert len(carriers) == 1
    assert all(r.status == "idle" for r in stack.registry.all())
    ev.close()


def test_garbage_messages_do_not_kill_scheduler(stack, broker):
    ev = Listener(broker, [config.EVENT_TOPIC])
    ev.publish(config.INSTRUCTION_TOPIC, ["oops"])
    ev.publish(config.INSTRUCTION_TOPIC, b"\xff\xfe not json", raw=True)
    ev.publish(config.INSTRUCTION_TOPIC, {**INSTRUCTION, "source": "mars"})
    ev.publish("robots/robot_01/amr/status", "just a string")
    ev.publish("robots/robot_01/amr/state", [1, 2, 3])
    ev.publish(config.COMMAND_TOPIC, {"cmd": 42})
    ev.publish(config.INSTRUCTION_TOPIC, INSTRUCTION)
    assert wait_for(lambda: any(e["event"] == "task_completed" for e in ev.json(config.EVENT_TOPIC)), 20)
    assert len(stack.rejected) == 2
    ev.close()


def test_three_parallel_tasks_complete(stack, broker):
    ev = Listener(broker, [config.EVENT_TOPIC])
    for i in range(3):
        ev.publish(config.INSTRUCTION_TOPIC, {**INSTRUCTION, "product": f"P{i}", "request_id": f"p{i}"})
    assert wait_for(lambda: sum(e["event"] == "task_completed" for e in ev.json(config.EVENT_TOPIC)) == 3, 30)
    ev.close()


def test_task_survives_agent_wifi_drop(broker, tmp_path):
    """Persistent sessions: a task published while the agent is offline is
    delivered when it reconnects (the old clean_session=True code lost it)."""
    stack = Stack(broker, str(tmp_path))
    try:
        assert wait_for(stack.all_healthy, 10)
        ev = Listener(broker, [config.EVENT_TOPIC])
        # take every amr agent except robot_01's off the air by putting their robots in maintenance
        for rid in ("robot_02", "robot_03", "robot_04"):
            stack.scheduler.handle_command({"cmd": "set_maintenance", "robot_id": rid, "on": True})
        amr = stack.agents[("robot_01", "amr")]
        amr._client.disconnect()                      # clean disconnect keeps the persistent session
        amr._client.loop_stop()
        # registry still has a fresh heartbeat, so the scheduler dispatches into the gap
        ev.publish(config.INSTRUCTION_TOPIC, INSTRUCTION)
        assert wait_for(lambda: any(e["event"] == "subtask_dispatched" for e in ev.json(config.EVENT_TOPIC)), 5)
        time.sleep(0.5)
        amr._client.reconnect()
        amr._client.loop_start()
        assert wait_for(lambda: any(e["event"] == "task_completed" for e in ev.json(config.EVENT_TOPIC)), 20), \
            [e["event"] for e in ev.json(config.EVENT_TOPIC)]
        names = [e["event"] for e in ev.json(config.EVENT_TOPIC)]
        assert "subtask_retry" not in names, "task should have been delivered from the session queue, not retried"
        ev.close()
    finally:
        stack.close()


def test_agent_crash_publishes_last_will_and_robot_is_skipped(stack, broker):
    agent = stack.agents[("robot_01", "arm")]
    agent._stop.set()
    agent._client.loop_stop()
    agent._client._sock_close()                      # die without DISCONNECT -> broker sends LWT
    assert wait_for(lambda: not stack.registry.get("robot_01").health["arm"].online, 10)
    ev = Listener(broker, [config.EVENT_TOPIC])
    ev.publish(config.INSTRUCTION_TOPIC, INSTRUCTION)
    assert wait_for(lambda: any(e["event"] == "subtask_dispatched" for e in ev.json(config.EVENT_TOPIC)), 5)
    first = next(e for e in ev.json(config.EVENT_TOPIC) if e["event"] == "subtask_dispatched")
    assert first["robot_id"] != "robot_01"
    ev.close()


def test_operator_cancel_over_mqtt(broker, tmp_path):
    stack = Stack(broker, str(tmp_path), durations=3.0)
    try:
        assert wait_for(stack.all_healthy, 10)
        ev = Listener(broker, [config.EVENT_TOPIC])
        ev.publish(config.INSTRUCTION_TOPIC, INSTRUCTION)
        assert wait_for(lambda: any(e["event"] == "subtask_dispatched" for e in ev.json(config.EVENT_TOPIC)), 5)
        task_id = next(e for e in ev.json(config.EVENT_TOPIC) if e["event"] == "task_accepted")["task_id"]
        robot = next(e for e in ev.json(config.EVENT_TOPIC) if e["event"] == "subtask_dispatched")["robot_id"]
        time.sleep(0.5)
        ev.publish(config.COMMAND_TOPIC, {"cmd": "cancel_task", "task_id": task_id})
        assert wait_for(lambda: stack.scheduler.task_state(task_id) == "cancelled", 5)
        assert wait_for(lambda: stack.registry.get(robot).status == "idle", 5), \
            "robot should be released once its agent confirms the cancel"
        ev.close()
    finally:
        stack.close()


def test_reasoning_agent_validates_llm_output(stack, broker, monkeypatch):
    outputs = {
        "good": dict(INSTRUCTION),
        "bad location": {**INSTRUCTION, "source": "the moon"},
        "not a job": {"error": "no transport requested"},
    }
    site = SiteConfig.load("site.json")
    agent = ReasoningAgent(site, llm=lambda prompt, system: outputs[prompt],
                           client_id=f"reasoner_{time.time_ns()}")
    monkeypatch.setattr(config, "MQTT_BROKER_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "MQTT_BROKER_PORT", broker)
    t = threading.Thread(target=agent.run_forever, daemon=True)
    t.start()
    ev = Listener(broker, [config.EVENT_TOPIC, config.HMI_FEEDBACK_TOPIC, config.INSTRUCTION_TOPIC])
    for prompt in outputs:
        ev.publish(config.HMI_PROMPT_TOPIC, prompt, raw=True)
    assert wait_for(lambda: len(ev.json(config.HMI_FEEDBACK_TOPIC)) == 3, 10)
    fb = ev.json(config.HMI_FEEDBACK_TOPIC)
    assert sum(1 for f in fb if f["ok"]) == 1
    assert len(ev.json(config.INSTRUCTION_TOPIC)) == 1, "only the valid instruction may reach the scheduler"
    assert wait_for(lambda: any(e["event"] == "task_completed" for e in ev.json(config.EVENT_TOPIC)), 20)
    agent._stop.set()
    ev.close()
