"""
robot_agent.py
--------------
Bridges one robot SUBSYSTEM (a MiR base, a UR arm, a QC camera) to the
scheduler over MQTT. Run one process per subsystem. With vendor REST/TCP
APIs it does not have to run on the robot itself -- any PC that can reach
both the broker and the robot controller works.

Topic contract (per robot_id / subsystem):
  robots/<id>/<sub>/task    (sub)  {"type":"task", subtask...} | {"type":"cancel", subtask_id, attempt}
  robots/<id>/<sub>/status  (pub)  {"subtask_id","attempt","state":"accepted"|"done"|"failed"|"cancelled", ...}
                                   terminal statuses are retained so a restarted scheduler still sees them
  robots/<id>/<sub>/state   (pub)  retained heartbeat {"online","ready","estop","battery","busy",...}
                                   + last-will {"online": false} if this process dies
  robots/<id>/+/state       (sub)  sibling heartbeats, for the arm/base software interlock

What this process guarantees:
  * the hardware action runs in a worker thread, so the MQTT connection
    stays alive and a cancel is processed while the robot is moving;
  * a task delivered twice (QoS 1) is executed once; the stored result is
    re-sent instead;
  * a stale task (delivered after its accept deadline, e.g. queued while
    the robot was off Wi-Fi and already failed over) is refused;
  * a subtask is refused while a sibling subsystem on the same robot is
    busy (arm won't start while the base is driving, and vice versa).
    This is a SOFTWARE interlock only -- the real one belongs in the robot
    controllers / safety PLC.

Run:
    python3 robot_agent.py --config agents/robot_01_amr.json
    python3 robot_agent.py --config agents/robot_01_amr.json --check            # print driver state
    python3 robot_agent.py --config agents/robot_01_amr.json --test navigate manufacturing_1
    python3 robot_agent.py --robot-id robot_01 --subsystem amr                    # simulator
"""

import argparse
import json
import logging
import os
import signal
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

import config
from drivers import create_driver
from drivers.base import Driver, failed
from mqtt_common import (STATE_TOPIC, STATUS_TOPIC, TASK_TOPIC, connected_ok, guarded, make_client,
                         parse_json, start, stop)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("robot_agent")

TERMINAL = ("done", "failed", "cancelled")
RESULT_CACHE = 500


