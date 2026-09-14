# Task Planning & Scheduling Agent

Sits between your reasoning agent (JSON instructions like `{"product": "ABCD",
"source": "manufacturing_1", "quality_check": "quality_station",
"success_dest": "package_area", "fail_dest": "waste_area"}`) and your fleet of
robots. Decomposes the instruction into ordered subtasks, allocates each to a
robot by capability match, and dispatches over MQTT.

**MQTT end to end -- no ROS2.** The reasoning agent publishes its instruction
JSON as a plain MQTT message on `scheduler/instructions`; the scheduler
publishes/subscribes to robot topics the same way it always did. One
transport for the whole pipeline.

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
| `config.py` | All deployment settings (broker host/port/creds, topic names) read from env vars / `.env`, with your values as defaults. |
| `.env.example` | Copy to `.env` on each machine and fill in real values; never commit the real one. |
| `robot_registry.py` | Loads metadata; atomic `try_claim` for allocation. |
| `task_decomposer.py` | Instruction -> ordered subtask graph, tagged with `group` (carrier/inspector) and `releases_robot`. |
| `allocator.py` | Translates a subtask into a `try_claim` call. |
| `mqtt_client.py` | Scheduler-side: publishes to `robots/<id>/<subsystem>/task`, subscribes to `robots/+/+/status`. |
| `scheduler.py` | Orchestrator: sticky carrier assignment, retry queue, locking, branch resolution. |
| `scheduler_service.py` | The production entry point — MQTT only. Subscribes to `scheduler/instructions` for new tasks and `robots/+/+/status` for acks. |
| `reasoning_agent.py` | Converted from your `agent_llm_node.py` — no ROS2. Subscribes to `hmi/user_prompt` over MQTT, calls Ollama, publishes the resulting JSON to `scheduler/instructions`. |
| `manual_runner.py` | For manual testing — submits one instruction you supply directly (file or default), instead of waiting on the instruction topic. |
| `robot_agent.py` | **Robot-side** agent — runs on each subsystem's controller, subscribes to its task topic, and publishes the status ack once `execute_task()` finishes. `execute_task()` is ported from your `manipulator_control_node.py` (navigate/gripper/QC-simulate), split per action so it fits the decomposed-subtask model instead of one node running the whole job. This is what closes the loop end to end. |
| `test_scheduler_offline.py` | Single-task walkthrough + concurrency stress test, no broker needed. |
| `requirements.txt` | `paho-mqtt`, `python-dotenv`. |
| `deploy/task-scheduler.service` | systemd unit for the scheduler process, auto-restart on failure. |
| `deploy/robot-agent-example.service` | systemd unit template for one robot subsystem's agent — copy per subsystem. |

## Deploying for real

1. `cp .env.example .env` on the scheduler host and on each robot controller
   box that will run `robot_agent.py`; fill in real values (already
   defaulted to what you gave, but `.env` is what you'd actually edit per
   machine going forward instead of touching source).
2. `pip install -r requirements.txt` on every machine.
3. Fill in `robots_metadata.json` with your real fleet.
4. On the scheduler host: `sudo cp deploy/task-scheduler.service /etc/systemd/system/`,
   edit the `User=`/paths (now pointing at `scheduler_service.py`), then
   `sudo systemctl enable --now task-scheduler`.
5. On each robot's controller box: implement `execute_task()` in
   `robot_agent.py` for that subsystem (Nav2 goal for `amr`, MoveIt pick/place
   for `arm`, your vision pipeline for `scanner`) — the stub just sleeps and
   returns success so you can validate the MQTT loop first. Then copy
   `deploy/robot-agent-example.service` per subsystem, adjust `--robot-id`/
   `--subsystem` in `ExecStart`, and enable it the same way.
6. Run `reasoning_agent.py` — it listens on `HMI_PROMPT_TOPIC`
   (`hmi/user_prompt`) for raw prompts, calls Ollama, and publishes to
   `INSTRUCTION_TOPIC` (`scheduler/instructions`), which `scheduler_service.py`
   is already listening on. `deploy/reasoning-agent.service` is the systemd
   unit for it.
7. Test end to end by publishing a raw prompt yourself:
   `mosquitto_pub -h $MQTT_BROKER_HOST -u $MQTT_USERNAME -P $MQTT_PASSWORD -t hmi/user_prompt -m "take the ABCD product from manufacturing_1, check quality, route to package_area or waste_area"`

Everything above also runs directly with `python3 <file>.py` for testing
before you commit to systemd.

## Extending

- New instruction shapes: add a `decompose_xxx()` in `task_decomposer.py`
  and a branch in `decompose()` — nothing else needs to change.
- Smarter robot selection (battery, distance, load) instead of first-idle:
  edit `RobotRegistry.try_claim`'s matching loop.
- If you want *AMR-only* or *arm-only* tasks (no full carrier chain), just
  submit instructions that decompose to subtasks without `group: "carrier"`
  continuity requirements — the sticky logic only kicks in when
  `group == "carrier"`.
