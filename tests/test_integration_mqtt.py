"""
End-to-end tests over a real MQTT broker with simulated robots: scheduler +
robot agents + reasoning agent, all talking MQTT exactly as in production.

Broker selection:
  * your EMQX (or any broker):  TEST_MQTT_HOST=172.25.200.57 TEST_MQTT_PORT=1883 \
                                TEST_MQTT_USERNAME=... TEST_MQTT_PASSWORD=... pytest
    Every test runs in its own random topic namespace ("pytest/<id>/...") with
    unique client ids, so it never touches the real robots' topics, and its
    retained messages and sessions are removed afterwards.
  * otherwise a local mosquitto binary, if installed;
  * otherwise these tests are skipped.
"""
import json
import os
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

from mqtt_fixtures import Broker, broker, clear_retained, purge_sessions, wait_for  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

INSTRUCTION = {"product": "ABCD", "source": "manufacturing_1", "quality_check": "quality_station",
               "success_dest": "package_area", "fail_dest": "waste_area"}

# Dobot projects for site.json as shipped: quality_station is a conveyor, so the
# arm also puts the part on the belt and takes it off at the exit there.
CONVEYOR_PROJECTS = ("pick_mfg", "place_belt", "pick_belt", "place_pkg", "place_waste")
DOBOT_PROJECTS = {"pick": {"manufacturing_1": "pick_mfg", "quality_station": "pick_belt"},
                  "place": {"quality_station": "place_belt", "package_area": "place_pkg", "waste_area": "place_waste"}}


class Listener:
    def __init__(self, broker, topics):
        self.messages = []
        self.client = make_client(f"listener_{time.time_ns()}", clean_session=True)
        self.client.on_connect = lambda c, u, f, rc, p: c.subscribe([(t, 1) for t in topics])
        self.client.on_message = lambda c, u, m: self.messages.append((m.topic, m.payload))
        self.client.connect(broker.host, broker.port)
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

    def __init__(self, broker, tmpdir, durations=0.1, qc_pass_rate=1.0, start_agents=True, site=None):
        self.broker = broker
        self.host, self.port = broker.host, broker.port
        site = site or SiteConfig.load("site.json")
        self.registry = RobotRegistry("robots_metadata.json", require_heartbeat=True,
                                      heartbeat_timeout_s=site.heartbeat_timeout_s,
                                      min_battery_percent=site.min_battery_percent)
        holder = {}
        self.mqtt = SchedulerMQTTClient(
            client_id=f"sched_{time.time_ns()}", host=self.host, port=self.port,
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
                           client_id=f"agent_{robot_id}_{topic}_{time.time_ns()}", host=self.host, port=self.port)
        agent.start()
        self.agents[(robot_id, topic)] = agent
        return agent

    def add_driver_agent(self, robot_id, topic, driver, siblings=(), require_travel_pose=()):
        agent = RobotAgent(robot_id, topic, driver, heartbeat_s=1, interlock_with=siblings,
                           require_travel_pose=require_travel_pose,
                           client_id=f"agent_{robot_id}_{topic}_{time.time_ns()}", host=self.host, port=self.port)
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
        threads = [threading.Thread(target=a.shutdown) for a in self.agents.values() if not a._stop.is_set()]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.mqtt.disconnect()
        purge_sessions(self.broker, [self.mqtt._client._client_id] +
                       [a._client._client_id for a in self.agents.values()])


@pytest.fixture
def stack(broker, tmp_path):
    s = Stack(broker, str(tmp_path))
    assert wait_for(s.all_healthy, 10), s.registry.fleet_view()
    yield s
    s.close()


def test_end_to_end_task_over_mqtt(stack, broker):
    ev = Listener(broker, [config.topic(config.EVENT_TOPIC)])
    ev.publish(config.topic(config.INSTRUCTION_TOPIC), {**INSTRUCTION, "request_id": "e2e-1"})
    assert wait_for(lambda: any(e["event"] == "task_completed" for e in ev.json(config.topic(config.EVENT_TOPIC))), 20), \
        [e["event"] for e in ev.json(config.topic(config.EVENT_TOPIC))]
    events = [e for e in ev.json(config.topic(config.EVENT_TOPIC)) if e.get("request_id") == "e2e-1"]
    carriers = {e["robot_id"] for e in events if e["event"] == "subtask_dispatched"
                and e["action"] in ("navigate", "pick", "place")}
    assert len(carriers) == 1
    assert all(r.status == "idle" for r in stack.registry.all())
    ev.close()