class RobotAgent:
    def __init__(self, robot_id: str, subsystem: str, driver: Driver,
                 heartbeat_s: float = 5.0, interlock_with: Tuple[str, ...] = (),
                 clock_skew_tolerance_s: float = 5.0, client_id: Optional[str] = None,
                 require_travel_pose: Tuple[str, ...] = (),
                 host: Optional[str] = None, port: Optional[int] = None):
        self.robot_id, self.subsystem, self.driver = robot_id, subsystem, driver
        self.heartbeat_s = heartbeat_s
        self.interlock_with = tuple(interlock_with)
        # Siblings that must POSITIVELY report travel_pose=true before we act
        # (a base carrying an arm: "unknown" is treated as "not tucked in").
        self.require_travel_pose = tuple(require_travel_pose)
        unknown = set(self.require_travel_pose) - set(self.interlock_with)
        if unknown:
            raise ValueError(f"require_travel_pose {sorted(unknown)} must also be listed in interlock_with")
        self.skew = clock_skew_tolerance_s
        self.task_topic = TASK_TOPIC.format(robot_id=robot_id, subsystem=subsystem)
        self.status_topic = STATUS_TOPIC.format(robot_id=robot_id, subsystem=subsystem)
        self.state_topic = STATE_TOPIC.format(robot_id=robot_id, subsystem=subsystem)
        self._sibling_filter = STATE_TOPIC.format(robot_id=robot_id, subsystem="+")

        self._lock = threading.RLock()
        self._current: Optional[Dict[str, Any]] = None       # {"key","thread","cancel","subtask"}
        self._results: "OrderedDict[Tuple[str,int], dict]" = OrderedDict()
        self._siblings: Dict[str, Dict[str, Any]] = {}
        self._stop = threading.Event()
        self._driver_state: Dict[str, Any] = {}
        self._host, self._port = host, port

        self._client = make_client(client_id or f"agent_{robot_id}_{subsystem}", clean_session=False,
                                   will_topic=self.state_topic,
                                   will_payload={"online": False, "ready": False,
                                                 "detail": "agent connection lost (last will)"})
        self._client.on_connect = guarded(self._on_connect)
        self._client.on_message = guarded(self._on_message)

    # ---- MQTT ---------------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if not connected_ok(reason_code):
            logger.error("MQTT connect refused: %s", reason_code)
            return
        logger.info("Connected (session_present=%s); listening on %s", flags.session_present, self.task_topic)
        subs = [(self.task_topic, 1)]
        if self.interlock_with:
            subs.append((self._sibling_filter, 1))
        client.subscribe(subs)
        self._publish_heartbeat(refresh=False)

    def _on_message(self, client, userdata, msg):
        if msg.topic == self.task_topic:
            payload = parse_json(msg)
            if not isinstance(payload, dict):
                logger.error("Ignoring malformed task message: %r", msg.payload[:200])
                return
            if payload.get("type", "task") == "cancel":
                self._handle_cancel(payload)
            else:
                self._handle_task(payload)
            return
        parts = msg.topic.split("/")
        if len(parts) == 4 and parts[3] == "state" and parts[2] != self.subsystem:
            payload = parse_json(msg)
            if isinstance(payload, dict):
                self._siblings[parts[2]] = payload

    def _publish_status(self, payload: Dict[str, Any], retain: bool = False):
        self._client.publish(self.status_topic, json.dumps(payload), qos=1, retain=retain)
        logger.info("<- %s %s#%s %s %s", self.status_topic, payload.get("subtask_id"),
                    payload.get("attempt"), payload["state"], payload.get("error", ""))

    def _publish_heartbeat(self, refresh: bool = True):
        """refresh=False re-sends the last driver state with an updated busy
        flag -- used from the MQTT thread, where a slow vendor API call
        would stall the connection."""
        if refresh or not self._driver_state:
            try:
                self._driver_state = self.driver.get_state()
            except Exception as e:  # a driver bug must not kill the heartbeat
                self._driver_state = {"ready": False, "detail": f"get_state error: {e}",
                                      "estop": False, "battery": None}
        st = self._driver_state
        with self._lock:
            current = self._current["key"][0] if self._current else None
        payload = {"online": True, "ready": bool(st.get("ready")), "estop": bool(st.get("estop")),
                   "battery": st.get("battery"), "detail": st.get("detail", ""),
                   "busy": current is not None, "current": current, "ts": time.time()}
        if "travel_pose" in st:
            payload["travel_pose"] = st["travel_pose"]
        self._client.publish(self.state_topic, json.dumps(payload), qos=1, retain=True)

    # ---- task handling ---------------------------------------------------------------
    def _interlock_problem(self) -> Optional[str]:
        for sib in self.interlock_with:
            st = self._siblings.get(sib)
            if st is None:
                return f"interlock: no state received from sibling '{sib}'"
            if not st.get("online", False):
                return f"interlock: sibling '{sib}' is offline"
            if st.get("busy"):
                return f"interlock: sibling '{sib}' is busy with {st.get('current')}"
            if st.get("travel_pose") is False:
                return f"interlock: sibling '{sib}' is not in its travel pose"
            if sib in self.require_travel_pose and st.get("travel_pose") is not True:
                return (f"interlock: sibling '{sib}' does not report a travel pose -- set travel_joints "
                        f"in its agent config")
        return None

    def _handle_task(self, task: Dict[str, Any]):
        sid, action = task.get("subtask_id"), task.get("action")
        attempt = task.get("attempt", 1)
        if not isinstance(sid, str) or not isinstance(action, str):
            logger.error("Task without subtask_id/action: %r", task)
            return
        key = (sid, attempt)
        base = {"subtask_id": sid, "attempt": attempt}

        with self._lock:
            if key in self._results:  # duplicate delivery after we finished
                logger.info("Duplicate task %s#%s -- re-sending stored result", sid, attempt)
                self._publish_status(self._results[key], retain=True)
                return
            if self._current and self._current["key"] == key:  # duplicate while running
                self._publish_status({**base, "state": "accepted"})
                return
            reject = None
            if self._current:
                reject = f"busy with {self._current['key'][0]}"
            elif action not in self.driver.actions:
                reject = f"action {action!r} not supported by {self.subsystem} ({self.driver.actions})"
            else:
                expires = task.get("expires_at")
                if isinstance(expires, (int, float)) and time.time() > expires + self.skew:
                    reject = (f"stale task: arrived {time.time() - expires:.0f}s after its accept "
                              f"deadline (robot was offline, or clocks are not NTP-synced)")
                else:
                    reject = self._interlock_problem()
            if reject:
                result = {**base, **failed(reject)}
                self._remember(key, result)
                self._publish_status(result, retain=True)
                return
            cancel = threading.Event()
            worker = threading.Thread(target=self._run, args=(task, key, cancel),
                                      name=f"task-{sid}", daemon=True)
            self._current = {"key": key, "thread": worker, "cancel": cancel, "subtask": task}
            self._publish_heartbeat(refresh=False)  # siblings see busy=True before anything moves
            self._publish_status({**base, "state": "accepted"})
            worker.start()

    def _run(self, task: Dict[str, Any], key, cancel: threading.Event):
        sid, attempt = key
        logger.info("Executing %s#%s: %s at %s", sid, attempt, task["action"], task.get("zone"))
        try:
            outcome = self.driver.execute(task, cancel)
            if not isinstance(outcome, dict) or outcome.get("state") not in TERMINAL:
                outcome = failed(f"driver returned invalid outcome {outcome!r}")
        except Exception as e:
            logger.exception("Driver crashed on %s", sid)
            outcome = failed(f"driver exception: {e}")
        result = {"subtask_id": sid, "attempt": attempt, **outcome}
        with self._lock:
            self._remember(key, result)
            self._current = None
        # Fresh heartbeat (busy=False, current travel pose) goes out BEFORE the
        # result: the scheduler only dispatches the sibling's next step after
        # seeing the result, so the sibling's interlock already sees our final
        # state. (We're on the worker thread, so a slow get_state() is fine.)
        self._publish_heartbeat(refresh=True)
        self._publish_status(result, retain=True)

    def _handle_cancel(self, msg: Dict[str, Any]):
        key = (msg.get("subtask_id"), msg.get("attempt", 1))
        with self._lock:
            if self._current and self._current["key"] == key:
                logger.warning("Cancelling %s#%s: %s", key[0], key[1], msg.get("reason"))
                self._current["cancel"].set()
                cancel_hw = True
            else:
                cancel_hw = False
                if key not in self._results:
                    # Cancel for something we never started: remember it so a
                    # late-arriving copy of that task is refused.
                    self._remember(key, {"subtask_id": key[0], "attempt": key[1], "state": "cancelled",
                                         "error": f"cancelled before start: {msg.get('reason')}"})
        if cancel_hw:
            try:
                self.driver.cancel()
            except Exception:
                logger.exception("driver.cancel() failed")

    def _remember(self, key, result):
        self._results[key] = result
        while len(self._results) > RESULT_CACHE:
            self._results.popitem(last=False)

    # ---- lifecycle ---------------------------------------------------------------------
    def start(self):
        start(self._client, self._host, self._port)

    def run_forever(self):
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        self.start()
        try:
            while not self._stop.wait(self.heartbeat_s):
                self._publish_heartbeat()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self):
        self._stop.set()
        with self._lock:
            current = self._current
        if current:
            logger.warning("Shutting down while executing %s -- cancelling", current["key"][0])
            current["cancel"].set()
            try:
                self.driver.cancel()
            except Exception:
                pass
            current["thread"].join(timeout=10)
        self._client.publish(self.state_topic, json.dumps(
            {"online": False, "ready": False, "detail": "agent stopped", "ts": time.time()}),
            qos=1, retain=True)
        time.sleep(0.2)
        stop(self._client)
        self.driver.close()


