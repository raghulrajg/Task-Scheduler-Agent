"""
Scheduler behaviour, including a regression test for every bug found in the
review of the original code. No broker needed.
"""
import threading

import pytest

from conftest import INSTRUCTION, FakeMQTT, beat, healthy_registry
from scheduler import TaskScheduler
from validation import InstructionError


def make(site, registry, clock, state_path=None):
    mqtt = FakeMQTT()
    return TaskScheduler(registry, mqtt, site, state_path=state_path, clock=clock), mqtt


def last(mqtt):
    return mqtt.tasks[-1]


def ack(sched, mqtt, state="done", idx=-1, accept=True, **extra):
    robot, sub, msg = mqtt.tasks[idx]
    base = {"subtask_id": msg["subtask_id"], "attempt": msg["attempt"]}
    if accept:
        sched.on_robot_status(robot, sub, {**base, "state": "accepted"})
    sched.on_robot_status(robot, sub, {**base, "state": state, **extra})
    return robot, sub, msg


def run_to_qc(sched, mqtt):
    for _ in range(3):  # navigate(source), pick, navigate(qc)
        ack(sched, mqtt)


# ---------------------------------------------------------------- happy paths
@pytest.mark.parametrize("passed,dest", [(True, "package_area"), (False, "waste_area")])
def test_full_task_both_branches(site, registry, clock, passed, dest):
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    run_to_qc(sched, mqtt)
    ack(sched, mqtt, result={"pass": passed})
    nav = ack(sched, mqtt)
    assert nav[2]["action"] == "navigate" and nav[2]["zone"] == dest
    place = ack(sched, mqtt)
    assert place[2]["action"] == "place"
    assert sched.task_state(tid) == "completed"
    carriers = {r for r, _, m in mqtt.tasks if m["group"] == "carrier"}
    assert len(carriers) == 1, "carrier changed mid-task"
    assert all(r.status == "idle" for r in registry.all())
    assert "task_completed" in mqtt.event_names()