def test_garbage_messages_do_not_kill_scheduler(stack, broker):
    ev = Listener(broker, [config.topic(config.EVENT_TOPIC)])
    ev.publish(config.topic(config.INSTRUCTION_TOPIC), ["oops"])
    ev.publish(config.topic(config.INSTRUCTION_TOPIC), b"\xff\xfe not json", raw=True)
    ev.publish(config.topic(config.INSTRUCTION_TOPIC), {**INSTRUCTION, "source": "mars"})
    ev.publish(config.topic("robots/robot_01/amr/status"), "just a string")
    ev.publish(config.topic("robots/robot_01/amr/state"), [1, 2, 3])
    ev.publish(config.topic(config.COMMAND_TOPIC), {"cmd": 42})
    ev.publish(config.topic(config.INSTRUCTION_TOPIC), INSTRUCTION)
    assert wait_for(lambda: any(e["event"] == "task_completed" for e in ev.json(config.topic(config.EVENT_TOPIC))), 20)
    assert len(stack.rejected) == 2
    ev.close()


def test_three_parallel_tasks_complete(stack, broker):
    ev = Listener(broker, [config.topic(config.EVENT_TOPIC)])
    for i in range(3):
        ev.publish(config.topic(config.INSTRUCTION_TOPIC), {**INSTRUCTION, "product": f"P{i}", "request_id": f"p{i}"})
    assert wait_for(lambda: sum(e["event"] == "task_completed" for e in ev.json(config.topic(config.EVENT_TOPIC))) == 3, 30)
    ev.close()


def test_task_survives_agent_wifi_drop(broker, tmp_path):
    """Persistent sessions: a task published while the agent is offline is
    delivered when it reconnects (the old clean_session=True code lost it)."""
    stack = Stack(broker, str(tmp_path))
    try:
        assert wait_for(stack.all_healthy, 10)
        ev = Listener(broker, [config.topic(config.EVENT_TOPIC)])
        # take every amr agent except robot_01's off the air by putting their robots in maintenance
        for rid in ("robot_02", "robot_03", "robot_04"):
            stack.scheduler.handle_command({"cmd": "set_maintenance", "robot_id": rid, "on": True})
        amr = stack.agents[("robot_01", "amr")]
        amr._client.disconnect()                      # clean disconnect keeps the persistent session
        amr._client.loop_stop()
        # registry still has a fresh heartbeat, so the scheduler dispatches into the gap
        ev.publish(config.topic(config.INSTRUCTION_TOPIC), INSTRUCTION)
        assert wait_for(lambda: any(e["event"] == "subtask_dispatched" for e in ev.json(config.topic(config.EVENT_TOPIC))), 5)
        time.sleep(0.5)
        amr._client.reconnect()
        amr._client.loop_start()
        assert wait_for(lambda: any(e["event"] == "task_completed" for e in ev.json(config.topic(config.EVENT_TOPIC))), 20), \
            [e["event"] for e in ev.json(config.topic(config.EVENT_TOPIC))]
        names = [e["event"] for e in ev.json(config.topic(config.EVENT_TOPIC))]
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
    ev = Listener(broker, [config.topic(config.EVENT_TOPIC)])
    ev.publish(config.topic(config.INSTRUCTION_TOPIC), INSTRUCTION)
    assert wait_for(lambda: any(e["event"] == "subtask_dispatched" for e in ev.json(config.topic(config.EVENT_TOPIC))), 5)
    first = next(e for e in ev.json(config.topic(config.EVENT_TOPIC)) if e["event"] == "subtask_dispatched")
    assert first["robot_id"] != "robot_01"
    ev.close()


