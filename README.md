# Robot Task Scheduler

The scheduler turns a request like *"take ABCD from manufacturing_1, check its
quality, good parts to packaging, bad parts to waste"* into robot actions. It
assigns those actions to mobile manipulators (a mobile base with a **Dobot Nova
2** mounted on it) and quality-check stations, then supervises every step until
the job is done. All parts talk over your **EMQX** MQTT broker.

```
 Operator / HMI                         CENTRAL SERVER                                    ROBOT PC (on robot_01)
 ──────────────                         ──────────────                                    ──────────────────────
 "take ABCD from ..."  ── hmi/user_prompt ──▶  reasoning_agent.py  (Ollama + validation)
 fleetctl / manual_runner                          │ scheduler/instructions
                                                   ▼
                                          scheduler_service.py ── robots/robot_01/amr/task ──▶ robot_agent (amr) ─▶ mobile robot web API
                                          (allocation, watchdog,  ── robots/robot_01/arm/task ──▶ robot_agent (arm) ─▶ Dobot Nova 2 (TCP 29999)
                                           retries, persistence)  ── robots/qc_01/scanner/task ─▶ robot_agent (QC)  ─▶ vision station / stand-in
                                                   ▲
                                                   └──── robots/+/+/status (results) ◀── robots/+/+/state (heartbeats) ──┘
                                            EMQX 172.25.200.57:1883  (every message goes through the broker)
```

> **Safety first.** This software coordinates work. It is **not** a safety
> system. E-stops, protective fields, speed limits and the interlock between
> arm and base must be enforced by the robot controllers and wired safety I/O.
> See [docs/COMMISSIONING.md](docs/COMMISSIONING.md).

