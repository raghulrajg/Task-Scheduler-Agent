"""Robot agent behaviour without a broker (publish is captured)."""
import json
import threading
import time

import pytest

from drivers.base import Driver, cancelled, done, failed, robot_state
from robot_agent import RobotAgent


class SlowDriver(Driver):
    actions = ("navigate",)

    def __init__(self, duration=0.3, raise_exc=False):
        super().__init__({})
        self.duration, self.raise_exc = duration, raise_exc
        self.runs = 0
        self.cancel_calls = 0

    def execute(self, subtask, cancel):
        self.runs += 1
        if self.raise_exc:
            raise RuntimeError("controller exploded")
        if cancel.wait(self.duration):
            return cancelled()
        return done()

    def cancel(self):
        self.cancel_calls += 1

    def get_state(self):
        return robot_state(True, "ok", battery=77.0)


def make_agent(driver, interlock=()):
    agent = RobotAgent("robot_01", "amr", driver, interlock_with=interlock, client_id="test_agent")
    published = []
    agent._client.publish = lambda topic, payload, qos=0, retain=False: published.append(
        (topic, json.loads(payload), retain))
    return agent, published


def statuses(published):
    return [(p["subtask_id"], p.get("attempt"), p["state"]) for t, p, _ in published if t.endswith("/status")]


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def task(sid="t-1", attempt=1, action="navigate", **kw):
    msg = {"type": "task", "subtask_id": sid, "attempt": attempt, "action": action, "zone": "z",
           "expires_at": time.time() + 30}
    msg.update(kw)
    return msg


def test_executes_once_and_resends_result_on_duplicate():
    drv = SlowDriver(0.05)
    agent, pub = make_agent(drv)
    agent._handle_task(task())
    agent._handle_task(task())                       # duplicate while running
    assert wait_for(lambda: ("t-1", 1, "done") in statuses(pub))
    agent._handle_task(task())                       # duplicate after finishing
    assert drv.runs == 1
    assert statuses(pub).count(("t-1", 1, "done")) == 2
    done_retained = [r for t, p, r in pub if t.endswith("/status") and p["state"] == "done"]
    assert all(done_retained)


def test_rejects_second_task_while_busy():
    drv = SlowDriver(0.3)
    agent, pub = make_agent(drv)
    agent._handle_task(task("a"))
    agent._handle_task(task("b"))
    assert ("b", 1, "failed") in statuses(pub)
    assert wait_for(lambda: ("a", 1, "done") in statuses(pub))


def test_rejects_stale_task():
    agent, pub = make_agent(SlowDriver())
    agent._handle_task(task(expires_at=time.time() - 120))
    st = [p for t, p, _ in pub if t.endswith("/status")][-1]
    assert st["state"] == "failed" and "stale" in st["error"]


def test_rejects_unsupported_action():
    agent, pub = make_agent(SlowDriver())
    agent._handle_task(task(action="pick"))
    assert statuses(pub)[-1][2] == "failed"


def test_interlock_blocks_when_sibling_busy_or_unknown():
    agent, pub = make_agent(SlowDriver(0.01), interlock=("arm",))
    agent._handle_task(task("a"))                                         # no sibling state yet
    assert statuses(pub)[-1] == ("a", 1, "failed")
    agent._siblings["arm"] = {"online": True, "busy": True, "current": "x-2"}
    agent._handle_task(task("b"))
    assert statuses(pub)[-1] == ("b", 1, "failed")
    agent._siblings["arm"] = {"online": True, "busy": False}
    agent._handle_task(task("c"))
    assert wait_for(lambda: ("c", 1, "done") in statuses(pub))


def test_cancel_while_running():
    drv = SlowDriver(5)
    agent, pub = make_agent(drv)
    agent._handle_task(task())
    t0 = time.time()
    agent._handle_cancel({"type": "cancel", "subtask_id": "t-1", "attempt": 1, "reason": "operator"})
    assert wait_for(lambda: ("t-1", 1, "cancelled") in statuses(pub), timeout=2)
    assert time.time() - t0 < 1.5 and drv.cancel_calls == 1


def test_cancel_before_task_arrives_blocks_late_task():
    drv = SlowDriver(0.01)
    agent, pub = make_agent(drv)
    agent._handle_cancel({"type": "cancel", "subtask_id": "t-9", "attempt": 1, "reason": "timeout"})
    agent._handle_task(task("t-9"))
    assert drv.runs == 0
    assert statuses(pub)[-1] == ("t-9", 1, "cancelled")


def test_new_attempt_runs_after_old_attempt_cancelled():
    drv = SlowDriver(0.05)
    agent, pub = make_agent(drv)
    agent._handle_task(task(attempt=1))
    agent._handle_cancel({"subtask_id": "t-1", "attempt": 1})
    assert wait_for(lambda: agent._current is None)
    agent._handle_task(task(attempt=2))
    assert wait_for(lambda: ("t-1", 2, "done") in statuses(pub))


def test_driver_exception_reported_as_failed():
    agent, pub = make_agent(SlowDriver(raise_exc=True))
    agent._handle_task(task())
    assert wait_for(lambda: ("t-1", 1, "failed") in statuses(pub))


def test_idle_heartbeat_published_before_result():
    agent, pub = make_agent(SlowDriver(0.02))
    agent._handle_task(task())
    assert wait_for(lambda: ("t-1", 1, "done") in statuses(pub))
    idx_done = next(i for i, (t, p, _) in enumerate(pub) if t.endswith("/status") and p["state"] == "done")
    before = [p for t, p, _ in pub[:idx_done] if t.endswith("/state")]
    assert before and before[-1]["busy"] is False
    busy_beats = [p for t, p, _ in pub if t.endswith("/state") and p["busy"]]
    assert busy_beats, "siblings must see busy=True while executing"
