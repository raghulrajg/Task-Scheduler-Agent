"""
Conveyor QC station (the conveyor agent: Arduino Opta + Hikrobot SC3000).

  * drivers/conveyor.py: correlating the Opta's unsolicited results with
    inspections (no broker needed: results are injected directly);
  * the conveyor job flow in the scheduler (place on belt -> QC -> pick from exit);
  * over a real broker: the Opta's exact JSON, published the way the Opta does.
"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

import config
from conftest import INSTRUCTION, FakeMQTT, beat, healthy_registry
from drivers import create_driver
from scheduler import TaskScheduler
from task_decomposer import decompose
from validation import SiteConfig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Exactly what the Opta publishes (conveyor_agent README / publishResult())
OPTA_OK = {"x": 12.5, "y": 30.2, "theta": 45.0, "result": "OK", "valid": True, "cycle": 5, "raw": "12.5;30.2;45.0;OK;"}
OPTA_NG = {"x": 10.0, "y": 20.0, "theta": 5.0, "result": "NG", "valid": True, "cycle": 6, "raw": "10;20;5;NG;"}
OPTA_INVALID = {"x": None, "y": None, "theta": None, "result": "NG", "valid": False, "cycle": 7, "raw": "garbage"}


# ============================================================== driver (no broker)
@pytest.fixture
def conveyor():
    drivers = []

    def make(**extra):
        cfg = {"type": "conveyor_inspection", "station": "qc_01", "timeout_s": 1.0, "announce": False,
               "_mqtt_host": "127.0.0.1", "_mqtt_port": 1}       # nothing listens there; results are injected
        cfg.update(extra)
        drv = create_driver(cfg)
        drv.connected = True
        drivers.append(drv)
        return drv
    yield make
    for d in drivers:
        d.close()


def inspect(drv, not_before=None, cancel=None, sid="t-5"):
    task = {"subtask_id": sid, "attempt": 1, "action": "quality_check", "zone": "quality_station", "target": "ABCD"}
    if not_before is not None:
        task["not_before"] = not_before
    return drv.execute(task, cancel or threading.Event())


@pytest.mark.parametrize("payload,passed", [(OPTA_OK, True), (OPTA_NG, False), (OPTA_INVALID, False)])
def test_opta_verdicts(conveyor, payload, passed):
    drv = conveyor()
    t0 = time.time()
    drv.add_result(payload)
    res = inspect(drv, not_before=t0)
    assert res["state"] == "done" and res["result"]["pass"] is passed
    d = res["result"]["details"]
    assert d["cycle"] == payload["cycle"] and d["raw"] == payload["raw"] and d["valid"] is payload["valid"]


def test_invalid_message_can_be_a_failed_inspection_instead(conveyor):
    drv = conveyor(invalid_is_fail=False)
    t0 = time.time()
    drv.add_result(OPTA_INVALID)
    res = inspect(drv, not_before=t0)
    assert res["state"] == "failed" and "invalid camera message" in res["error"]


@pytest.mark.parametrize("bad", [{**OPTA_OK, "result": "ok"}, {**OPTA_OK, "result": "MAYBE"},
                                 {k: v for k, v in OPTA_OK.items() if k != "result"}])
def test_unrecognised_verdict_is_never_a_pass(conveyor, bad):
    drv = conveyor()
    t0 = time.time()
    drv.add_result(bad)
    res = inspect(drv, not_before=t0)
    assert res["state"] == "failed" and "no recognised verdict" in res["error"]


def test_result_that_arrived_before_the_qc_step_is_used(conveyor):
    """Fast belt, slow arm retract: the verdict can come before the QC step is dispatched."""
    drv = conveyor()
    loaded_at = time.time()
    drv.add_result(OPTA_OK, t=loaded_at + 0.5)
    res = inspect(drv, not_before=loaded_at)
    assert res["state"] == "done" and res["result"]["pass"] is True


def test_result_from_before_the_part_was_loaded_is_ignored(conveyor):
    drv = conveyor(clock_skew_s=0)
    drv.add_result(OPTA_OK, t=time.time() - 30)        # an older part / a manual test
    res = inspect(drv, not_before=time.time() - 5)
    assert res["state"] == "failed" and "no conveyor result" in res["error"]


def test_each_result_is_used_once_in_arrival_order(conveyor):
    drv = conveyor()
    t0 = time.time()
    drv.add_result(OPTA_NG)
    drv.add_result(OPTA_OK)
    first, second = inspect(drv, not_before=t0, sid="a-5"), inspect(drv, not_before=t0, sid="b-5")
    assert first["result"]["pass"] is False and second["result"]["pass"] is True
    assert inspect(drv, not_before=t0, sid="c-5")["state"] == "failed"


def test_waits_for_a_result_that_comes_later(conveyor):
    drv = conveyor(timeout_s=5)
    t0 = time.time()
    threading.Timer(0.4, drv.add_result, args=(OPTA_NG,)).start()
    res = inspect(drv, not_before=t0)
    assert res["state"] == "done" and res["result"]["pass"] is False
    assert res["result"]["details"]["waited_s"] >= 0.3


def test_duplicate_delivery_counts_once(conveyor):
    drv = conveyor()
    t0 = time.time()
    assert drv.add_result(OPTA_OK) is True
    assert drv.add_result(dict(OPTA_OK)) is False
    assert inspect(drv, not_before=t0, sid="a-5")["state"] == "done"
    assert inspect(drv, not_before=t0, sid="b-5")["state"] == "failed"


def test_retained_and_garbage_messages_are_ignored(conveyor):
    class Msg:
        def __init__(self, payload, retain=False):
            self.topic, self.payload, self.retain = "autonomous/product/Transformation", payload, retain
    drv = conveyor()
    t0 = time.time()
    drv._on_message(None, None, Msg(json.dumps(OPTA_OK).encode(), retain=True))
    drv._on_message(None, None, Msg(b"\xff not json"))
    drv._on_message(None, None, Msg(b"[1, 2]"))
    assert inspect(drv, not_before=t0)["state"] == "failed"
    drv._on_message(None, None, Msg(json.dumps(OPTA_OK).encode()))
    assert inspect(drv, not_before=t0)["state"] == "done"


def test_without_not_before_a_recent_result_is_accepted(conveyor):
    drv = conveyor(lookback_s=10)
    drv.add_result(OPTA_OK, t=time.time() - 3)
    assert inspect(drv)["state"] == "done"


def test_cancel_while_waiting(conveyor):
    drv = conveyor(timeout_s=10)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    t0 = time.time()
    assert inspect(drv, cancel=cancel)["state"] == "cancelled"
    assert time.time() - t0 < 2


def test_state_reports_link_and_last_result(conveyor):
    drv = conveyor()
    st = drv.get_state()
    assert st["ready"] and "no conveyor result yet" in st["detail"]
    drv.add_result(OPTA_NG)
    assert "last result NG (cycle 6)" in drv.get_state()["detail"]
    drv.connected = False
    assert not drv.get_state()["ready"]


def test_topic_follows_the_run_mode_namespace_unless_told_not_to(conveyor, monkeypatch):
    monkeypatch.setattr(config, "TOPIC_PREFIX", "sim/")
    assert conveyor().result_topic == "sim/autonomous/product/Transformation"
    assert conveyor(namespaced=False).result_topic == "autonomous/product/Transformation"


# ============================================================== job flow (scheduler)
def make(site, registry, clock):
    mqtt = FakeMQTT()
    return TaskScheduler(registry, mqtt, site, clock=clock), mqtt


def ack(sched, mqtt, state="done", **extra):
    robot, sub, msg = mqtt.tasks[-1]
    base = {"subtask_id": msg["subtask_id"], "attempt": msg["attempt"]}
    sched.on_robot_status(robot, sub, {**base, "state": "accepted"})
    sched.on_robot_status(robot, sub, {**base, "state": state, **extra})
    return robot, sub, msg


def steps(mqtt):
    return [(m["action"], m["zone"], sub) for _, sub, m in mqtt.tasks]


def test_conveyor_decomposition_order(conveyor_site):
    subs = decompose(INSTRUCTION, conveyor_site)
    common = [(s["action"], s["zone"], s.get("handover")) for s in subs if s["branch"] is None]
    assert common == [("navigate", "manufacturing_1", None), ("pick", "manufacturing_1", None),
                      ("navigate", "quality_station", None), ("place", "quality_station", "load"),
                      ("quality_check", "quality_station", None), ("pick", "quality_station", "unload")]
    unload = subs[5]["subtask_id"]
    assert {s["depends_on"] for s in subs if s["action"] == "navigate" and s["branch"]} == {unload}
    assert [s["group"] for s in subs[:6]] == ["carrier"] * 4 + ["inspector", "carrier"]


@pytest.mark.parametrize("passed,dest", [(True, "package_area"), (False, "waste_area")])
def test_conveyor_job_end_to_end(conveyor_site, registry, clock, passed, dest):
    sched, mqtt = make(conveyor_site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    ack(sched, mqtt)                                   # navigate source
    ack(sched, mqtt)                                   # pick
    assert sched.get_task(tid)["product_on_board"]
    ack(sched, mqtt)                                   # navigate QC
    clock.advance(1)
    load_robot, _, load = mqtt.tasks[-1]
    assert (load["action"], load["handover"], load_robot) == ("place", "load", "robot_01")
    clock.advance(4)
    ack(sched, mqtt)                                   # part on the belt
    assert not sched.get_task(tid)["product_on_board"]
    qc_robot, _, qc = mqtt.tasks[-1]
    assert qc["action"] == "quality_check" and qc["not_before"] == load["dispatched_at"]
    assert registry.get("robot_01").status == "busy", "carrier stays claimed while the belt runs"
    ack(sched, mqtt, result={"pass": passed, "details": {"cycle": 5}})
    assert registry.get(qc_robot).status == "idle"
    _, _, unload = ack(sched, mqtt)
    assert (unload["action"], unload["handover"], unload["zone"]) == ("pick", "unload", "quality_station")
    assert sched.get_task(tid)["product_on_board"]
    _, _, nav = ack(sched, mqtt)
    assert (nav["action"], nav["zone"]) == ("navigate", dest)
    ack(sched, mqtt)                                   # final place
    assert sched.task_state(tid) == "completed"
    assert {r for r, _, m in mqtt.tasks if m["group"] == "carrier"} == {"robot_01"}
    assert all(r.status == "idle" for r in registry.all())
    qc_event = next(e for e in mqtt.events if e["event"] == "qc_result")
    assert qc_event["passed"] is passed and qc_event["details"] == {"cycle": 5}


def run_to_conveyor_qc(sched, mqtt):
    for _ in range(4):                                 # navigate, pick, navigate, place on belt
        ack(sched, mqtt)
    assert mqtt.tasks[-1][2]["action"] == "quality_check"


def test_conveyor_qc_failure_releases_empty_carrier(conveyor_site, registry, clock):
    sched, mqtt = make(conveyor_site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    run_to_conveyor_qc(sched, mqtt)
    stations = set()
    for _ in range(conveyor_site.retries_for("quality_check") + 1):
        stations.add(ack(sched, mqtt, state="failed", error="no conveyor result")[0])
    task = sched.get_task(tid)
    assert task["state"] == "failed" and "left on the QC station" in task["error"]
    assert registry.get("robot_01").status == "idle", "the part is on the conveyor, not on the robot"
    assert any(registry.get(q).status == "error" for q in stations)
    assert task["carrier_robot"] is None


def test_conveyor_qc_retry_keeps_not_before(conveyor_site, registry, clock):
    sched, mqtt = make(conveyor_site, registry, clock)
    sched.submit_instruction(INSTRUCTION)
    run_to_conveyor_qc(sched, mqtt)
    first = mqtt.tasks[-1][2]
    clock.advance(30)
    beat(registry, clock)
    ack(sched, mqtt, state="failed", error="no conveyor result")
    retry = mqtt.tasks[-1][2]
    assert retry["action"] == "quality_check" and retry["attempt"] == first["attempt"] + 1
    assert retry["not_before"] == first["not_before"]


def test_cancel_during_conveyor_qc_frees_the_carrier(conveyor_site, registry, clock):
    sched, mqtt = make(conveyor_site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    run_to_conveyor_qc(sched, mqtt)
    assert sched.handle_command({"cmd": "cancel_task", "task_id": tid})["ok"]
    assert registry.get("robot_01").status == "idle"


def test_failed_load_onto_belt_flags_the_robot(conveyor_site, registry, clock):
    sched, mqtt = make(conveyor_site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    for _ in range(3):
        ack(sched, mqtt)
    ack(sched, mqtt, state="failed", error="S1 blocked")          # place has 0 retries
    task = sched.get_task(tid)
    assert task["state"] == "failed" and task["product_on_board"] and "recover it" in task["error"]
    assert registry.get("robot_01").status == "error"


def exit_site():
    data = json.load(open("site.json"))
    data["locations"]["conveyor_exit"] = {"description": "Conveyor exit (S3)"}
    data["locations"]["quality_station"]["conveyor"] = {"exit_location": "conveyor_exit"}
    return SiteConfig.from_dict(data)


def test_conveyor_with_separate_exit_location(registry, clock):
    site = exit_site()
    assert site.job_locations(INSTRUCTION) == ["manufacturing_1", "quality_station", "conveyor_exit",
                                               "package_area", "waste_area"]
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    for _ in range(4):
        ack(sched, mqtt)
    ack(sched, mqtt, result={"pass": True})
    assert steps(mqtt)[-1] == ("navigate", "conveyor_exit", "amr")
    ack(sched, mqtt)
    assert steps(mqtt)[-1] == ("pick", "conveyor_exit", "arm")
    ack(sched, mqtt)
    ack(sched, mqtt)
    ack(sched, mqtt)
    assert sched.task_state(tid) == "completed"


def test_driving_to_the_exit_fails_task_instead_of_restarting_it(registry, clock):
    """The part is on the conveyor: re-running the job from the pick-up point on
    another robot would be wrong even though nothing is on board."""
    site = exit_site()
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    for _ in range(4):
        ack(sched, mqtt)
    ack(sched, mqtt, result={"pass": True})
    for _ in range(site.retries_for("navigate") + 1):
        ack(sched, mqtt, state="failed", error="path blocked")
    assert sched.task_state(tid) == "failed"
    assert "task_reassigned" not in mqtt.event_names()
    assert registry.get("robot_01").status == "error"


def test_exit_location_needs_coordinates_too(clock):
    site = exit_site()
    registry = healthy_registry(clock)
    for r in registry.all():
        if r.navigation_topics():
            registry.update_health(r.id, "amr", {"online": True, "ready": True, "battery": 90,
                                                 "locations": ["manufacturing_1", "quality_station",
                                                               "package_area", "waste_area"]}, now=clock())
    sched, mqtt = make(site, registry, clock)
    with pytest.raises(ValueError, match="conveyor_exit"):
        sched.submit_instruction(INSTRUCTION)
    assert mqtt.tasks == []


@pytest.mark.parametrize("conv,expected", [(True, {}), ({}, {}), (False, None), (None, None)])
def test_conveyor_flag_forms(conv, expected):
    data = json.load(open("site.json"))
    data["locations"]["quality_station"]["conveyor"] = conv
    assert SiteConfig.from_dict(data).conveyor_for("quality_station") == expected


@pytest.mark.parametrize("conv", [{"exit_location": "nowhere"}, {"exit_location": "quality_station"}, "yes"])
def test_bad_conveyor_config_rejected(conv):
    data = json.load(open("site.json"))
    data["locations"]["quality_station"]["conveyor"] = conv
    with pytest.raises(ValueError):
        SiteConfig.from_dict(data)


# ============================================================== shipped configs
def test_every_shipped_agent_config_uses_a_known_driver():
    from drivers import DRIVER_TYPES
    import glob
    paths = sorted(glob.glob("agents/*.json") + glob.glob("agents/examples/*.json"))
    assert paths
    for p in paths:
        with open(p) as f:
            cfg = json.load(f)
        assert cfg["driver"]["type"] in DRIVER_TYPES, p
        assert cfg.get("robot_id") and cfg.get("subsystem"), p


def test_dobot_projects_cover_every_pick_and_place_of_the_shipped_job():
    site = SiteConfig.load("site.json")
    with open("agents/robot_01_arm.json") as f:
        projects = json.load(f)["driver"]["projects"]
    for s in decompose(INSTRUCTION, site):
        if s["action"] in ("pick", "place"):
            assert s["zone"] in projects.get(s["action"], {}), f"no Dobot project for {s['action']} at {s['zone']}"


def test_conveyor_agent_config_matches_the_opta_sketch():
    with open("agents/qc_01_conveyor.json") as f:
        cfg = json.load(f)
    assert (cfg["robot_id"], cfg["subsystem"]) == ("qc_01", "scanner")
    drv = cfg["driver"]
    assert drv["result_topic"] == "autonomous/product/Transformation"     # MQTT_TOPIC in the sketch
    assert drv["pass_values"] == ["OK"] and drv["fail_values"] == ["NG"]  # the only values publishResult() sends


# ============================================================== over a real broker
from mqtt_fixtures import broker, wait_for  # noqa: E402,F401


def test_driver_reads_the_opta_messages_over_mqtt(broker):
    from mqtt_common import make_client
    pub = make_client("opta-conveyor", clean_session=True)
    pub.connect(broker.host, broker.port)
    pub.loop_start()
    topic = config.topic("autonomous/product/Transformation")
    # someone left a retained result on the topic: it arrives on subscribe, flagged retained, and is old
    pub.publish(topic, json.dumps(OPTA_OK), qos=0, retain=True).wait_for_publish(3)
    drv = create_driver({"type": "conveyor_inspection", "station": "qc_01", "timeout_s": 5,
                         "_mqtt_host": broker.host, "_mqtt_port": broker.port})
    try:
        assert wait_for(lambda: drv.connected, 5)
        time.sleep(0.3)
        t0 = time.time() - 60
        result = {}
        t = threading.Thread(target=lambda: result.update(inspect(drv, not_before=t0)))
        t.start()
        time.sleep(0.5)
        assert t.is_alive(), "a retained message is not a fresh verdict"
        pub.publish(topic, json.dumps(OPTA_NG), qos=0).wait_for_publish(3)      # the Opta publishes QoS 0
        t.join(5)
        assert result["state"] == "done" and result["result"]["pass"] is False
        assert result["result"]["details"]["raw"] == OPTA_NG["raw"]
        pub.publish(topic, b"", qos=0, retain=True).wait_for_publish(3)
    finally:
        pub.loop_stop()
        pub.disconnect()
        drv.close()


def test_conveyor_simulator_refuses_real_mode():
    env = dict(os.environ, RUN_MODE="real", MQTT_BROKER_HOST="127.0.0.1", MQTT_BROKER_PORT="1")
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "conveyor_sim.py")], env=env, cwd=ROOT,
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode != 0 and "only runs in TEST mode" in proc.stderr
