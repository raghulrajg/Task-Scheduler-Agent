# Task Planning & Scheduling Agent

Sits between your Ollama reasoning agent (which emits JSON instructions like
`{"product": "ABCD", "source": "manufacturing_1", "quality_check": "quality_station",
"success_dest": "package_area", "fail_dest": "waste_area"}`) and your fleet of
robots. It decomposes the instruction into atomic subtasks, allocates each
one to a robot by matching required capabilities against a metadata
registry, and dispatches over MQTT — one topic per robot, keyed by robot id.

## Pieces

| File | Responsibility |
|---|---|
| `robots_metadata.json` | Your fleet: id, name, capabilities, manipulator flag, zones, status. Edit this to add/remove robots. |
| `robot_registry.py` | Loads the metadata, answers "which idle robots can do X in zone Y". |
| `task_decomposer.py` | Turns one instruction into an ordered subtask graph (navigate → pick → navigate → quality_check → [branch] → navigate → place). |
| `allocator.py` | Capability-based matching: picks a robot for a single subtask. |
| `mqtt_client.py` | Publishes to `robots/<id>/task`, subscribes to `robots/+/status` for acks. |
| `scheduler.py` | The orchestrator: dispatches ready subtasks, advances the graph on each ack, resolves the success/fail branch once the QC result comes back, skips the branch not taken. |
| `scheduler_node.py` | ROS2 node wiring: subscribes to your reasoning agent's output topic, feeds it into the scheduler. |
| `test_scheduler_offline.py` | Runs the full flow with a fake MQTT client — no broker or ROS2 needed. Run this first. |

## How allocation works

For each subtask the allocator asks the registry for robots that are:
1. `status == "idle"`
2. Have every capability the subtask needs (e.g. `pick` requires `["pick"]`
   and `require_manipulator: true`)
3. Can operate in the subtask's zone (`zones` on the robot, `"*"` = anywhere)

First match wins today; `allocator._pick_best()` is the one place to add
smarter tie-breaking later (battery level, distance, current load) without
touching decomposition or MQTT.

## MQTT contract

- Scheduler → robot: `robots/<robot_id>/task`, payload is the subtask dict
  (`subtask_id`, `action`, `target`, `zone`, ...).
- Robot → scheduler: `robots/<robot_id>/status`, payload:
  `{"subtask_id": "...", "state": "done"|"failed", "result": {...}}`.
  For the `quality_check` action, `result` should include `{"pass": true|false}`
  — that's what the scheduler reads to pick the success/fail branch.

Each robot node needs to publish that status message when it finishes
executing a task — that's the one piece of new code needed on the robot side
to close the loop.

## Wiring it up

1. **Confirm the topic name.** `scheduler_node.py` currently subscribes to
   `robot_task_queue` — swap `REASONING_AGENT_TOPIC` for whatever your
   `agent_llm_node` actually publishes JSON instructions on.
2. **Install deps** on the node running the scheduler:
   `pip install paho-mqtt --break-system-packages`
   (rclpy comes from your ROS2 Jazzy install already.)
3. **Point at your broker**: edit `MQTT_BROKER_HOST` / `MQTT_BROKER_PORT` in
   `scheduler_node.py`.
4. **Fill in `robots_metadata.json`** with your real fleet — the three
   sample entries mirror the roles implied by your test prompt (a mobile
   base for navigation/transport, a 6-DOF arm for pick/place, a vision
   station for quality_check). Split/merge as needed if one physical robot
   does more than one role (e.g. an arm mounted on a mobile base — give it
   `capabilities: ["navigate","pick","place"]`).
5. **Run**: `ros2 run task_scheduler_pkg scheduler_node` (after adding this
   as a package the normal ROS2 way — `setup.py` entry point, `package.xml`
   depending on `rclpy` and `std_msgs`), or `python3 scheduler_node.py`
   directly for a quick check.

## Extending decomposition

`task_decomposer.py` currently only knows the pick→QC→route pattern. When
your reasoning agent starts emitting other instruction shapes, add another
`decompose_xxx()` function and a branch in `decompose()` that matches on the
instruction's keys — the allocator, MQTT layer, and scheduler don't need to
change.

## Notes / things to double check

- `test_scheduler_offline.py` was run in this environment and passes —
  it validates the dispatch order, the branch resolution, and that the
  fail-branch steps are correctly never sent when QC passes.
- The scheduler currently assumes each task instruction is independent —
  concurrent tasks work (each gets its own `task_id`), but if two tasks
  need the *same* robot at once, whichever one allocates first wins and the
  other logs an `AllocationError` and is not retried automatically. Add a
  retry/backoff loop in `TaskScheduler._try_dispatch` if you want tasks to
  queue for a busy robot instead of failing.