def test_operator_cancel_over_mqtt(broker, tmp_path):
    stack = Stack(broker, str(tmp_path), durations=3.0)
    try:
        assert wait_for(stack.all_healthy, 10)
        ev = Listener(broker, [config.topic(config.EVENT_TOPIC)])
        ev.publish(config.topic(config.INSTRUCTION_TOPIC), INSTRUCTION)
        assert wait_for(lambda: any(e["event"] == "subtask_dispatched" for e in ev.json(config.topic(config.EVENT_TOPIC))), 5)
        task_id = next(e for e in ev.json(config.topic(config.EVENT_TOPIC)) if e["event"] == "task_accepted")["task_id"]
        robot = next(e for e in ev.json(config.topic(config.EVENT_TOPIC)) if e["event"] == "subtask_dispatched")["robot_id"]
        time.sleep(0.5)
        ev.publish(config.topic(config.COMMAND_TOPIC), {"cmd": "cancel_task", "task_id": task_id})
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
    monkeypatch.setattr(config, "MQTT_BROKER_HOST", broker.host)
    monkeypatch.setattr(config, "MQTT_BROKER_PORT", broker.port)
    t = threading.Thread(target=agent.run_forever, daemon=True)
    t.start()
    ev = Listener(broker, [config.topic(config.EVENT_TOPIC), config.topic(config.HMI_FEEDBACK_TOPIC), config.topic(config.INSTRUCTION_TOPIC)])
    for prompt in outputs:
        ev.publish(config.topic(config.HMI_PROMPT_TOPIC), prompt, raw=True)
    assert wait_for(lambda: len(ev.json(config.topic(config.HMI_FEEDBACK_TOPIC))) == 3, 10)
    fb = ev.json(config.topic(config.HMI_FEEDBACK_TOPIC))
    assert sum(1 for f in fb if f["ok"]) == 1
    assert len(ev.json(config.topic(config.INSTRUCTION_TOPIC))) == 1, "only the valid instruction may reach the scheduler"
    assert wait_for(lambda: any(e["event"] == "task_completed" for e in ev.json(config.topic(config.EVENT_TOPIC))), 20)
    agent._stop.set()
    t.join(5)
    ev.close()
    purge_sessions(broker, [agent._client._client_id])


def test_full_job_with_dobot_and_web_amr_drivers(broker, tmp_path, monkeypatch):
    """robot_01 = web-dashboard AMR (http_amr driver) + Dobot Nova 2 (dobot driver),
    against protocol-faithful fakes; QC simulated. Exercises the real drivers,
    the travel-pose interlock and the whole MQTT chain."""
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from drivers import create_driver
    from fake_amr import FakeAMR
    from fake_dobot import FakeDobot

    amr = FakeAMR(points=("station_A", "station_B", "station_C", "station_D"), drive_time=0.3, pickup_delay=0.1)
    arm = FakeDobot(run_time=0.2, projects=CONVEYOR_PROJECTS)
    monkeypatch.setenv("TEST_AMR_PW", "pw{1}")
    stack = Stack(broker, str(tmp_path), start_agents=False)
    try:
        amr_drv = create_driver({
            "type": "http_amr", "base_url": amr.url,
            "login": {"method": "POST", "path": "/api/login",
                      "json": {"username": "admin", "password": "${TEST_AMR_PW}"}, "token_path": "data.token"},
            "locations": {"manufacturing_1": {"id": "station_A"}, "quality_station": {"id": "station_B"},
                          "package_area": {"id": "station_C"}, "waste_area": {"id": "station_D"}},
            "navigate": {"method": "POST", "path": "/api/nav/goto", "json": {"point": "{id}"},
                         "ok_path": "code", "ok_values": [0]},
            "cancel": {"method": "POST", "path": "/api/nav/cancel"},
            "status": {"method": "GET", "path": "/api/robot/status"},
            "fields": {"nav_state": "data.nav.state", "running_values": ["RUNNING"],
                       "arrived_values": ["SUCCEEDED"], "failed_values": ["FAILED", "CANCELED"],
                       "target": "data.nav.target", "battery": "data.battery", "battery_scale": 100,
                       "estop": "data.emergency"},
            "poll_interval_s": 0.05, "settle_s": 1.0})
        arm_drv = create_driver({
            "type": "dobot", "host": "127.0.0.1", "port": arm.port, "protocol": "v4",
            "projects": DOBOT_PROJECTS,
            "travel_joints": [0, -30, 120, 0, 90, 0], "poll_interval_s": 0.05})
        stack.add_driver_agent("robot_01", "amr", amr_drv, siblings=("arm",), require_travel_pose=("arm",))
        stack.add_driver_agent("robot_01", "arm", arm_drv, siblings=("amr",))
        stack.add_agent("qc_01", "scanner", qc_pass_rate=0.0)          # part fails QC -> waste
        assert wait_for(lambda: stack.registry.fleet_view() and next(
            r for r in stack.registry.fleet_view() if r["id"] == "robot_01")["healthy"], 10), \
            stack.registry.fleet_view()

        ev = Listener(broker, [config.topic(config.EVENT_TOPIC)])
        ev.publish(config.topic(config.INSTRUCTION_TOPIC), {**INSTRUCTION, "request_id": "hw-1"})
        assert wait_for(lambda: any(e["event"] in ("task_completed", "task_failed")
                                    for e in ev.json(config.topic(config.EVENT_TOPIC))), 30), \
            [e["event"] for e in ev.json(config.topic(config.EVENT_TOPIC))]
        events = ev.json(config.topic(config.EVENT_TOPIC))
        assert [e for e in events if e["event"] == "task_completed"][0]["branch"] == "fail"
        assert "subtask_retry" not in [e["event"] for e in events], "interlock/heartbeat race caused a retry"
        assert [r[2] for r in amr.requests if r[1] == "/api/nav/goto"] == [
            {"point": "station_A"}, {"point": "station_B"}, {"point": "station_D"}]
        assert 'RunScript("pick_mfg")' in arm.commands and 'RunScript("place_waste")' in arm.commands
        ev.close()
    finally:
        stack.close()
        amr.close()
        arm.close()


