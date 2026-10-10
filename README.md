# Task Planning & Scheduling Agent

Turns a job like *"take ABCD from manufacturing_1, check quality, send good
parts to packaging and bad ones to waste"* into a sequence of robot actions,
assigns them to a fleet of mobile manipulators and QC stations, and supervises
them to completion over MQTT.

```
HMI / voice ──hmi/user_prompt──▶ reasoning_agent.py ──scheduler/instructions──▶ scheduler_service.py
   ▲                │  (Ollama + validation)                                         │
   └──hmi/feedback──┘                                       robots/<id>/<sub>/task   │  ▲ robots/<id>/<sub>/status
                                                                                     ▼  │ robots/<id>/<sub>/state
                                                       robot_agent.py (one per subsystem)
                                                         ├─ drivers/mir.py         MiR REST API   (navigate)
                                                         ├─ drivers/ur.py          UR Dashboard + Modbus (pick/place)
                                                         └─ drivers/inspection.py  HTTP or TCP vision (quality_check)
```

> **Safety.** This software coordinates work; it is **not** a safety system.
> E-stops, protective scanners, speed/force limits and arm-vs-base interlocks
> must be enforced by the robot controllers and a risk assessment for the cell
> (ISO 10218 / ISO/TS 15066 for the arm, ISO 3691-4 for the AMR). See
> [docs/COMMISSIONING.md](docs/COMMISSIONING.md) before running on hardware.

## Quick start (simulated fleet, no hardware)

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements-dev.txt
sudo apt install mosquitto          # or point MQTT_BROKER_HOST at your broker
cp .env.example .env                # set MQTT_BROKER_HOST=localhost, MQTT_BROKER_PORT=1883, MQTT_TLS=0

python3 scheduler_service.py &                 # terminal 1
python3 tools/run_sim_fleet.py &               # terminal 2: sim agent for every subsystem
python3 manual_runner.py                       # terminal 3: submit one job and follow it
python3 tools/fleetctl.py status               # fleet + tasks
pytest                                         # ~110 tests incl. end-to-end over a real broker
```

## How a job runs

| # | Subtask | Robot | Group |
|---|---|---|---|
| 1 | navigate → source | combo robot (AMR) | carrier |
| 2 | pick | same robot (arm) | carrier |
| 3 | navigate → QC zone | same robot (AMR) | carrier |
| 4 | quality_check | any free QC station there | inspector |
| 5 | navigate → success/fail dest | same robot (AMR) | carrier |
| 6 | place | same robot (arm) | carrier |

* **Sticky carrier** – the robot that picks a part is the one that places it.
* **Atomic claims + FIFO queue** – no double-booking; work waits for a free,
  healthy robot.
* **Health gating** – a robot is only assigned if *every* subsystem sent a
  recent heartbeat saying online, ready, no e-stop and battery ≥
  `min_battery_percent`.
* **Exactly-once effect** – duplicate, late, or wrong-robot acks are ignored;
  agents de-duplicate re-delivered tasks.
* **Timeouts & retries** – per action (`site.json`); overdue actions are
  cancelled on the robot.
* **Fail safe** – QC only passes on an explicit boolean `pass`; after a
  failure with a part on board the robot goes to `error` and waits for a
  human (`fleetctl reset`). Before the pick, the job is moved to another robot.
* **Persistence** – state in `state/scheduler_state.json`, restored on restart.

## Files

| File | Role |
|---|---|
| `site.json` | Valid locations (the LLM may use only these), timeouts, retries, battery limit. |
| `robots_metadata.json` | Fleet: robots, subsystems, capabilities, zones. |
| `agents/*.json` | Per-subsystem agent config: driver type, host, location → MiR mission / UR program mapping. |
| `scheduler_service.py` | Scheduler process (MQTT, watchdog, status publishing, single-instance lock). |
| `scheduler.py` | Scheduling logic: dispatch, acks, retries, failure policy, operator commands, persistence. |
| `robot_registry.py` | Fleet status (`idle/busy/error/maintenance`) + live health, atomic claims. |
| `validation.py` | Site config + instruction validation (shared by scheduler and reasoning agent). |
| `task_decomposer.py` | Instruction → subtask graph. |
| `robot_agent.py` | Runs a driver for one subsystem; dedupe, cancel, heartbeat + last-will, interlock. |
| `drivers/` | `mir.py`, `ur.py`, `inspection.py` (HTTP / TCP), `sim.py`. Add vendors by subclassing `drivers.base.Driver`. |
| `reasoning_agent.py` | Ollama prompt → validated instruction; optional operator confirmation. |
| `mqtt_common.py`, `mqtt_client.py` | Hardened MQTT clients (persistent sessions, reconnect, TLS, guarded callbacks). |
| `tools/fleetctl.py` | Operator CLI: `status`, `submit`, `cancel`, `reset`, `maintenance`, `watch`. |
| `tools/mir_missions.py` | Lists MiR missions/GUIDs and checks your location mapping. |
| `tools/run_sim_fleet.py` | Simulated agents for the whole fleet. |
| `manual_runner.py` | Submit one job to the running scheduler and follow its events. |
| `deploy/` | systemd units, mosquitto TLS + ACL config. |
| `docs/COMMISSIONING.md` | Step-by-step bring-up on real robots. |

## MQTT contract

| Topic | Direction | Payload |
|---|---|---|
| `scheduler/instructions` | HMI/LLM → scheduler | `{product, source, quality_check, success_dest, fail_dest, request_id?}` |
| `scheduler/commands` | operator → scheduler | `{"cmd":"cancel_task","task_id"}` · `{"cmd":"reset_robot","robot_id"}` · `{"cmd":"set_maintenance","robot_id","on"}` · `{"cmd":"status"}` |
| `scheduler/events` | scheduler → all | `task_accepted/rejected/completed/failed/cancelled/reassigned`, `subtask_dispatched/queued/retry`, `qc_result`, `command_result` |
| `scheduler/status` | scheduler → all (retained) | fleet + task snapshot every 5 s |
| `robots/<id>/<sub>/task` | scheduler → agent | `{"type":"task", subtask_id, attempt, action, zone, expires_at, ...}` or `{"type":"cancel", subtask_id, attempt}` |
| `robots/<id>/<sub>/status` | agent → scheduler | `{subtask_id, attempt, state: accepted\|done\|failed\|cancelled, result?, error?}` (QC: `result.pass` boolean) |
| `robots/<id>/<sub>/state` | agent → all (retained, LWT) | `{online, ready, estop, battery, busy, current, detail}` |
| `hmi/user_prompt` / `hmi/feedback` / `hmi/confirm` | HMI ↔ reasoning agent | prompt text · parse result / errors · `{request_id, confirm}` |

## Deploying

See **[docs/COMMISSIONING.md](docs/COMMISSIONING.md)**. In short: broker with
TLS + per-role accounts (`deploy/mosquitto/`), fill `site.json`,
`robots_metadata.json` and one `agents/*.json` per subsystem, verify each
driver alone with `robot_agent.py --check` / `--test`, then run the services
with the systemd units in `deploy/`.
