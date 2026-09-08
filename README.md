# Task Planning & Scheduling Agent

Sits between your reasoning agent (JSON instructions like `{"product": "ABCD",
"source": "manufacturing_1", "quality_check": "quality_station",
"success_dest": "package_area", "fail_dest": "waste_area"}`) and your fleet of
robots. Decomposes the instruction into ordered subtasks, allocates each to a
robot by capability match, and dispatches over MQTT.

## Fleet model

Each entry in `robots_metadata.json` is **one physical robot**, which can have
multiple **subsystems** with independent controllers/topics:

```json
{
  "id": "robot_01",
  "name": "MobileManipulator_1",
  "status": "idle",
  "zones": ["*"],
  "subsystems": {
    "amr": { "topic": "amr", "capabilities": ["navigate", "transport"] },
    "arm": { "topic": "arm", "capabilities": ["pick", "place"], "manipulator": true, "dof": 6 }
  }
}
```

The sample file ships 4 mobile-manipulator combos (`robot_01`-`robot_04`) and
2 QC stations (`qc_01`, `qc_02`) — copy the pattern to scale up to your real
count (you mentioned 3-10 combo robots, 1-2 QC stations).

**Whole-robot locking, subsystem-level topics.** The AMR and arm on one combo
robot share a single busy/idle status, because they're physically one unit —
you can't have the base drive off with the arm still mid-task. But each
subsystem still gets its own MQTT topic, matching your separate controllers.

## MQTT topic contract

```
robots/<robot_id>/<subsystem>/task      scheduler -> robot subsystem
robots/<robot_id>/<subsystem>/status    robot subsystem -> scheduler
```
e.g. `robots/robot_01/amr/task`, `robots/robot_01/arm/task`, `robots/qc_01/scanner/task`.

Status payload: `{"subtask_id": "...", "state": "done"|"failed", "result": {...}}`.
For `quality_check`, `result` should include `{"pass": true|false}` — that's
what resolves the success/fail branch.

## Sticky carrier robot

A product is physically carried by one robot from pick to place. So once a
task's first subtask (navigate to source) claims a robot, every later
"carrier" subtask in that task (pick, navigate to dest, place) **reuses that
exact robot** instead of being re-allocated independently — see
`scheduler.py`'s `_carrier_robot` map. The QC step is separate ("inspector"
group): allocated normally, released the instant it reports a result, since
QC stations are scarce and shouldn't be tied up.

## Concurrency safety

Two things make it safe for multiple instructions to hit the scheduler at
the same time:

1. **Atomic claims.** `RobotRegistry.try_claim()` does the "find an idle,
   capable robot" search and the "mark it busy" write inside a single lock,
   so two threads can never both see the same robot as idle and both claim
   it.
2. **A retry queue.** If nothing is idle right now, the subtask goes on a
   pending list instead of failing outright, and is retried every time any
   robot frees up (`_drain_pending`, called after every ack).
3. **A scheduler-level lock** around the task graphs, the pending queue, and
   the sticky-robot map, so `submit_instruction()` and `on_robot_status()`
   calls arriving concurrently (from ROS2 callbacks, MQTT's own thread, or
   several manual submissions) don't corrupt shared state.

`test_scheduler_offline.py` includes a concurrency test that fires 6
instructions at once against 4 available combo robots and asserts exactly 4
claim a robot immediately while the other 2 correctly queue — no
double-claims, nothing dropped.

## Files

| File | Responsibility |
|---|---|
| `robots_metadata.json` | Your fleet: id, subsystems (topic + capabilities each), zones, status. |
| `robot_registry.py` | Loads metadata; atomic `try_claim` for allocation. |
| `task_decomposer.py` | Instruction -> ordered subtask graph, tagged with `group` (carrier/inspector) and `releases_robot`. |
| `allocator.py` | Translates a subtask into a `try_claim` call. |
| `mqtt_client.py` | Publishes to `robots/<id>/<subsystem>/task`, subscribes to `robots/+/+/status`. |
| `scheduler.py` | Orchestrator: sticky carrier assignment, retry queue, locking, branch resolution. |
| `scheduler_node.py` | ROS2 node wiring (ties the reasoning agent's topic to the scheduler). |
| `manual_runner.py` | Runs without ROS2 — for testing with a hand-written instruction against a real broker. |
| `test_scheduler_offline.py` | Single-task walkthrough + concurrency stress test, no broker/ROS2 needed. |

## Wiring it up

1. Fill in `robots_metadata.json` with your real robot ids/names and QC
   station count.
2. `pip install paho-mqtt --break-system-packages`
3. `python3 test_scheduler_offline.py` first — validates dispatch order,
   branch resolution, sticky-robot behavior, and concurrency, with no
   network involved.
4. For a live test without the reasoning agent: `python3 manual_runner.py`
   (broker host/user/pass are already filled in at the top of that file).
5. Each robot subsystem's controller needs to: subscribe to its own
   `.../task` topic, execute what it's told, and publish `{"subtask_id",
   "state", "result"}` back on its own `.../status` topic. That's the one
   piece of new code needed on the robot side to close the loop.
6. Once the reasoning agent is back in the loop, confirm
   `REASONING_AGENT_TOPIC` in `scheduler_node.py` matches whatever
   `agent_llm_node` actually publishes JSON instructions on, then run via
   `ros2 run` (after adding the usual `setup.py`/`package.xml`) or directly
   with `python3 scheduler_node.py`.

## Extending

- New instruction shapes: add a `decompose_xxx()` in `task_decomposer.py`
  and a branch in `decompose()` — nothing else needs to change.
- Smarter robot selection (battery, distance, load) instead of first-idle:
  edit `RobotRegistry.try_claim`'s matching loop.
- If you want *AMR-only* or *arm-only* tasks (no full carrier chain), just
  submit instructions that decompose to subtasks without `group: "carrier"`
  continuity requirements — the sticky logic only kicks in when
  `group == "carrier"`.
