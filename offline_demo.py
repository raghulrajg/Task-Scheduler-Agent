"""
offline_demo.py
---------------
Run the real scheduler against simulated robots, entirely in memory:
no MQTT broker, no robots, no network, no .env needed. Each scenario prints
what the scheduler does step by step and ends with PASS/FAIL, so this also
works as a quick self-test after you change something.

    python3 offline_demo.py                  # run every scenario
    python3 offline_demo.py --list           # list scenarios
    python3 offline_demo.py qc_fail pick_fail
    python3 offline_demo.py -q               # only the PASS/FAIL lines
    python3 offline_demo.py --jobs 10 --qc-pass-rate 0.7 --seed 1   # push N jobs through the fleet

Time is simulated, so timeout scenarios that would take minutes run instantly.
"""

import argparse
import os
import random
import sys
import tempfile
import threading
from typing import Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

from robot_registry import RobotRegistry  # noqa: E402
from scheduler import TaskScheduler  # noqa: E402
from validation import InstructionError, SiteConfig  # noqa: E402

JOB = {"product": "ABCD", "source": "manufacturing_1", "quality_check": "quality_station",
       "success_dest": "package_area", "fail_dest": "waste_area"}
QUIET = False


def say(text: str = ""):
    if not QUIET:
        print(text)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return 1_000_000.0 + self.t

    def advance(self, dt: float):
        self.t += dt


# A policy decides how a simulated robot answers one dispatched subtask:
#   ("done", result_dict)  |  ("failed", "reason")  |  None  (robot never answers)
Policy = Callable[[str, str, dict], Optional[Tuple[str, object]]]


def normal(qc_pass: bool = True) -> Policy:
    def policy(robot, sub, msg):
        if msg["action"] == "quality_check":
            return "done", {"pass": qc_pass}
        return "done", None
    return policy


ACTION_SECONDS = {"navigate": 20, "pick": 8, "place": 8, "quality_check": 5}


class Sim:
    """Scheduler + in-memory 'MQTT' + simulated robots answering per policy."""

    def __init__(self, policy: Policy, state_path: Optional[str] = None, clock: Optional[Clock] = None,
                 durations: Optional[Dict[str, float]] = None):
        self.clock = clock or Clock()
        self.durations = durations or ACTION_SECONDS
        self.site = SiteConfig.load("site.json")
        self.registry = RobotRegistry("robots_metadata.json", require_heartbeat=True,
                                      heartbeat_timeout_s=self.site.heartbeat_timeout_s,
                                      min_battery_percent=self.site.min_battery_percent, clock=self.clock)
        self.offline = set()          # robot ids whose agents are "down"
        self.policy = policy
        self.queue: List[Tuple[str, str, dict]] = []
        self.events: List[dict] = []
        self.cancelled = set()
        self.beat()
        self.sched = TaskScheduler(self.registry, self, self.site, state_path=state_path, clock=self.clock)

    # ---- the scheduler's "MQTT client" interface ----------------------------------
    def publish_task(self, robot_id, subsystem, msg):
        say(f"  t+{self.clock.t:5.0f}s  -> {robot_id}/{subsystem:8} {msg['action']:13} {msg['zone']:16}"
            f" (attempt {msg['attempt']})")
        self.queue.append((robot_id, subsystem, msg))

    def publish_cancel(self, robot_id, subsystem, subtask_id, attempt, reason):
        say(f"  t+{self.clock.t:5.0f}s  xx CANCEL {robot_id}/{subsystem} {subtask_id}#{attempt}: {reason}")
        self.cancelled.add((robot_id, subtask_id, attempt))

    def publish_event(self, event):
        self.events.append(event)
        e = event["event"]
        text = {
            "task_accepted": lambda: f"TASK {event['task_id']} accepted: {event['instruction']['product']}",
            "subtask_queued": lambda: ("job waiting for a free robot" if event["action"] == "navigate"
                                       else f"{event['action']} waiting for a free station"),
            "qc_result": lambda: f"QC says {'PASS -> success_dest' if event['passed'] else 'FAIL -> fail_dest'}",
            "subtask_retry": lambda: f"retry {event['action']} (attempt {event['attempt']}): {event['reason']}",
            "task_reassigned": lambda: f"nothing picked yet -> job moved off {event['from_robot']}",
            "task_completed": lambda: f"TASK {event['task_id']} COMPLETED (branch={event['branch']})",
            "task_failed": lambda: f"TASK {event['task_id']} FAILED: {event['error']}",
            "task_cancelled": lambda: f"TASK {event['task_id']} CANCELLED",
            "robot_reset": lambda: f"operator reset {event['robot_id']}",
        }.get(e)
        if text:
            say(f"  t+{self.clock.t:5.0f}s  ** {text()}")

    # ---- simulation ---------------------------------------------------------------------
    def beat(self):
        for r in self.registry.all():
            for topic in r.topics():
                online = r.id not in self.offline
                self.registry.update_health(r.id, topic, {"online": online, "ready": online, "battery": 90},
                                            now=self.clock())

    def run(self, max_sim_seconds: float = 3600):
        """Answer dispatched subtasks until nothing is left in flight."""
        start = self.clock.t
        while self.clock.t - start < max_sim_seconds:
            if self.queue:
                robot, sub, msg = self.queue.pop(0)
                if (robot, msg["subtask_id"], msg["attempt"]) in self.cancelled:
                    continue
                outcome = None if robot in self.offline else self.policy(robot, sub, msg)
                if outcome is None:
                    continue                                   # robot silent -> watchdog handles it
                base = {"subtask_id": msg["subtask_id"], "attempt": msg["attempt"]}
                self.sched.on_robot_status(robot, sub, {**base, "state": "accepted"})
                self.clock.advance(self.durations.get(msg["action"], 5))
                self.beat()
                state, extra = outcome
                payload = {**base, "state": state}
                if state == "done" and extra is not None:
                    payload["result"] = extra
                if state == "failed":
                    payload["error"] = extra
                say(f"  t+{self.clock.t:5.0f}s  <- {robot}/{sub:8} {state}"
                    + (f": {extra}" if state == "failed" else ""))
                self.sched.on_robot_status(robot, sub, payload)
            elif self.sched.in_flight() or self.sched.pending():
                self.clock.advance(1)
                self.beat()
                self.sched.tick()
            else:
                return

    def status(self, robot_id):
        return self.registry.get(robot_id).status

    def names(self):
        return [e["event"] for e in self.events]