@pytest.mark.parametrize("qc", ["conveyor", "scanner_bridge"])
def test_robot_01_as_deployed_onix_dobot_qc(broker, tmp_path, qc):
    """The real deployment shape over the broker: ONIX base (onix driver vs the
    ONIX simulator, x/y/theta from stations.json, verified arrival), Dobot arm
    (dobot driver vs the fake controller, only acts when the base reports it is
    at the zone), and QC over MQTT only:
      conveyor        site.json as shipped: the arm puts the part on the belt, the
                      conveyor_inspection driver takes the simulated Opta's verdict
                      (tools/conveyor_sim.py), the arm takes the part off the belt;
      scanner_bridge  part inspected in the gripper: mqtt_inspection driver <->
                      tools/scanner_bridge.py."""
    import math
    import subprocess
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from drivers import create_driver
    from fake_dobot import FakeDobot
    from tools.onix_simulator import OnixSimulator

    poses = {"manufacturing_1": (1.0, 0.0, 0.0), "quality_station": (1.0, 1.0, math.pi / 2),
             "package_area": (0.0, 1.0, math.pi), "waste_area": (-0.5, 0.0, -math.pi / 2)}
    stations_file = tmp_path / "stations.json"
    stations_file.write_text(json.dumps({
        "defaults": {"xy_tolerance_m": 0.1, "yaw_tolerance_deg": 5}, "stations": {},
        "locations": {**{k: {"x": x, "y": y, "theta": t} for k, (x, y, t) in poses.items()},
                      "rest_position": {"station": None, "x": None, "y": None, "theta": None}}}))
    onix = OnixSimulator(map_body={"header": {}, "advancedPointList": []}, start=(0.0, 0.0, 0.0),
                         speed=3.0, turn_speed=4.0, status_hz=20)
    arm = FakeDobot(run_time=0.2, projects=CONVEYOR_PROJECTS)
    site = SiteConfig.load("site.json")
    if qc == "conveyor":
        helper = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "conveyor_sim.py"), "--station",
                                   "qc_01", "--pass-rate", "1", "--delay", "0.3"],
                                  env={**broker.env(), "RUN_MODE": "test"}, cwd=ROOT,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        qc_cfg = {"type": "conveyor_inspection", "station": "qc_01", "timeout_s": 10}
        projects = DOBOT_PROJECTS
    else:
        for meta in site.locations.values():
            meta.pop("conveyor", None)
        helper = subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "scanner_bridge.py"), "--station",
                                   "qc_01", "--heartbeat", "1", "--command", "echo PASS"],
                                  env=broker.env(), cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        qc_cfg = {"type": "mqtt_inspection", "station": "qc_01", "timeout_s": 10}
        projects = {"pick": {"manufacturing_1": "pick_mfg"},
                    "place": {"package_area": "place_pkg", "waste_area": "place_waste"}}
    stack = Stack(broker, str(tmp_path), start_agents=False, site=site)
    try:
        amr_drv = create_driver({"type": "onix", "host": "127.0.0.1", "ports": onix.ports,
                                 "stations_file": str(stations_file), "settle_s": 0.3, "start_timeout_s": 5})
        arm_drv = create_driver({
            "type": "dobot", "host": "127.0.0.1", "port": arm.port, "protocol": "v4",
            "projects": projects, "travel_joints": [0, -30, 120, 0, 90, 0], "poll_interval_s": 0.05})
        qc_drv = create_driver({**qc_cfg, "_mqtt_host": broker.host, "_mqtt_port": broker.port})
        stack.add_driver_agent("robot_01", "amr", amr_drv, siblings=("arm",), require_travel_pose=("arm",))
        arm_agent = RobotAgent("robot_01", "arm", arm_drv, heartbeat_s=1, interlock_with=("amr",),
                               require_base_at_zone="amr", client_id=f"agent_robot_01_arm_{time.time_ns()}",
                               host=broker.host, port=broker.port)
        arm_agent.start()
        stack.agents[("robot_01", "arm")] = arm_agent
        stack.add_driver_agent("qc_01", "scanner", qc_drv)

        def healthy(rid):
            return next(r for r in stack.registry.fleet_view() if r["id"] == rid)["healthy"]
        assert wait_for(lambda: healthy("robot_01") and healthy("qc_01"), 15), stack.registry.fleet_view()
        assert wait_for(lambda: stack.registry.reachable_locations() == set(poses), 5)
        time.sleep(1.0)    # let the QC helper subscribe

        ev = Listener(broker, [config.topic(config.EVENT_TOPIC)])
        # rest_position has no coordinates yet -> refused before anything moves
        ev.publish(config.topic(config.INSTRUCTION_TOPIC), {**INSTRUCTION, "fail_dest": "rest_position"})
        assert wait_for(lambda: stack.rejected, 5)
        assert "no mobile base has coordinates for ['rest_position']" in stack.rejected[0]

        ev.publish(config.topic(config.INSTRUCTION_TOPIC), {**INSTRUCTION, "request_id": "onix-1"})
        assert wait_for(lambda: any(e["event"] in ("task_completed", "task_failed")
                                    for e in ev.json(config.topic(config.EVENT_TOPIC))), 60), \
            [(e["event"], e.get("error") or e.get("reason")) for e in ev.json(config.topic(config.EVENT_TOPIC))]
        events = ev.json(config.topic(config.EVENT_TOPIC))
        done = [e for e in events if e["event"] == "task_completed"]
        assert done and done[0]["branch"] == "success", [(e["event"], e.get("error")) for e in events]
        assert "subtask_retry" not in [e["event"] for e in events], [e for e in events
                                                                      if e["event"] == "subtask_retry"]
        goals = [r["free_nav"] for a, r in onix.requests if a == "4002"]
        assert [(g["linear_x"], g["linear_y"], g["angular_z"]) for g in goals] == [
            poses["manufacturing_1"], poses["quality_station"], poses["package_area"]]
        scripts = [c for c in arm.commands if c.startswith("RunScript")]
        if qc == "conveyor":
            assert scripts == ['RunScript("pick_mfg")', 'RunScript("place_belt")', 'RunScript("pick_belt")',
                               'RunScript("place_pkg")'], scripts
            qc_event = next(e for e in events if e["event"] == "qc_result")
            assert qc_event["details"]["result"] == "OK" and qc_event["details"]["valid"] is True
        else:
            assert scripts == ['RunScript("pick_mfg")', 'RunScript("place_pkg")'], scripts
        st = onix.status()
        assert math.hypot(st["x"] - 0.0, st["y"] - 1.0) < 0.1      # parked at package_area
        ev.close()
    finally:
        stack.close()
        helper.terminate()
        helper.wait(10)
        onix.close()
        arm.close()
