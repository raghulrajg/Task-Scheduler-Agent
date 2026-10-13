"""
drivers/conveyor.py
-------------------
Quality check on a CONVEYOR with a camera: the conveyor agent
(github.com/raghulrajg/conveyor_agent -- Arduino Opta + Hikrobot SC3000)
runs the part from the entry sensor S1 to the camera at S2, reads the
camera's x;y;theta;result and publishes one MQTT message per part:

    autonomous/product/Transformation
    {"x":12.500,"y":30.200,"theta":45.000,"result":"OK","valid":true,"cycle":5,"raw":"12.5;30.2;45.0;OK;"}
    {"x":null,"y":null,"theta":null,"result":"NG","valid":false,"cycle":7,"raw":"garbage"}

The Opta does not take requests and does not echo a request id: it simply
publishes a verdict whenever a part passes the camera. So this driver
correlates by TIME. Before the inspection, the carrier robot placed the part
on the belt (the scheduler's "load" step); the scheduler sends the QC step
with not_before = when that load step was dispatched. The driver takes the
FIRST conveyor result received at or after not_before that no earlier
inspection has used. Results are buffered as they arrive, so a verdict that
comes in before the QC step is dispatched (fast belt, slow arm retract) is
not lost.

The Opta publishes the verdict while the part is still at the camera (S2)
and only then runs it to the exit S3. The driver therefore reports the
verdict only exit_travel_s after it arrived, so the unload pick that the
scheduler dispatches next finds the part at S3. (The pick_conveyor_exit
project should still wait for the part, e.g. on S3 wired to a Dobot DI.)

Fail-safe rules:
  * "valid": false (the camera sent something that is not x;y;theta;result)
    is a FAIL verdict by default (invalid_is_fail): the part goes to the
    fail destination, never to packaging. The part has already left the
    camera, so re-inspecting it is not possible.
  * a verdict outside pass_values / fail_values is a failed inspection.
  * no result within timeout_s is a failed inspection (the scheduler retries,
    then escalates).
  * retained messages are ignored (they are old by definition).
  * a verdict that may belong to ANOTHER part fails the inspection instead
    of routing this part ("ambiguous"): when an earlier inspection ended
    without its verdict (timeout, cancel: e.g. the Opta was offline and
    keeps that verdict in its send queue) and that verdict has not turned
    up yet, or when more than one result arrives for one part. A retry of
    an ambiguous inspection fails too, so a human checks the part.
  * if the Opta adds "age_ms" (time since the inspection) to its message,
    results are matched by inspection time instead of arrival time, so a
    verdict flushed late from its offline queue is never taken for a new part.
  * an operator's reset_robot for this station (scheduler/events
    "robot_reset", sent after a person cleared the belt and checked that the
    Opta's send queue is empty) forgets missing and buffered verdicts.

Config (agents/qc_01_conveyor.json):
{
  "type": "conveyor_inspection",
  "station": "qc_01",
  "result_topic": "autonomous/product/Transformation",   # MQTT_TOPIC in the Opta sketch
  "namespaced": true,        # true: topic gets the run mode prefix ("sim/..." in TEST mode)
  "verdict_field": "result",
  "pass_values": ["OK"],
  "fail_values": ["NG"],
  "invalid_is_fail": true,
  "timeout_s": 60,           # from QC dispatch; the Opta itself faults after 15 s travel + 10 s camera
  "exit_travel_s": 8,        # measured S2 -> S3 travel + margin: wait this long after the verdict
  "lookback_s": 30,          # used only when the QC step has no not_before
  "clock_skew_s": 2,         # tolerance on not_before (scheduler and agent clocks, NTP)
  "announce": true           # also publish qc/<station>/request (for dashboards and tools/conveyor_sim.py)
}
Keep timeout_s + exit_travel_s below site.json timeouts_s.quality_check (90).
"""

import json
import logging
import threading
import time
import uuid
from collections import OrderedDict, deque
from typing import Any, Deque, Dict, Optional

from drivers.base import Driver, cancelled, done, failed, robot_state
from drivers.util import dig, matches

logger = logging.getLogger("driver.conveyor")

DEFAULT_TOPIC = "autonomous/product/Transformation"
BUFFER_S = 600          # forget results older than this
BUFFER_MAX = 200
DUPLICATE_S = 5.0       # same cycle + raw within this window = a duplicate delivery
MEMORY = 100            # missed / ambiguous inspections remembered