# ============================================================================ scenarios
def sc_happy():
    """One job, QC passes, part goes to package_area."""
    sim = Sim(normal(qc_pass=True))
    tid = sim.sched.submit_instruction(JOB)
    sim.run()
    t = sim.sched.get_task(tid)
    return t["state"] == "completed" and t["branch"] == "success" and sim.status("robot_01") == "idle"


def sc_qc_fail():
    """QC rejects the part, the same robot carries it to waste_area."""
    sim = Sim(normal(qc_pass=False))
    tid = sim.sched.submit_instruction(JOB)
    sim.run()
    t = sim.sched.get_task(tid)
    return t["state"] == "completed" and t["branch"] == "fail"


def sc_pick_fail():
    """Gripper misses twice -> job fails, robot flagged 'error' until an operator resets it."""
    def policy(robot, sub, msg):
        if msg["action"] == "pick":
            return "failed", "no part detected in gripper"
        return normal()(robot, sub, msg)
    sim = Sim(policy)
    tid = sim.sched.submit_instruction(JOB)
    sim.run()
    ok = sim.sched.task_state(tid) == "failed" and sim.status("robot_01") == "error"
    say(f"  robot_01 status: {sim.status('robot_01')} ({sim.registry.get('robot_01').status_reason})")
    res = sim.sched.handle_command({"cmd": "reset_robot", "robot_id": "robot_01"})
    say(f"  operator: reset robot_01 -> {res['message']}")
    return ok and sim.status("robot_01") == "idle"


def sc_qc_no_result():
    """QC station answers 'done' but with no pass/fail -> NOT treated as a pass."""
    def policy(robot, sub, msg):
        if msg["action"] == "quality_check":
            return "done", {}
        return "done", None
    sim = Sim(policy)
    tid = sim.sched.submit_instruction(JOB)
    sim.run()
    t = sim.sched.get_task(tid)
    return t["state"] == "failed" and t["branch"] is None and sim.status("robot_01") == "error"


def sc_robot_silent():
    """robot_01's base never answers (Wi-Fi dead) -> timeouts, then the job moves to robot_02."""
    def policy(robot, sub, msg):
        if robot == "robot_01":
            return None
        return normal()(robot, sub, msg)
    sim = Sim(policy)
    tid = sim.sched.submit_instruction(JOB)
    sim.run()
    t = sim.sched.get_task(tid)
    return t["state"] == "completed" and "task_reassigned" in sim.names() and sim.status("robot_01") == "error"


def sc_offline_robot_skipped():
    """robot_01's agent is offline (no heartbeat) -> it is never given work."""
    sim = Sim(normal())
    sim.offline.add("robot_01")
    sim.beat()
    sim.sched.submit_instruction(JOB)
    first = sim.queue[0][0] if sim.queue else None
    sim.run()
    return first is not None and first != "robot_01"