## Contents
1. [Three ways to run](#1-three-ways-to-run)
2. [Install](#2-install-once-per-machine)
3. [Configure](#3-configure)
4. [Preflight check](#4-preflight-check-nothing-moves)
5. [TEST mode: simulated robots on your EMQX](#5-test-mode-simulated-robots-on-your-emqx)
6. [REAL mode: deployment](#6-real-mode-deployment)
7. [Offline dry run and automated tests](#7-offline-dry-run-and-automated-tests)
8. [How it works, end to end](#8-how-it-works-end-to-end)
9. [MQTT topic reference](#9-mqtt-topic-reference)
10. [Configuration reference](#10-configuration-reference)
11. [Troubleshooting](#11-troubleshooting)
12. [Files](#12-files)

---

## 1. Three ways to run

| | **Offline dry run** | **TEST mode** | **REAL mode** |
|---|---|---|---|
| What runs | the real scheduler logic, simulated robots, simulated time | the full system with simulated robots | the full system with the real robots |
| Broker | none | your EMQX | your EMQX |
| Topics | none (in memory) | under `sim/` (`sim/robots/...`, `sim/scheduler/...`) | at the root (`robots/...`, `scheduler/...`) |
| MQTT client ids | none | end with `-test` | as is |
| Scheduler state file | none | `state/test_scheduler_state.json` | `state/scheduler_state.json` |
| Start with | `python3 offline_demo.py` | `python3 launch.py test` | `python3 launch.py server` + `python3 launch.py robot robot_01` |
| Talk to it with | (prints everything) | `tools/fleetctl.py --test ...`, `manual_runner.py --test` | `tools/fleetctl.py ...`, `manual_runner.py` |

TEST and REAL mode can run **at the same time on the same EMQX** without
seeing each other. Every topic, client id and state file is separate, so
simulated robots can never answer for real ones. As extra guards:
- the simulated fleet refuses to start in REAL mode;
- a simulated agent can only serve REAL jobs if its config explicitly says so (the QC stand-in, see 6.3).

## 2. Install (once per machine)

You need Python 3.9 or newer on the central server and on each robot PC (developed and tested on 3.13, also checked on 3.11). There's
nothing to install for the broker, because you already run EMQX.

**Linux**
```bash
cd /opt/robot-fleet            # or wherever you unzip/clone the project
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt          # paho-mqtt, python-dotenv
pip install -r requirements-dev.txt      # + pytest (only for running the tests)
```
**Windows** (PowerShell)
```powershell
cd C:\robot-fleet
py -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```
On Windows, type `python` where this README says `python3`.

## 3. Configure

### 3.1 `.env`: broker and Ollama (every machine)
Copy `.env.example` to `.env` in the project folder and fill it in. The same
file serves both TEST and REAL mode.
```dotenv
MQTT_BROKER_HOST=172.25.200.57
MQTT_BROKER_PORT=1883
MQTT_USERNAME=raghulrajg
MQTT_PASSWORD=<your EMQX password -- change the one that was published on GitHub>
MQTT_TLS=0

OLLAMA_HOST=172.16.13.90
OLLAMA_PORT=11434
OLLAMA_MODEL=llama3:8b
REQUIRE_CONFIRMATION=1           # operator confirms each LLM-parsed job before robots move

AMR_USERNAME=                    # robot PC only, if the mobile robot's dashboard needs a login
AMR_PASSWORD=
```
`.env` is in `.gitignore`. Never commit it.

### 3.2 Site and fleet (central server)
- `site.json` lists the **locations** (the only names the LLM and the
  scheduler accept), plus timeouts, retries and the battery limit.
- `robots_metadata.json` lists the robots and what each subsystem can do.
  Robots without a running agent are simply treated as offline. You can
  delete the sample entries you don't have (robot_02–04, qc_02).

### 3.3 Agent configs (robot PC): `agents/*.json`
One file per robot subsystem, named `<robot_id>_<subsystem>.json`:

| File | Driver | What to fill in |
|---|---|---|
| `agents/robot_01_amr.json` | `http_amr`: the mobile robot's own web API | endpoints and fields from the capture ([docs/AMR_API_CAPTURE.md](docs/AMR_API_CAPTURE.md)), and the drop-point id of each location |
| `agents/robot_01_arm.json` | `dobot`: Dobot Nova 2, TCP 29999 | controller IP, `protocol` v3/v4, one DobotStudio Pro project per pick/place, `travel_joints` |
| `agents/qc_01_stand_in.json` | `sim`: stand-in QC that **passes every part** | nothing. Use it until a camera is integrated |
| `agents/qc_01_scanner.json` | `http_inspection` / `tcp_inspection` | the vision system's address and pass/fail fields |

Secrets in agent configs are written as `"${AMR_PASSWORD}"` and read from `.env`.

## 4. Preflight check (nothing moves)

```bash
python3 launch.py check                 # REAL mode namespace
python3 launch.py check --test          # TEST mode namespace
python3 launch.py check robot_01        # + state of robot_01's hardware (reads only)
```
It checks:
- the EMQX login, and that publish and subscribe work;
- whether a scheduler is running and which agents are online (from their heartbeats);
- that Ollama is reachable and the model is installed;
- that `site.json` and `robots_metadata.json` load;
- with a robot id, each driver's live state, e.g. Dobot mode, travel pose and result register, or the mobile robot's navigation state and battery.

The exit code is non-zero if anything failed.

## 5. TEST mode: simulated robots on your EMQX

Everything runs on one PC against your real broker. Nothing physical moves.

```bash
python3 launch.py test                         # scheduler + simulated fleet (Ctrl+C stops all)
python3 launch.py test --jobs 5 --speed 4      # ... auto-submit 5 jobs, robots 4x faster
python3 launch.py test --with-llm              # ... + reasoning agent (your Ollama)
python3 launch.py test --fail-rate 0.2 --qc-pass-rate 0.5   # stress: failures and rejects
```
In a second terminal:
```bash
python3 manual_runner.py --test                # submit one job and watch every step
python3 tools/fleetctl.py --test status        # robots + jobs
python3 tools/fleetctl.py --test watch         # live event stream
python3 tools/fleetctl.py --test prompt "take ABCD from manufacturing 1, check it, good parts to packaging"
python3 tools/fleetctl.py --test confirm <request_id>    # if REQUIRE_CONFIRMATION=1
```

**Hardware in the loop.** You can let **one real subsystem** join the test
while the rest stays simulated:
```bash
python3 launch.py test --hardware agents/robot_01_arm.json
```
Here the real Dobot runs its projects, and the base and QC are simulated.
This is the safest way to bring up one machine under real scheduler control.

## 6. REAL mode: deployment

Run `python3 launch.py check robot_01` first and keep someone at the e-stop.

### 6.1 Central server
```bash
python3 launch.py server             # scheduler + reasoning agent
python3 launch.py server --no-llm    # scheduler only
```
### 6.2 Robot PC (the computer riding on robot_01)
```bash
python3 launch.py robot robot_01     # starts every agents/robot_01_*.json (amr + arm)
```
This PC needs network access to the mobile robot's API, the Dobot controller,
**and** EMQX. See "Network layout" in [docs/AMR_API_CAPTURE.md](docs/AMR_API_CAPTURE.md).

### 6.3 Quality-check station
```bash
python3 launch.py agents agents/qc_01_stand_in.json   # stand-in: no inspection, every part passes
python3 launch.py agents agents/qc_01_scanner.json    # the real vision station, once configured
```
### 6.4 Give the robots work
```bash
python3 tools/fleetctl.py prompt "take ABCD from manufacturing 1, check it, good parts to packaging"
python3 tools/fleetctl.py confirm <request_id>        # REQUIRE_CONFIRMATION=1
python3 manual_runner.py                              # fixed job (or: manual_runner.py my_job.json), follow it live
python3 tools/fleetctl.py submit my_job.json          # submit and return
```
An HMI can do the same by publishing on `hmi/user_prompt` (text, or
`{"prompt": "...", "request_id": "..."}`) and reading `hmi/feedback`. It can
also publish a structured job straight to `scheduler/instructions`.

### 6.5 Operate
```bash
python3 tools/fleetctl.py status                     # who does what, health problems
python3 tools/fleetctl.py watch                      # live events
python3 tools/fleetctl.py cancel <task_id>           # stop a job; robots stop their current action
python3 tools/fleetctl.py reset <robot_id>           # after a human has recovered a robot in 'error'
python3 tools/fleetctl.py maintenance <robot_id> on  # take a robot out of rotation (off to return it)
```
A robot in **error** may still be holding a part. Check it physically before you reset it.

### 6.6 Start on boot
**Linux (systemd).** Install the project at `/opt/robot-fleet`, owned by user `fleet`:
```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now robot-fleet-server                  # central server
sudo systemctl enable --now robot-fleet-robot@robot_01          # robot PC
sudo systemctl enable --now robot-fleet-agent@qc_01_stand_in    # QC (stand-in or real)
journalctl -u robot-fleet-server -f                             # logs
```
**Windows.** Use `deploy\windows\server.bat`, `robot.bat robot_01`,
`test.bat` and `check.bat`. To start one at boot, create a Task Scheduler task
("At startup") that runs it.

`launch.py` restarts any program that crashes (with back-off) and shuts
everything down cleanly. When an agent stops, it cancels its running action on
the hardware first. On Windows, stop it with Ctrl+C in the launcher window,
not by closing the window.

## 7. Offline dry run and automated tests

```bash
python3 offline_demo.py                     # 11 scenarios, step by step, PASS/FAIL (no broker, no packages)
python3 offline_demo.py --list
python3 offline_demo.py --jobs 10 --qc-pass-rate 0.7 --seed 1

pytest                                      # unit + driver tests; broker tests skip without a broker
TEST_MQTT_HOST=172.25.200.57 TEST_MQTT_PORT=1883 \
TEST_MQTT_USERNAME=raghulrajg TEST_MQTT_PASSWORD='<password>' pytest   # + end-to-end over your EMQX
```
The broker tests give every test its own topic namespace (`pytest/<random>/`)
and unique client ids. They never touch the real topics, and they delete
their retained messages and sessions afterwards.

---

## 8. How it works, end to end

### 8.1 One job, step by step
```
 time ─────────────────────────────────────────────────────────────────────────────────────────────▶
 reasoning   prompt ─▶ Ollama ─▶ JSON ─▶ validate ─▶ (operator confirms) ─▶ scheduler/instructions
 scheduler   validate ─▶ decompose (6 steps + 2 branches) ─▶ ALLOCATE carrier ─▶ dispatch step 1
 robot_01    amr: navigate→manufacturing_1   arm: pick   amr: navigate→quality_station
 qc_01                                                                     quality_check → pass/fail
 robot_01                                                                      amr: navigate→package_area|waste_area
 robot_01                                                                      arm: place ─▶ robot released
 scheduler   every result: verify ─▶ mark done ─▶ dispatch next step ─▶ ... ─▶ task_completed event
```

### 8.2 Instruction intake and validation (`reasoning_agent.py`, `validation.py`)
1. A prompt arrives on `hmi/user_prompt`. It goes onto a queue, and a worker
   thread calls Ollama, so a slow model never blocks the MQTT connection. The
   system prompt lists the allowed locations, generated from `site.json`.
2. The model's JSON is **validated before anything else**:
   - all 5 fields are present and non-empty strings;
   - every location exists in `site.json`;
   - `success_dest` and `fail_dest` differ;
   - the product name is safe.

   A rejection goes back on `hmi/feedback` with the reason, and nothing is sent on.
3. With `REQUIRE_CONFIRMATION=1`, the parsed job waits until an operator
   confirms it on `hmi/confirm`. Unconfirmed jobs expire after 5 minutes.
4. The scheduler validates **again** when the job arrives on
   `scheduler/instructions`. It also checks that at least one QC station works
   in the requested QC zone. Otherwise the carrier would wait there forever.

### 8.3 Decomposition (`task_decomposer.py`)
Each job becomes a small graph of subtasks:

| id | action | zone | needs | group | releases robot |
|---|---|---|---|---|---|
| `T-1` | navigate | source | navigate | carrier | no |
| `T-2` | pick | source | pick (manipulator) | carrier | no |
| `T-3` | navigate | QC zone | navigate | carrier | no |
| `T-4` | quality_check | QC zone | quality_check | inspector | **yes** |
| `T-nav-success` / `T-nav-fail` | navigate | success / fail dest | navigate | carrier | no |
| `T-place-success` / `T-place-fail` | place | success / fail dest | place (manipulator) | carrier | **yes** |

Each step depends on the previous one. Both branches depend on `T-4`, and the
QC result decides which branch runs. The other branch is never dispatched.

### 8.4 Allocation: how a robot is chosen (`robot_registry.py`, `scheduler.py`)
When a step must be dispatched:

1. **Carrier steps after the first one reuse the job's carrier robot** ("sticky
   carrier"). The robot that picked the part is always the one that places it.
   No new allocation happens.
2. Otherwise (the first navigate, or any QC step) the registry runs an
   **atomic claim**. Under one lock, it keeps only robots that pass *all* of
   these checks:
   - status is `idle`. It is not `busy`, `error` (waiting for a human) or `maintenance`;
   - **every subsystem is healthy**, according to its latest heartbeat:
     online, `ready`, no e-stop, heard from within `heartbeat_timeout_s` (15 s),
     battery ≥ `min_battery_percent` (25 %);
   - it has every capability the step needs, on the right kind of subsystem
     (`pick`/`place` must be on a manipulator);
   - it may work in the step's zone (`"*"` = anywhere; QC stations only in their own zone).

   Among the robots left, it picks the one with the **most battery**, marks it
   `busy`, and releases the lock. Two jobs arriving at the same moment can
   never get the same robot.
3. If nothing qualifies, the step joins a **FIFO waiting queue**, and a
   `subtask_queued` event is published once. The queue is retried:
   - after every robot result;
   - whenever a heartbeat changes whether a robot can take work (charged, e-stop released, came online);
   - every second by the watchdog.

   The oldest waiting step gets the first free robot.
4. Robots are freed only by steps marked *releases robot*:
   - a QC station right after its result;
   - the carrier only after the final **place**, never in the middle of a job.

### 8.5 Dispatch and the robot agent (`robot_agent.py`, `drivers/`)
The scheduler publishes the step on `robots/<id>/<subsystem>/task`. The
message adds:
- an `attempt` number;
- `dispatched_at`;
- `expires_at` (= now + `accept` timeout).

It records the step as *in flight*, with a deadline for the robot to accept.

The agent on the robot PC:
1. **De-duplicates.** MQTT QoS 1 can deliver a message twice. If it already
   ran this exact `(subtask_id, attempt)`, it re-sends the stored result
   instead of moving again.
2. **Refuses**, with a `failed` status and the reason, if:
   - it is already busy;
   - the action isn't supported;
   - the task is **stale** (delivered after `expires_at`, e.g. it was queued while the robot was off Wi-Fi);
   - the **interlock** says no: the sibling subsystem is busy or offline, or the mounted arm isn't in its travel pose.
3. Otherwise it publishes a heartbeat with `busy=true` (so the sibling sees it
   at once), then `accepted`. It runs the driver in a **worker thread**, so
   the MQTT connection stays alive and a cancel can arrive mid-motion:
   - **Mobile robot (`http_amr`):**
     1. Check there's no e-stop and it isn't already moving.
     2. Send the goto request.
     3. Poll status until it has been seen moving and then arrived. A stale
        "arrived" from the previous goal is ignored, and the target must
        match.
     4. On e-stop, cancel or timeout, send cancel.
   - **Dobot (`dobot`):**
     1. Check the arm is enabled and idle (`RobotMode()` = 5).
     2. Write 0 to output register 0.
     3. `RunScript("<project>")`.
     4. Watch `RobotMode()` until the project ends. On an alarm or collision,
        send `Stop()`.
     5. Read register 0 (1 = success, 2 = failure, 0 = no result → failure).
     6. Check the arm is back in its **travel pose**.
   - **QC:** an explicit boolean `pass`. Anything ambiguous is a failure, never a pass.
4. It publishes a fresh heartbeat (`busy=false`, current travel pose), **then**
   the retained result: `done` (with `result`), `failed` (with `error`) or
   `cancelled`.

### 8.6 Results back at the scheduler
A status is applied **only if it matches the in-flight record exactly**: same
subtask, robot, subsystem and attempt. Duplicates, late answers from an old
attempt, and answers from the wrong robot are ignored. The scheduler then
handles each state:
- `accepted`: the deadline becomes now + the action timeout (`site.json`:
  navigate 300 s, pick/place 120 s, QC 90 s).
- `running`: informational only.
- `done`: the step is marked complete.
  - A pick marks the part as **on board**.
  - A QC `done` sets the branch. Only `pass: true` → success and
    `pass: false` → fail. A missing or garbled result is treated as a failure.
  - The steps that depend on it are dispatched. When every step on the chosen
    branch is done, the job is **completed**.
- `failed` / `cancelled`: see 8.7.

### 8.7 Failures, retries, timeouts and cancels
- **Timeouts.** The watchdog runs every second. A step that wasn't accepted
  within 30 s, or didn't finish within its action timeout, is cancelled on the
  robot and treated as failed.
- **Retries.** A failed step is retried up to `max_retries` (navigate 1,
  pick 1, place 0, QC 1). Every retry gets a **new attempt number**, so robots
  never confuse it with the old one. A QC retry may go to the other station.
- **After retries run out:**
  - **Before the pick** (the carrier failed while driving to the source):
    the robot goes to `error` and the job is **reassigned** to another robot,
    at most `max_task_reassignments` (2) times.
  - **Pick, or anything after it:** the robot goes to `error` (it may hold
    the part) and the job is **failed**. A human recovers the part, then runs
    `fleetctl reset <robot>`.
  - **QC station failure:** the station and the carrier go to `error`, and the job is failed.
- **Operator cancel.** Steps in flight get a cancel message. The robot is
  released when its agent confirms. It goes to `idle`, or to `error` if a part
  may still be on board.

### 8.8 Health, heartbeats and interlocks
- Each agent publishes a **retained heartbeat** every 5 s on
  `robots/<id>/<sub>/state` with `online`, `ready`, `estop`, `battery`, `busy`
  and `travel_pose`.
- Its MQTT **last will** (also retained) says `online:false` if the process or
  the network dies, so the scheduler stops allocating that robot within
  seconds.
- The base and arm agents of one robot watch each other's heartbeats. That is
  the software interlock:
  - the base will not drive while the arm is busy or not in its travel pose;
  - the arm will not move while the base is busy.

  The real interlock must still be hardware.

### 8.9 Persistence and restart
After every change, the scheduler writes all jobs, steps in flight, the
waiting queue and robot statuses to its state file. It writes atomically, so
a crash can't half-write it. On restart, it:
- reloads everything;
- keeps robots that carry a part `busy`;
- gives robots `startup_grace_s` (30 s) to report before any timeout fires.

Results that came in while it was down are not lost. They are delivered from
the retained status topics and from EMQX's persistent sessions (clients
connect with clean session = false). A file lock stops two schedulers from
running from the same state directory.

### 8.10 TEST-mode isolation
`RUN_MODE=test` changes three things. `--test` on any program and
`launch.py test` set it for you.
- **Topic prefix:** every topic gets `sim/`, and incoming topics without that
  prefix are ignored.
- **Client ids:** every client id gets `-test`. EMQX allows only one
  connection per client id, so this stops test clients from kicking real ones
  off.
- **State file:** the scheduler uses its own state file.

---

## 9. MQTT topic reference
In TEST mode, prefix every topic with `sim/`.

| Topic | From → to | Payload |
|---|---|---|
| `hmi/user_prompt` | HMI → reasoning agent | text, or `{"prompt", "request_id"}` |
| `hmi/feedback` | reasoning agent → HMI | `{request_id, ok, instruction?, needs_confirmation?, forwarded?, error?}` |
| `hmi/confirm` | HMI → reasoning agent | `{"request_id", "confirm": true/false}` |
| `scheduler/instructions` | HMI/LLM → scheduler | `{product, source, quality_check, success_dest, fail_dest, request_id?}` |
| `scheduler/commands` | operator → scheduler | `{"cmd": "cancel_task"/"reset_robot"/"set_maintenance"/"status", ...}` |
| `scheduler/events` | scheduler → all | `task_accepted/rejected/completed/failed/cancelled/reassigned`, `subtask_dispatched/queued/retry`, `qc_result`, `command_result`, `robot_reset` |
| `scheduler/status` | scheduler → all (retained, every 5 s) | robots + jobs snapshot; `"scheduler": "running"/"stopped"` |
| `robots/<id>/<sub>/task` | scheduler → agent | `{"type": "task", subtask_id, attempt, action, zone, target, expires_at, ...}` / `{"type": "cancel", subtask_id, attempt}` |
| `robots/<id>/<sub>/status` | agent → scheduler (terminal states retained) | `{subtask_id, attempt, state: accepted/done/failed/cancelled, result?, error?}` |
| `robots/<id>/<sub>/state` | agent → all (retained, + last will) | `{online, ready, estop, battery, busy, current, travel_pose?, detail}` |

## 10. Configuration reference
| Setting | Where | Meaning |
|---|---|---|
| `MQTT_BROKER_HOST/PORT/USERNAME/PASSWORD`, `MQTT_TLS` | `.env` | your EMQX |
| `OLLAMA_HOST/PORT/MODEL`, `REQUIRE_CONFIRMATION` | `.env` | reasoning agent |
| `RUN_MODE`, `MQTT_TOPIC_PREFIX` | env (set by `--test` / `launch.py`) | real or test; test prefix defaults to `sim/` |
| `locations` | `site.json` | allowed location names (+ descriptions shown to the LLM) |
| `timeouts_s`, `max_retries`, `max_task_reassignments` | `site.json` | failure handling (8.7) |
| `min_battery_percent`, `heartbeat_timeout_s`, `startup_grace_s` | `site.json` | allocation health gate, restart grace |
| robots, subsystems, capabilities, zones | `robots_metadata.json` | fleet model |
| driver settings, `interlock_with`, `require_travel_pose` | `agents/*.json` | per-subsystem hardware binding |

Ready-made configs for other vendors are in `agents/examples/`: MiR, Universal Robots and a TCP camera.

## 11. Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `check`: *broker refused the login* | wrong `MQTT_USERNAME/PASSWORD` in `.env` |
| `check`: *no answer from the broker* | wrong host/port, firewall, or `MQTT_TLS=1` on the plain 1883 port |
| `fleetctl status`: *scheduler STOPPED* / *no status* | start `launch.py server` (or `launch.py test`); check `--test` matches the stack you started |
| robot shows `amr: offline` | its agent isn't running, or can't reach EMQX |
| robot shows `not ready (...)` | read the detail: Dobot not enabled / alarm, mobile robot e-stop, battery low, arm not in travel pose |
| job stuck `waiting for a free robot` | no robot passes the checks in 8.4. `fleetctl status` shows why for each robot |
| agent refuses: `interlock: ...` | the sibling is busy/offline, or the arm isn't in its travel pose |
| Dobot: `travel_joints must be a list of 6 ...` | fill in `travel_joints` (read them with `tools/dobot_console.py --host <ip> "GetAngle()"`) |
| Dobot: commands refused after `RequestControl` | start the agent while the arm is disabled, or switch DobotStudio Pro to TCP mode (v3) |
| robot in `error` | inspect the robot (part on board?), then `fleetctl reset <robot>` |
| `simulated driver in REAL mode` | use `--test`, or the explicit stand-in config `agents/qc_01_stand_in.json` |

## 12. Files
| File | Role |
|---|---|
| `launch.py` | one command per machine role (`check`, `test`, `server`, `robot`, `agents`), supervises and restarts |
| `scheduler_service.py` | scheduler process: MQTT, 1 s watchdog, status every 5 s |
| `scheduler.py` | the scheduling logic: dispatch, results, retries, failure policy, operator commands, persistence |
| `robot_registry.py` | fleet status + live health, atomic claims |
| `task_decomposer.py`, `validation.py` | job → steps; input validation |
| `robot_agent.py` | one subsystem's agent: dedupe, cancel, heartbeat/last will, interlock |
| `drivers/` | `dobot.py`, `http_amr.py`, `inspection.py`, `sim.py` (+ `mir.py`, `ur.py`) |
| `reasoning_agent.py` | prompt → Ollama → validated job, optional confirmation |
| `mqtt_common.py`, `mqtt_client.py`, `config.py`, `modes.py` | MQTT plumbing, settings, run modes |
| `manual_runner.py`, `tools/fleetctl.py` | submit and follow jobs; operator console (`status`, `watch`, `prompt`, `confirm`, `cancel`, `reset`, `maintenance`) |
| `offline_demo.py`, `tools/run_sim_fleet.py` | dry run without a broker; simulated fleet for TEST mode |
| `tools/dobot_console.py`, `tools/har_summary.py`, `tools/api_watch.py` | commissioning helpers |
| `deploy/systemd/`, `deploy/windows/`, `deploy/emqx/` | start-on-boot units, Windows launchers, optional EMQX ACL |
| `docs/COMMISSIONING.md`, `docs/AMR_API_CAPTURE.md` | bring-up checklist; how to capture the mobile robot's API |