class ConveyorInspectionDriver(Driver):
    actions = ("quality_check",)

    def __init__(self, cfg: Dict[str, Any], clock=time.time):
        super().__init__(cfg)
        import config
        from mqtt_common import connected_ok, guarded, make_client, start
        self.station = cfg.get("station") or cfg.get("_robot_id") or "qc_01"
        self.robot_id = cfg.get("_robot_id") or self.station
        self.event_topic = config.topic(config.EVENT_TOPIC)
        raw_topic = cfg.get("result_topic") or DEFAULT_TOPIC
        self.result_topic = config.topic(raw_topic) if cfg.get("namespaced", True) else raw_topic
        self.request_topic = config.topic(f"qc/{self.station}/request") if cfg.get("announce", True) else None
        self._field = cfg.get("verdict_field", "result")
        self._pass_values = cfg.get("pass_values", ["OK"])
        self._fail_values = cfg.get("fail_values", ["NG"])
        self._invalid_is_fail = bool(cfg.get("invalid_is_fail", True))
        self._timeout = float(cfg.get("timeout_s", 60))
        self._lookback = float(cfg.get("lookback_s", 30))
        self._skew = float(cfg.get("clock_skew_s", 2))
        self._exit_travel = float(cfg.get("exit_travel_s", 0))
        self._clock = clock

        self._cond = threading.Condition()
        # {"seq", "t" (inspection time: arrival - age_ms), "arrived", "payload", "used"}
        self._results: Deque[Dict[str, Any]] = deque()
        self._owed: "OrderedDict[str, float]" = OrderedDict()     # subtask_id -> since; ended without a verdict
        self._ambiguous: "OrderedDict[str, bool]" = OrderedDict()  # subtask_ids failed as ambiguous
        self._seq = 0
        self._last: Optional[Dict[str, Any]] = None
        self.connected = False
        self._connected_ok = connected_ok
        self._created = time.time()

        self._client = make_client(f"conveyor_{self.station}_{uuid.uuid4().hex[:6]}", clean_session=True)
        self._client.on_connect = guarded(self._on_connect)
        self._client.on_disconnect = guarded(self._on_disconnect)
        self._client.on_message = guarded(self._on_message)
        start(self._client, cfg.get("_mqtt_host"), cfg.get("_mqtt_port"))

    # ---- MQTT ------------------------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if not self._connected_ok(reason_code):
            logger.error("conveyor link: MQTT connect refused: %s", reason_code)
            return
        self.connected = True
        client.subscribe([(self.result_topic, 1), (self.event_topic, 1)])
        logger.info("listening for conveyor results on %s", self.result_topic)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        if msg.topic == self.event_topic:
            try:
                event = json.loads(msg.payload.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return
            if isinstance(event, dict) and event.get("event") == "robot_reset" \
                    and event.get("robot_id") == self.robot_id:
                self.reset()
            return
        if msg.retain:
            logger.info("ignoring retained (old) message on %s", msg.topic)
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            logger.warning("non-JSON conveyor message on %s ignored: %r", msg.topic, msg.payload[:120])
            return
        if not isinstance(payload, dict):
            return
        self.add_result(payload)

    def add_result(self, payload: Dict[str, Any], t: Optional[float] = None) -> bool:
        """Store one conveyor result (t = arrival time). Returns False for a duplicate delivery."""
        arrived = self._clock() if t is None else t
        age = payload.get("age_ms")
        inspected = arrived - age / 1000.0 if isinstance(age, (int, float)) and not isinstance(age, bool) \
            and age >= 0 else arrived
        with self._cond:
            key = (payload.get("cycle"), payload.get("raw"))
            for r in reversed(self._results):
                if arrived - r["arrived"] > DUPLICATE_S:
                    break
                if key != (None, None) and (r["payload"].get("cycle"), r["payload"].get("raw")) == key:
                    return False
            self._seq += 1
            entry = {"seq": self._seq, "t": inspected, "arrived": arrived, "payload": payload, "used": False}
            self._results.append(entry)
            self._last = entry
            while self._results and (len(self._results) > BUFFER_MAX
                                     or arrived - self._results[0]["arrived"] > BUFFER_S):
                self._results.popleft()
            self._cond.notify_all()
        logger.info("conveyor result #%d: %s", entry["seq"], payload)
        return True

    def reset(self) -> None:
        """The operator cleared the belt and reset the station: forget verdicts
        still owed or buffered, so the next part starts clean."""
        with self._cond:
            for r in self._results:
                r["used"] = True
            dropped = list(self._owed)
            self._owed.clear()
            self._ambiguous.clear()
        logger.warning("station reset by the operator: buffered results discarded%s",
                       f", missing verdicts for {dropped} forgotten" if dropped else "")

    def _take(self, since: float) -> Optional[Dict[str, Any]]:
        """First unused result inspected at or after `since` (self._cond held)."""
        for r in self._results:
            if not r["used"] and r["t"] >= since:
                r["used"] = True
                return r
        return None

    def _unused_since(self, since: float) -> int:
        return sum(1 for r in self._results if not r["used"] and r["t"] >= since)

    def _settle_debts(self, since: float) -> None:
        """A result from BEFORE this part was loaded, but after an earlier part
        whose inspection ended without a verdict was loaded, is that earlier
        part's late verdict: it pays the debt (self._cond held)."""
        for r in self._results:
            if not self._owed:
                return
            if r["used"] or r["t"] >= since:
                continue
            for owed_sid, owed_since in self._owed.items():
                if r["t"] >= owed_since:
                    r["used"] = True
                    del self._owed[owed_sid]
                    logger.warning("conveyor result #%d is the late verdict of %s (not used)", r["seq"], owed_sid)
                    break

    def _remember(self, store: "OrderedDict", key: str, value) -> None:
        store[key] = value
        while len(store) > MEMORY:
            store.popitem(last=False)

    def _ambiguous_result(self, sid: str, since: float, why: str) -> Dict[str, Any]:
        """Fail closed: discard every candidate verdict (self._cond held)."""
        for r in self._results:
            if not r["used"] and r["t"] >= since:
                r["used"] = True
        self._owed.clear()
        self._remember(self._ambiguous, sid, True)
        logger.error("QC %s: ambiguous conveyor verdict: %s", sid, why)
        return failed(f"ambiguous conveyor verdict ({why}): check this part by hand; it is not routed "
                      f"on a verdict that may belong to another part")

    def _startup_wait(self, window: float = 5.0) -> None:
        end = self._created + window
        while time.time() < end and not self.connected:
            time.sleep(0.05)

    # ---- Driver API ---------------------------------------------------------------------------
    def execute(self, subtask: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
        self._startup_wait()
        sid = subtask["subtask_id"]
        start = self._clock()
        not_before = subtask.get("not_before")
        since = (float(not_before) if isinstance(not_before, (int, float)) else start - self._lookback) - self._skew
        rid = f"{subtask['subtask_id']}#{subtask.get('attempt', 1)}"
        if self.request_topic and self.connected:
            request = {"request_id": rid, "subtask_id": subtask["subtask_id"], "product": subtask.get("target"),
                       "station": subtask.get("zone"), "not_before": since, "ts": start}
            self._client.publish(self.request_topic, json.dumps(request), qos=1)
        logger.info("QC %s: waiting for a conveyor result on %s received after %s", rid, self.result_topic,
                    time.strftime("%H:%M:%S", time.localtime(since)))

        deadline = start + self._timeout
        with self._cond:
            if sid in self._ambiguous:
                return failed("an earlier attempt for this part got an ambiguous conveyor verdict: check the part "
                              "by hand")
            self._owed.pop(sid, None)        # a retry for the same part: its verdict is the one we wait for
            while True:
                self._settle_debts(since)
                entry = self._take(since)
                if entry is not None:
                    if self._owed:
                        return self._ambiguous_result(
                            sid, since, f"the inspection of {next(iter(self._owed))} never got its verdict "
                                        f"(Opta offline or faulted?), this result may be that part's")
                    break
                if cancel.is_set():
                    self._remember(self._owed, sid, since)
                    return cancelled()
                if self._clock() > deadline:
                    self._remember(self._owed, sid, since)
                    why = "" if self.connected else " (not connected to the MQTT broker)"
                    return failed(f"no conveyor result on {self.result_topic} within {self._timeout:.0f}s{why}: "
                                  f"is the part on the belt, the conveyor in fault, or the Opta offline?")
                self._cond.wait(0.2)

        # The verdict comes while the part is still at the camera: report it once
        # the part can be at the exit, and treat a second result meanwhile as ambiguous.
        ready_at = entry["arrived"] + self._exit_travel
        while True:
            with self._cond:
                if self._unused_since(since):
                    return self._ambiguous_result(sid, since, "more than one conveyor result for one part")
            if cancel.is_set():
                with self._cond:               # a retry of this part may still use the verdict
                    entry["used"] = False
                    self._cond.notify_all()
                return cancelled()
            remaining = ready_at - self._clock()
            if remaining <= 0:
                break
            cancel.wait(min(0.2, remaining))
        return self._verdict(entry, start)

    def _verdict(self, entry: Dict[str, Any], start: float) -> Dict[str, Any]:
        p = entry["payload"]
        details = {k: p.get(k) for k in ("x", "y", "theta", "result", "valid", "cycle", "raw")}
        details["received_at"] = entry["arrived"]
        details["waited_s"] = round(max(0.0, entry["arrived"] - start), 2)
        if p.get("valid") is False:
            if self._invalid_is_fail:
                details["reason"] = "camera message was not x;y;theta;result -- treated as NG"
                return done({"pass": False, "details": details})
            return failed(f"conveyor reported an invalid camera message: {p.get('raw')!r}")
        verdict = dig(p, self._field)
        if matches(verdict, self._pass_values):
            return done({"pass": True, "details": details})
        if matches(verdict, self._fail_values):
            return done({"pass": False, "details": details})
        return failed(f"conveyor result has no recognised verdict at '{self._field}': {p!r}"[:300])

    def get_state(self) -> Dict[str, Any]:
        self._startup_wait()
        if not self.connected:
            return robot_state(False, f"not connected to the MQTT broker (conveyor topic {self.result_topic})")
        with self._cond:
            last = self._last
            owed = list(self._owed)
        if last is None:
            detail = f"listening on {self.result_topic}; no conveyor result yet"
        else:
            p = last["payload"]
            detail = (f"listening on {self.result_topic}; last result {p.get('result')} (cycle {p.get('cycle')}) "
                      f"{self._clock() - last['arrived']:.0f}s ago")
        if owed:
            detail += f"; verdict still missing for {owed} (the next one is treated as ambiguous)"
        return robot_state(True, detail)

    def close(self) -> None:
        from mqtt_common import stop
        stop(self._client)