def load_agent_config(path: str) -> Dict[str, Any]:
    with open(path, "r") as f:
        return json.load(f)


def main():
    p = argparse.ArgumentParser(description="Robot subsystem MQTT agent")
    p.add_argument("--config", help="agent config JSON (see agents/)")
    p.add_argument("--robot-id", help="override robot_id from config")
    p.add_argument("--subsystem", help="override subsystem from config")
    p.add_argument("--check", action="store_true", help="print driver state and exit (no MQTT)")
    p.add_argument("--test", nargs=2, metavar=("ACTION", "ZONE"),
                   help="run ONE action on the hardware and exit (no MQTT). The robot WILL move.")
    args = p.parse_args()

    if args.config:
        cfg = load_agent_config(args.config)
    else:
        cfg = {"driver": {"type": "sim"}}
        logger.warning("No --config given: using the SIMULATED driver")
    robot_id = args.robot_id or cfg.get("robot_id")
    subsystem = args.subsystem or cfg.get("subsystem")
    if not robot_id or not subsystem:
        p.error("robot_id and subsystem are required (in config or on the command line)")

    driver = create_driver(cfg["driver"])

    if args.check:
        report = driver.get_state()
        if hasattr(driver, "check_result_channel"):
            report["result_channel"] = driver.check_result_channel()
        print(json.dumps(report, indent=2))
        return
    if args.test:
        action, zone = args.test
        print(f"About to run {action} at {zone} on {robot_id}/{subsystem}. Ctrl+C cancels.")
        cancel = threading.Event()
        try:
            result = driver.execute({"subtask_id": "manual-test", "action": action, "zone": zone,
                                     "target": "TEST"}, cancel)
        except KeyboardInterrupt:
            cancel.set()
            driver.cancel()
            result = {"state": "cancelled"}
        print(json.dumps(result, indent=2, default=str))
        return

    agent = RobotAgent(robot_id, subsystem, driver,
                       heartbeat_s=float(cfg.get("heartbeat_s", 5)),
                       interlock_with=tuple(cfg.get("interlock_with", [])),
                       require_travel_pose=tuple(cfg.get("require_travel_pose", [])),
                       clock_skew_tolerance_s=float(cfg.get("clock_skew_tolerance_s", 5)))
    agent.run_forever()


if __name__ == "__main__":
    main()