def test_dispatch_goes_to_right_subsystems(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    sched.submit_instruction(INSTRUCTION)
    assert last(mqtt)[1] == "amr"
    ack(sched, mqtt)
    assert last(mqtt)[1] == "arm"
    ack(sched, mqtt)
    ack(sched, mqtt)
    assert last(mqtt)[0].startswith("qc_") and last(mqtt)[1] == "scanner"


# ---------------------------------------------------------------- review bugs
def test_bug_duplicate_done_does_not_double_dispatch(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    sched.submit_instruction(INSTRUCTION)
    robot, sub, msg = ack(sched, mqtt)
    sched.on_robot_status(robot, sub, {"subtask_id": msg["subtask_id"], "attempt": 1, "state": "done"})
    assert sum(1 for _, _, m in mqtt.tasks if m["action"] == "pick") == 1


def test_bug_ack_from_wrong_robot_ignored(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    _, _, msg = last(mqtt)
    sched.on_robot_status("robot_04", "amr", {"subtask_id": msg["subtask_id"], "attempt": 1, "state": "done"})
    assert sched.get_task(tid)["completed"] == []
    assert len(mqtt.tasks) == 1


def test_bug_missing_qc_result_is_not_a_pass(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    run_to_qc(sched, mqtt)
    for _ in range(site.retries_for("quality_check") + 1):
        ack(sched, mqtt, result={})                     # no 'pass'
    task = sched.get_task(tid)
    assert task["branch"] is None
    assert task["state"] == "failed"
    assert not any(m["action"] == "place" for _, _, m in mqtt.tasks)
    assert registry.get(task["carrier_robot"]).status == "error"  # still holding the part


def test_bug_qc_string_false_is_not_a_pass(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    run_to_qc(sched, mqtt)
    ack(sched, mqtt, result={"pass": "false"})
    assert sched.get_task(tid)["branch"] is None


def test_bug_pick_failure_does_not_leave_task_in_limbo(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    ack(sched, mqtt)                                   # navigate ok
    for _ in range(site.retries_for("pick") + 1):
        ack(sched, mqtt, state="failed", error="no part detected")
    task = sched.get_task(tid)
    assert task["state"] == "failed"
    assert registry.get("robot_01").status == "error"
    assert "task_failed" in mqtt.event_names()
    # operator recovers the robot
    assert sched.handle_command({"cmd": "reset_robot", "robot_id": "robot_01"})["ok"]
    assert registry.get("robot_01").status == "idle"


def test_bug_qc_station_failure_flags_carrier_and_station(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    run_to_qc(sched, mqtt)
    qc_robots = set()
    for _ in range(site.retries_for("quality_check") + 1):
        qc_robots.add(ack(sched, mqtt, state="failed", error="camera timeout")[0])
    task = sched.get_task(tid)
    assert task["state"] == "failed"
    assert registry.get("robot_01").status == "error"
    assert any(registry.get(q).status == "error" for q in qc_robots)


def test_bug_unknown_location_rejected_before_dispatch(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    with pytest.raises(InstructionError):
        sched.submit_instruction({**INSTRUCTION, "source": "manufactring_1"})
    assert mqtt.tasks == []


def test_bug_qc_zone_without_station_rejected(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    with pytest.raises(InstructionError, match="no QC station"):
        sched.submit_instruction({**INSTRUCTION, "quality_check": "package_area"})
    assert mqtt.tasks == []


@pytest.mark.parametrize("bad", [["oops"], "text", None, 42, {"product": 5}])
def test_bug_malformed_instruction_raises_cleanly(site, registry, clock, bad):
    sched, _ = make(site, registry, clock)
    with pytest.raises(InstructionError):
        sched.submit_instruction(bad)


@pytest.mark.parametrize("bad", [["x"], "x", None, {"state": "done"}, {"subtask_id": 1, "state": "done"}])
def test_bug_malformed_status_ignored(site, registry, clock, bad):
    sched, mqtt = make(site, registry, clock)
    sched.submit_instruction(INSTRUCTION)
    sched.on_robot_status("robot_01", "amr", bad)
    assert len(mqtt.tasks) == 1


def test_bug_running_state_is_not_a_failure(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    robot, sub, msg = last(mqtt)
    sched.on_robot_status(robot, sub, {"subtask_id": msg["subtask_id"], "attempt": 1, "state": "running"})
    assert sched.get_task(tid)["state"] == "active"
    assert msg["subtask_id"] in sched.in_flight()


def test_bug_finished_tasks_are_pruned(site, registry, clock):
    site.finished_task_retention = 5
    sched, mqtt = make(site, registry, clock)
    for _ in range(12):
        sched.submit_instruction(INSTRUCTION)
        run_to_qc(sched, mqtt)
        ack(sched, mqtt, result={"pass": True})
        ack(sched, mqtt)
        ack(sched, mqtt)
    sched.tick()
    assert len(sched.snapshot()["tasks"]) == 5


# ---------------------------------------------------------------- timeouts & retries
def test_no_accept_times_out_and_retries(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    clock.advance(site.timeout_for("accept") + 1)
    beat(registry, clock)
    sched.tick()
    assert mqtt.cancels and mqtt.cancels[0][3] == 1
    robot, _, msg = last(mqtt)
    assert msg["attempt"] == 2 and robot == "robot_01"
    assert sched.get_task(tid)["state"] == "active"


def test_action_timeout_cancels_then_escalates(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    ack(sched, mqtt)  # navigate done
    robot, sub, msg = last(mqtt)  # pick
    for attempt in range(1, site.retries_for("pick") + 2):
        robot, sub, msg = last(mqtt)
        sched.on_robot_status(robot, sub, {"subtask_id": msg["subtask_id"], "attempt": attempt,
                                           "state": "accepted"})
        clock.advance(site.timeout_for("pick") + 1)
        sched.tick()
    assert sched.get_task(tid)["state"] == "failed"
    assert registry.get("robot_01").status == "error"
    assert len(mqtt.cancels) == site.retries_for("pick") + 1


def test_late_done_from_timed_out_attempt_ignored(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    sched.submit_instruction(INSTRUCTION)
    robot, sub, msg = last(mqtt)
    clock.advance(site.timeout_for("accept") + 1)
    beat(registry, clock)
    sched.tick()                                      # attempt 1 expired -> attempt 2 sent
    n = len(mqtt.tasks)
    sched.on_robot_status(robot, sub, {"subtask_id": msg["subtask_id"], "attempt": 1, "state": "done"})
    assert len(mqtt.tasks) == n, "late ack of old attempt must not advance the task"


def test_navigate_failure_before_pick_reassigns_to_other_robot(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    for _ in range(site.retries_for("navigate") + 1):
        ack(sched, mqtt, state="failed", error="path blocked")
    assert registry.get("robot_01").status == "error"
    robot, _, msg = last(mqtt)
    assert robot != "robot_01" and msg["action"] == "navigate"
    assert sched.get_task(tid)["state"] == "active"
    assert "task_reassigned" in mqtt.event_names()


def test_navigate_failure_with_product_on_board_fails_task(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    ack(sched, mqtt)
    ack(sched, mqtt)  # pick done -> product on board
    for _ in range(site.retries_for("navigate") + 1):
        ack(sched, mqtt, state="failed", error="localization lost")
    task = sched.get_task(tid)
    assert task["state"] == "failed" and task["product_on_board"]
    assert "recover it" in task["error"]
    assert registry.get("robot_01").status == "error"


# ---------------------------------------------------------------- health gating
def test_offline_robot_not_allocated(site, clock):
    registry = healthy_registry(clock)
    registry.update_health("robot_01", "arm", {"online": False}, now=clock())
    sched, mqtt = make(site, registry, clock)
    sched.submit_instruction(INSTRUCTION)
    assert last(mqtt)[0] != "robot_01"


def test_stale_heartbeat_low_battery_and_estop_not_allocated(site, clock):
    registry = healthy_registry(clock)
    clock.advance(site.heartbeat_timeout_s + 1)
    beat(registry, clock, {("robot_02", "amr"): {"battery": 10},
                              ("robot_03", "arm"): {"estop": True},
                              ("robot_04", "amr"): {"ready": False}})
    registry.update_health("robot_01", "amr", {"online": True, "ready": True}, now=clock() - 60)
    sched, mqtt = make(site, registry, clock)
    sched.submit_instruction(INSTRUCTION)
    assert mqtt.tasks == [] and len(sched.pending()) == 1
    # robot_02 gets charged -> heartbeat change drains the queue
    sched.on_robot_heartbeat("robot_02", "amr", {"online": True, "ready": True, "battery": 80})
    assert last(mqtt)[0] == "robot_02"


def test_maintenance_robot_skipped(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    assert sched.handle_command({"cmd": "set_maintenance", "robot_id": "robot_01", "on": True})["ok"]
    sched.submit_instruction(INSTRUCTION)
    assert last(mqtt)[0] != "robot_01"


# ---------------------------------------------------------------- operator commands
def test_cancel_task_before_pick_frees_robot_on_confirmation(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    robot, sub, msg = last(mqtt)
    assert sched.handle_command({"cmd": "cancel_task", "task_id": tid})["ok"]
    assert mqtt.cancels[-1][2] == msg["subtask_id"]
    assert registry.get(robot).status == "busy"       # until the agent confirms
    sched.on_robot_status(robot, sub, {"subtask_id": msg["subtask_id"], "attempt": 1, "state": "cancelled"})
    assert registry.get(robot).status == "idle"
    assert sched.task_state(tid) == "cancelled"


def test_cancel_task_with_product_on_board_flags_robot(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    tid = sched.submit_instruction(INSTRUCTION)
    ack(sched, mqtt)
    ack(sched, mqtt)                                   # picked
    robot, sub, msg = last(mqtt)
    sched.handle_command({"cmd": "cancel_task", "task_id": tid})
    sched.on_robot_status(robot, sub, {"subtask_id": msg["subtask_id"], "attempt": 1, "state": "cancelled"})
    assert registry.get("robot_01").status == "error"


def test_reset_refused_while_robot_in_use(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    sched.submit_instruction(INSTRUCTION)
    res = sched.handle_command({"cmd": "reset_robot", "robot_id": "robot_01"})
    assert not res["ok"]


@pytest.mark.parametrize("cmd", [None, [], {"cmd": "nope"}, {"cmd": "cancel_task", "task_id": "x"}])
def test_bad_commands_rejected(site, registry, clock, cmd):
    sched, _ = make(site, registry, clock)
    assert not sched.handle_command(cmd)["ok"]


# ---------------------------------------------------------------- persistence
def test_restart_restores_state_and_finishes_task(site, clock, tmp_path):
    path = str(tmp_path / "state.json")
    reg1 = healthy_registry(clock)
    s1, m1 = make(site, reg1, clock, state_path=path)
    tid = s1.submit_instruction(INSTRUCTION)
    ack(s1, m1)
    ack(s1, m1)                                         # picked; navigate to QC in flight
    robot, sub, msg = m1.tasks[-1]
    s1.on_robot_status(robot, sub, {"subtask_id": msg["subtask_id"], "attempt": 1, "state": "accepted"})

    # scheduler process restarts
    reg2 = healthy_registry(clock)
    s2, m2 = make(site, reg2, clock, state_path=path)
    assert reg2.get("robot_01").status == "busy"
    assert s2.get_task(tid)["product_on_board"]
    # the robot's (retained / queued) result arrives after reconnect
    s2.on_robot_status(robot, sub, {"subtask_id": msg["subtask_id"], "attempt": 1, "state": "done"})
    assert m2.tasks[-1][2]["action"] == "quality_check"  # dispatched by the new instance


def test_restart_gives_grace_before_timing_out(site, clock, tmp_path):
    path = str(tmp_path / "state.json")
    s1, m1 = make(site, healthy_registry(clock), clock, state_path=path)
    s1.submit_instruction(INSTRUCTION)
    clock.advance(site.timeout_for("accept") + 5)      # deadline passed while "down"
    s2, m2 = make(site, healthy_registry(clock), clock, state_path=path)
    s2.tick()
    assert m2.cancels == [], "must wait startup_grace_s before expiring"
    clock.advance(site.startup_grace_s + 1)
    beat(s2.registry, clock)
    s2.tick()
    assert m2.cancels


# ---------------------------------------------------------------- concurrency
def test_concurrent_submissions_never_double_claim(site, registry, clock):
    sched, mqtt = make(site, registry, clock)
    threads = [threading.Thread(target=sched.submit_instruction, args=({**INSTRUCTION, "product": f"P{i}"},))
               for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    robots = [r for r, _, _ in mqtt.tasks]
    assert len(robots) == 4 and len(set(robots)) == 4
    assert len(sched.pending()) == 2
    # finishing one task lets a queued one start on the freed robot
    tid0 = mqtt.tasks[0][2]["task_id"]

    def step(task_id, **kw):
        r, s, m = [x for x in mqtt.tasks if x[2]["task_id"] == task_id][-1]
        sched.on_robot_status(r, s, {"subtask_id": m["subtask_id"], "attempt": m["attempt"], "state": "done", **kw})
    for _ in range(3):
        step(tid0)
    step(tid0, result={"pass": True})
    step(tid0)
    step(tid0)
    assert sched.task_state(tid0) == "completed"
    assert len(sched.pending()) == 1