def sc_busy_fleet():
    """6 jobs at once, 4 robots, 2 QC stations -> 2 jobs wait, all 6 finish, no robot double-booked."""
    sim = Sim(normal())
    ids = []
    lock = threading.Lock()

    def submit(i):
        tid = sim.sched.submit_instruction({**JOB, "product": f"P{i}"})
        with lock:
            ids.append(tid)
    threads = [threading.Thread(target=submit, args=(i,)) for i in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    first_robots = [r for r, _, m in sim.queue if m["action"] == "navigate"]
    queued = len(sim.sched.pending())
    say(f"  first dispatches went to {sorted(first_robots)}; {queued} job(s) waiting")
    sim.run()
    done = sum(sim.sched.task_state(t) == "completed" for t in ids)
    say(f"  {done}/6 jobs completed")
    return len(set(first_robots)) == len(first_robots) == 4 and queued == 2 and done == 6


def sc_invalid():
    """Bad instructions (e.g. from the LLM) are rejected before any robot moves."""
    sim = Sim(normal())
    bad = [
        {**JOB, "source": "manufactring_1"},
        {**JOB, "quality_check": "package_area"},
        {k: v for k, v in JOB.items() if k != "fail_dest"},
        ["not", "an", "object"],
    ]
    rejected = 0
    for instr in bad:
        try:
            sim.sched.submit_instruction(instr)
        except InstructionError as e:
            rejected += 1
            say(f"  rejected: {e}")
    return rejected == len(bad) and not sim.queue


def sc_cancel():
    """Operator cancels a job before the pick -> robot is released when it confirms."""
    sim = Sim(normal())
    tid = sim.sched.submit_instruction(JOB)
    robot, sub, msg = sim.queue.pop(0)
    sim.sched.on_robot_status(robot, sub, {"subtask_id": msg["subtask_id"], "attempt": 1, "state": "accepted"})
    res = sim.sched.handle_command({"cmd": "cancel_task", "task_id": tid})
    say(f"  operator: cancel {tid} -> {res['message']}")
    sim.sched.on_robot_status(robot, sub, {"subtask_id": msg["subtask_id"], "attempt": 1, "state": "cancelled"})
    say(f"  {robot} confirmed the cancel -> status {sim.status(robot)}")
    return sim.sched.task_state(tid) == "cancelled" and sim.status(robot) == "idle"


def sc_restart():
    """Scheduler process restarts mid-job (part on board) and finishes the job from saved state."""
    state = os.path.join(tempfile.mkdtemp(), "state.json")
    clock = Clock()
    sim1 = Sim(normal(), state_path=state, clock=clock)
    tid = sim1.sched.submit_instruction(JOB)
    for _ in range(2):                                   # navigate + pick, then "crash"
        robot, sub, msg = sim1.queue.pop(0)
        base = {"subtask_id": msg["subtask_id"], "attempt": msg["attempt"]}
        sim1.sched.on_robot_status(robot, sub, {**base, "state": "accepted"})
        sim1.sched.on_robot_status(robot, sub, {**base, "state": "done"})
    in_flight = sim1.queue.pop(0)                        # navigate to QC is on its way
    say("  ...scheduler process killed while the robot drives to QC with the part on board...")
    sim2 = Sim(normal(), state_path=state, clock=clock)
    say(f"  restarted: robot_01 status={sim2.status('robot_01')}, "
        f"part on board={sim2.sched.get_task(tid)['product_on_board']}")
    sim2.queue.append(in_flight)                         # the robot's result arrives after reconnect
    sim2.run()
    return sim2.sched.task_state(tid) == "completed"


def sc_drivers():
    """Real Dobot and web-AMR drivers against local fake controllers (localhost only)."""
    sys.path.insert(0, os.path.join(HERE, "tests"))
    from drivers import create_driver
    from fake_amr import FakeAMR
    from fake_dobot import FakeDobot

    amr = FakeAMR(drive_time=0.4, pickup_delay=0.1)
    arm = FakeDobot(run_time=0.3)
    try:
        os.environ.setdefault("DEMO_AMR_PW", "pw{1}")
        amr_drv = create_driver({
            "type": "http_amr", "base_url": amr.url,
            "login": {"method": "POST", "path": "/api/login",
                      "json": {"username": "admin", "password": "${DEMO_AMR_PW}"}, "token_path": "data.token"},
            "locations": {"manufacturing_1": {"id": "station_A"}},
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
            "projects": {"pick": {"manufacturing_1": "pick_mfg"}},
            "travel_joints": [0, -30, 120, 0, 90, 0], "poll_interval_s": 0.05})
        say(f"  AMR state : {amr_drv.get_state()}")
        r1 = amr_drv.execute({"subtask_id": "d1", "action": "navigate", "zone": "manufacturing_1"},
                             threading.Event())
        say(f"  navigate  : {r1}")
        say(f"  Dobot state: {arm_drv.get_state()}")
        r2 = arm_drv.execute({"subtask_id": "d2", "action": "pick", "zone": "manufacturing_1"},
                             threading.Event())
        say(f"  pick      : {r2}")
        say(f"  Dobot commands sent: {arm.commands[-6:]}")
        arm.outcome = 2
        r3 = arm_drv.execute({"subtask_id": "d3", "action": "pick", "zone": "manufacturing_1"},
                             threading.Event())
        say(f"  missed pick: {r3}")
        return r1["state"] == "done" and r2["state"] == "done" and r3["state"] == "failed"
    finally:
        amr.close()
        arm.close()


def fleet_run(jobs: int, speed: float, qc_pass_rate: float, seed: Optional[int]) -> bool:
    """Push N jobs through the simulated fleet at once and report each outcome."""
    rng = random.Random(seed)

    def policy(robot, sub, msg):
        if msg["action"] == "quality_check":
            return "done", {"pass": rng.random() < qc_pass_rate}
        return "done", None
    durations = {a: d / max(speed, 0.001) for a, d in ACTION_SECONDS.items()}
    sim = Sim(policy, durations=durations)
    say(f"\n=== fleet: {jobs} job(s), QC pass rate {qc_pass_rate:.0%}, seed {seed}")
    ids = [(f"P{i + 1:03d}", sim.sched.submit_instruction({**JOB, "product": f"P{i + 1:03d}"}))
           for i in range(jobs)]
    sim.run(max_sim_seconds=24 * 3600)
    print()
    ok = True
    for product, tid in ids:
        t = sim.sched.get_task(tid)
        if t["state"] == "completed" and t["branch"] == "success":
            print(f"  {product}: DONE -> good part to package_area")
        elif t["state"] == "completed":
            print(f"  {product}: DONE -> rejected part to waste_area")
        else:
            ok = False
            print(f"  {product}: {t['state'].upper()} {t.get('error') or ''}")
    print(f"\n{sum(1 for _, t in ids if sim.sched.task_state(t) == 'completed')}/{jobs} jobs finished "
          f"in {sim.clock.t:.0f} simulated seconds")
    return ok


SCENARIOS: Dict[str, Callable[[], bool]] = {
    "happy": sc_happy,
    "qc_fail": sc_qc_fail,
    "pick_fail": sc_pick_fail,
    "qc_no_result": sc_qc_no_result,
    "robot_silent": sc_robot_silent,
    "offline_robot": sc_offline_robot_skipped,
    "busy_fleet": sc_busy_fleet,
    "invalid": sc_invalid,
    "cancel": sc_cancel,
    "restart": sc_restart,
    "drivers": sc_drivers,
}


def main():
    global QUIET
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenarios", nargs="*", help="scenario names (default: all)")
    p.add_argument("--list", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    fleet = p.add_argument_group("fleet run (instead of the scenarios)")
    fleet.add_argument("--jobs", type=int, help="push this many jobs through the simulated fleet")
    fleet.add_argument("--speed", type=float, default=1.0, help="divide simulated action times by this")
    fleet.add_argument("--qc-pass-rate", type=float, default=0.8, help="probability a part passes QC")
    fleet.add_argument("--seed", type=int, help="random seed, for repeatable runs")
    args = p.parse_args()
    if args.list:
        for name, fn in SCENARIOS.items():
            print(f"  {name:14} {fn.__doc__.strip()}")
        return
    QUIET = args.quiet
    import logging
    logging.disable(logging.CRITICAL)   # the step-by-step print-out replaces the logs here

    if args.jobs:
        sys.exit(0 if fleet_run(args.jobs, args.speed, args.qc_pass_rate, args.seed) else 1)

    names = args.scenarios or list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        sys.exit(f"unknown scenario(s) {unknown}; use --list")
    results = {}
    for name in names:
        fn = SCENARIOS[name]
        say(f"\n=== {name}: {fn.__doc__.strip()}")
        try:
            results[name] = bool(fn())
        except Exception as e:  # show the error, keep going
            print(f"  !! {name} raised {type(e).__name__}: {e}")
            results[name] = False
        print(f"{'PASS' if results[name] else 'FAIL'}  {name}")
    failed = [n for n, ok in results.items() if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} scenarios passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
