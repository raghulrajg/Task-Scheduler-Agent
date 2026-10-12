# Robot Task Scheduler

The scheduler turns a request like *"take ABCD from manufacturing_1, check its
quality, good parts to packaging, bad parts to waste"* into robot actions. It
assigns those actions to mobile manipulators (an **ONIX** mobile base with a
**Dobot Nova 2** mounted on it) and quality-check stations, then supervises
every step until the job is done. All parts talk over your **EMQX** MQTT broker.

Every location has an **x, y, theta** on the AMR's map, stored in
[`stations.json`](#34-station-coordinates-stationsjson). When the base is sent
to a location, the agent reads that pose, sends it to the AMR over its API, and
reports the move done **only after it has verified the robot is there**:
within tolerance, stopped, and localized. Only then does the next step (e.g.
the Dobot pick) start.

```
 Operator / HMI                         CENTRAL SERVER                                    ROBOT PC (on robot_01)
 ──────────────                         ──────────────                                    ──────────────────────
 "take ABCD from ..."  ── hmi/user_prompt ──▶  reasoning_agent.py  (Ollama + validation)
 fleetctl / manual_runner                          │ scheduler/instructions
                                                   ▼
                                          scheduler_service.py ── robots/robot_01/amr/task ──▶ robot_agent (amr) ─▶ ONIX AMR (WebSocket API,
                                          (allocation, watchdog,                                         │           x,y,theta from stations.json)
                                           retries, persistence)  ── robots/robot_01/arm/task ──▶ robot_agent (arm) ─▶ Dobot Nova 2 (TCP 29999)
                                                   │
                                                   ├── robots/qc_01/scanner/task ─▶ robot_agent (QC) ── qc/qc_01/request ──▶ scanner_bridge.py
                                                   ▲                                                ◀── qc/qc_01/result ───   (SCANNER PC)
                                                   └──── robots/+/+/status (results) ◀── robots/+/+/state (heartbeats) ──┘
                                            EMQX 172.25.200.57:1883  (every message goes through the broker; MQTT only)
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
pip install -r requirements.txt          # paho-mqtt, python-dotenv, websocket-client
pip install -r requirements-dev.txt      # + pytest, websockets (only for the tests / ONIX simulator)
pip install requests matplotlib          # only for the vendor-style tools in amr_tools/ (live map viewer, GUI)
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

SCANNER_COMMAND=                 # scanner PC only: the program that does one inspection (see 6.3)
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
| `agents/robot_01_amr.json` | `onix`: the ONIX AMR's WebSocket API (ports 3000/4000/7000) | robot IP (`172.22.74.119`), arrival tolerances. Targets come from `stations.json` (3.4) |
| `agents/robot_01_arm.json` | `dobot`: Dobot Nova 2, TCP 29999 | controller IP, `protocol` v3/v4, one DobotStudio Pro project per pick/place, `travel_joints` |
| `agents/qc_01_scanner.json` | `mqtt_inspection`: QC over MQTT only | station name (`qc_01`), timeouts, pass/fail values. The scanner PC runs `scanner_bridge.py` (6.3) |
| `agents/qc_01_stand_in.json` | `sim`: stand-in QC that **passes every part** | nothing. Use it until a camera is integrated |

Secrets in agent configs are written as `"${NAME}"` and read from `.env`.

### 3.4 Station coordinates: `stations.json`
`stations.json` is the **metadata file of every place the AMR can be sent**. It
has two parts:

- `stations`: the stations on the AMR's map (`LM1`, `LM2`, ...) with their
  x, y (metres) and theta (radians), copied from the map. Re-run `pull` after
  you edit the map in the AMR web app.
- `locations`: every `site.json` location (`manufacturing_1`, `package_area`,
  ...) with **its own x, y, theta**. A location is either linked to a map
  station (`"station": "LM1"`, its pose follows the station on the next
  `pull`) or has a pose of its own (taught or typed in). It may also override
  the arrival tolerances.

```json
"locations": {
  "manufacturing_1": {"station": "LM1", "x": -1.1577, "y": 0.0483, "theta": 0.0},
  "package_area":    {"x": 2.35, "y": -0.8, "theta": 1.5708, "xy_tolerance_m": 0.05},
  "waste_area":      {"station": null, "x": null, "y": null, "theta": null}
}
```
A location with no pose (`null`) cannot be used: jobs that need it are
**refused when they are submitted**, before anything moves.

Maintain it with `tools/amr_stations.py` (it reads the robot's address from
`agents/robot_01_amr.json`; nothing here moves the robot):
```bash
python3 tools/amr_stations.py pull                        # stations from the robot's live map
python3 tools/amr_stations.py pull --map amr_tools/maps/live_map.json   # ... or from an exported map file
python3 tools/amr_stations.py list                        # stations, locations, what is missing
python3 tools/amr_stations.py assign manufacturing_1 LM1  # location = that map station's pose
python3 tools/amr_stations.py teach package_area          # location = where the robot stands NOW
python3 tools/amr_stations.py set waste_area -2.5 1.2 90  # x y theta (degrees), typed in
python3 tools/amr_stations.py set waste_area -2.5 1.2 90 --xy-tol 0.05 --yaw-tol 3
python3 tools/amr_stations.py unassign waste_area
python3 tools/amr_stations.py where                       # robot pose now, and the location it is at
python3 tools/amr_stations.py check                       # exit 1 if a site.json location has no pose
```
To **teach** a place: drive the robot there in the AMR web app (or push it),
check it is localized, then run `teach <location>`. The agent re-reads
`stations.json` at the start of every move, so changes apply without a restart.

**State of the file shipped here** (from the map *"Map 17:3 September 10"*,
stations LM1 and LM2):

| Location | Pose | |
|---|---|---|
| `manufacturing_1` | LM1 (-1.158, 0.048, 0°) | **assumed**: check it is the right station |
| `quality_station` | LM2 (-3.820, 0.011, 0°) | **assumed**: check it is the right station |
| `package_area`, `waste_area`, `rest_position` | none | **teach or set before use** |

## 4. Preflight check (nothing moves)

```bash
python3 launch.py check                 # REAL mode namespace
python3 launch.py check --test          # TEST mode namespace
python3 launch.py check robot_01        # + state of robot_01's hardware (reads only)
```
It checks:
- the EMQX login, and that publish and subscribe work;
- whether a scheduler is running and which agents are online (from their
  heartbeats), and where each base says it is;
- whether the scanner bridges are online (`qc/<station>/status`);
- that Ollama is reachable and the model is installed;
- that `site.json` and `robots_metadata.json` load, and which locations still
  have no x/y/theta in `stations.json`;
- with a robot id, each driver's live state, e.g. Dobot mode, travel pose and
  result register, or the ONIX pose, localization, battery and current location.

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

The simulated bases behave like the real one where it matters: they report
which location they are at, and the simulated arms refuse to pick or place
unless their base is at the task's location.

**Hardware in the loop.** You can let **real subsystems** join the test
while the rest stays simulated:
```bash
python3 launch.py test --hardware agents/robot_01_arm.json      # real Dobot, simulated base and QC
python3 launch.py test --hardware agents/robot_01_amr.json      # real ONIX base, simulated arm and QC
python3 launch.py test --hardware agents/qc_01_scanner.json     # real QC chain over MQTT; on the scanner PC:
python3 launch.py scanner qc_01 --test                          #   bridge on the test topics (sim/qc/qc_01/...)
```
This is the safest way to bring up one machine under real scheduler control.

**No robot at hand?** `tools/onix_simulator.py` speaks the ONIX API (same
ports and messages) and drives a virtual robot on your map:
```bash
python3 tools/onix_simulator.py --map amr_tools/maps/live_map.json --ports 3000 4000 7000
# then, in agents/robot_01_amr.json, set "host": "127.0.0.1" (on a copy) and use it like the real robot
```

## 6. REAL mode: deployment

Run `python3 launch.py check robot_01` first and keep someone at the e-stop.

### 6.1 Central server
```bash
python3 launch.py server             # scheduler + reasoning agent
python3 launch.py server --no-llm    # scheduler only
```
### 6.2 Robot PC (the computer riding on robot_01)
```bash
python3 tools/amr_stations.py check  # every location has x/y/theta? (3.4)
python3 launch.py robot robot_01     # starts every agents/robot_01_*.json (amr + arm)
```
This PC needs network access to the ONIX robot (`172.22.74.119`, TCP 3000,
4000 and 7000), the Dobot controller (TCP 29999) **and** EMQX. The ONIX API is
only reachable from the robot's own network, so this is usually the Dobot's
computer riding on the base, with a second interface (Wi-Fi/LTE) to the
factory network, or a small gateway PC with two interfaces.

### 6.3 Quality-check station (MQTT only)
The QC chain has two halves that only talk through EMQX:
```
 central server: robot_agent (qc_01, mqtt_inspection) ── qc/qc_01/request ──▶  scanner PC: scanner_bridge.py ─▶ your vision program
                                                      ◀── qc/qc_01/result ───                (heartbeat on qc/qc_01/status)
```
```bash
# central server (or any PC with EMQX access)
python3 launch.py agents agents/qc_01_scanner.json

# scanner PC: the vision program does ONE inspection and prints PASS / FAIL
# (or JSON {"pass": true, ...}); it gets QC_PRODUCT, QC_STATION, QC_REQUEST_ID.
python3 launch.py scanner qc_01 --command "python3 /opt/vision/check.py"
python3 launch.py scanner qc_01            # same, with SCANNER_COMMAND from .env

# no camera yet: stand-in that passes every part (instead of the two above)
python3 launch.py agents agents/qc_01_stand_in.json
```
A camera or PLC that can speak MQTT itself can skip `scanner_bridge.py`: it
subscribes to `qc/qc_01/request` and answers on `qc/qc_01/result` with the same
`request_id` (contract in section 9). Without a pass/fail verdict within
`timeout_s`, the inspection **fails**; it never passes by default.
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
sudo systemctl enable --now robot-fleet-agent@qc_01_scanner     # QC agent (or @qc_01_stand_in)
sudo systemctl enable --now robot-fleet-scanner@qc_01           # scanner PC (SCANNER_COMMAND in .env)
journalctl -u robot-fleet-server -f                             # logs
```
**Windows.** Use `deploy\windows\server.bat`, `robot.bat robot_01`,
`scanner.bat qc_01`, `test.bat` and `check.bat`. To start one at boot, create a Task Scheduler task
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
their retained messages and sessions afterwards. They include a full job on
robot_01 as deployed: the `onix` driver against the ONIX simulator, the
`dobot` driver against a fake controller, and QC through `scanner_bridge.py`.

---

## 8. How it works, end to end

### 8.1 One job, step by step
```
 time ─────────────────────────────────────────────────────────────────────────────────────────────▶
 reasoning   prompt ─▶ Ollama ─▶ JSON ─▶ validate ─▶ (operator confirms) ─▶ scheduler/instructions
 scheduler   validate (+ every location has x/y/theta) ─▶ decompose (6 steps + 2 branches) ─▶ ALLOCATE carrier ─▶ dispatch step 1
 robot_01    amr: navigate→manufacturing_1   arm: pick   amr: navigate→quality_station
 qc_01                                                                     quality_check → pass/fail (MQTT ⇄ scanner PC)
 robot_01                                                                      amr: navigate→package_area|waste_area
 robot_01                                                                      arm: place ─▶ robot released
 scheduler   every result: verify ─▶ mark done ─▶ dispatch next step ─▶ ... ─▶ task_completed event
```
Each `navigate` step: look up x, y, theta in `stations.json` → send it to the
ONIX → follow its live pose → **done only when verified at the place** → the
next step is dispatched. The arm additionally checks that its base reports
being at that place before it moves (8.8).

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
   `scheduler/instructions`. It also checks that:
   - at least one QC station works in the requested QC zone (otherwise the
     carrier would wait there forever);
   - every location of the job (source, QC, both destinations) has
     **x, y, theta on at least one mobile base**. The bases report which
     locations have coordinates in their heartbeat (from `stations.json`).
     A job with an untaught location is rejected with
     `no mobile base has coordinates for [...]`, instead of failing halfway
     with a part on board. Bases that don't report locations (simulated,
     other vendors) are not checked.

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
   - it may work in the step's zone (`"*"` = anywhere; QC stations only in their own zone);
   - for the carrier: its base has **coordinates for every location of the
     job**, because it keeps the job until the final place.

   Among the robots left, it picks the one with the **most battery**, marks it
   `busy`, and releases the lock. Two jobs arriving at the same moment can
   never get the same robot.
3. If nothing qualifies, the step joins a **FIFO waiting queue**, and a
   `subtask_queued` event is published once. The queue is retried:
   - after every robot result;
   - whenever a heartbeat changes whether a robot can take work (charged, e-stop released, came online, a location was taught);
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
   - the **interlock** says no: the sibling subsystem is busy or offline, the
     mounted arm isn't in its travel pose, or (for the arm) the base is not
     at the task's location.
3. Otherwise it publishes a heartbeat with `busy=true` (so the sibling sees it
   at once), then `accepted`. It runs the driver in a **worker thread**, so
   the MQTT connection stays alive and a cancel can arrive mid-motion:
   - **ONIX mobile robot (`onix`), navigate to a location:**
     1. **Fetch the coordinates.** Read `stations.json` fresh and look up the
        location's x, y, theta and its tolerances (default 0.10 m / 5°). No
        pose → `failed` with "assign or teach it".
     2. **Pre-checks** on the live status (`ws://<robot>:7000/status`, channel
        7004): no e-stop, localization ≥ `min_localization` (70 %), no robot
        error, not already moving.
     3. **Send the goal** over the robot's API: `free_nav {linear_x: x,
        linear_y: y, angular_z: theta}` on `ws://<robot>:4000/nav` (or
        `path_nav` to the linked map station with `"nav_mode": "station"`). A
        rejected goal fails at once.
     4. **Follow the live pose** (~1 Hz stream) and declare the move **done
        only when verified**: within `xy_tolerance_m` and `yaw_tolerance_deg`
        of the target, linear and angular speed ≈ 0, and still localized,
        **continuously for `settle_s`** (1.5 s). The result carries the final
        pose and the error (`error_m`, `error_deg`).
     5. **Fail, and cancel the goal,** on: e-stop; localization lost; a robot
        error lasting `error_grace_s`; no movement within `start_timeout_s`
        (goal ignored); stopped short of the target for `stall_timeout_s`
        (blocked); no status for `max_comm_loss_s`; `mission_timeout_s`; or
        a cancel from the scheduler.
   - **Dobot (`dobot`):**
     1. Check the arm is enabled and idle (`RobotMode()` = 5).
     2. Write 0 to output register 0.
     3. `RunScript("<project>")`.
     4. Watch `RobotMode()` until the project ends. On an alarm or collision,
        send `Stop()`.
     5. Read register 0 (1 = success, 2 = failure, 0 = no result → failure).
     6. Check the arm is back in its **travel pose**.
   - **QC over MQTT (`mqtt_inspection`):**
     1. Publish `{"request_id": "<subtask>#<attempt>", "product", "station"}`
        on `qc/<station>/request`.
     2. Wait for the answer with **that** `request_id` on `qc/<station>/result`.
        Answers to other or older requests are ignored.
     3. Only an explicit verdict counts (`pass: true/false`, or `OK`/`NG`,
        `PASS`/`FAIL`). An `error` answer, an unknown verdict, or no answer
        within `timeout_s` is a **failed** inspection (the message says if the
        scanner's heartbeat is missing or old), never a pass.
4. It publishes a fresh heartbeat (`busy=false`, travel pose, the base's
   current location), **then** the retained result: `done` (with `result`),
   `failed` (with `error`) or `cancelled`. So when the scheduler dispatches
   the next step, the sibling already knows where the base is.

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
  `robots/<id>/<sub>/state` with `online`, `ready`, `estop`, `battery`, `busy`,
  and depending on the driver:
  - arm: `travel_pose`;
  - base: `pose` (x, y, theta), `at_location` / `at_locations` (the
    location(s) it is at now, within their tolerances) and `locations` (the
    locations that have coordinates). After a verified arrival the base keeps
    reporting that location while it stays within `arrival_drift_m` /
    `arrival_drift_deg` of the verified pose. While driving it reports no
    location.
- Its MQTT **last will** (also retained) says `online:false` if the process or
  the network dies, so the scheduler stops allocating that robot within
  seconds.
- The base and arm agents of one robot watch each other's heartbeats. That is
  the software interlock:
  - the base will not drive while the arm is busy or not in its travel pose
    (`require_travel_pose`);
  - the arm will not move while the base is busy, and only picks/places
    when the base **reports it is at the task's location**
    (`"require_base_at_zone": "amr"`). A base that moved, was pushed or lost
    localization after arriving no longer reports the location, so the arm
    refuses with `interlock: base 'amr' is not at '<zone>'`.

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
| `robots/<id>/<sub>/state` | agent → all (retained, + last will) | `{online, ready, estop, battery, busy, current, detail, travel_pose?, pose?, at_location?, at_locations?, locations?}` |
| `qc/<station>/request` | QC agent → scanner | `{request_id: "<subtask_id>#<attempt>", subtask_id, product, station, ts}` |
| `qc/<station>/result` | scanner → QC agent | `{request_id, pass: true/false, details?}` or `{request_id, error}` |
| `qc/<station>/status` | scanner → all (retained, + last will) | `{online, detail, mode, ts}` every 5 s |

The ONIX robot itself is not on MQTT: the `amr` agent talks to it over its
own WebSocket/HTTP API (`ws://<robot>:4000/nav`, `ws://<robot>:7000/status`,
`http://<robot>:3000/map/live`).

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
| `stations`, `locations` (x, y, theta, tolerances), `defaults` | `stations.json` | where each location is on the AMR map (3.4) |
| driver settings, `interlock_with`, `require_travel_pose`, `require_base_at_zone` | `agents/*.json` | per-subsystem hardware binding |
| `SCANNER_COMMAND` | `.env` (scanner PC) | the inspection program for `launch.py scanner` |

**`onix` driver** (`agents/robot_01_amr.json`):

| Key | Default | Meaning |
|---|---|---|
| `host`, `ports` | –, `{rest: 3000, control: 4000, status: 7000}` | the robot's API |
| `stations_file` | `stations.json` | coordinates, re-read at every move |
| `nav_mode` | `point` | `point` = free nav to x, y, theta; `station` = path nav to the linked map station |
| `xy_tolerance_m`, `yaw_tolerance_deg` | 0.10, 5 | arrival tolerance (overridden by `stations.json` defaults / per location) |
| `settle_s` | 1.5 | must be in tolerance and stopped this long |
| `stationary_linear`, `stationary_angular` | 0.02 m/s, 0.02 rad/s | "stopped" |
| `min_localization` | 70 | % confidence required before and during a move |
| `start_timeout_s`, `stall_timeout_s`, `mission_timeout_s` | 15, 30, 280 | goal ignored / blocked / too long (keep below site.json `navigate`, 300) |
| `max_comm_loss_s`, `error_grace_s` | 15, 3 | status gap / robot error tolerated |
| `arrival_drift_m`, `arrival_drift_deg` | 0.05, 3 | how far the base may drift after arriving and still be "at" the location |
| `api_key` | the web app's key | only if your robot uses a different one |

**`mqtt_inspection` driver** (`agents/qc_01_scanner.json`): `station`,
`timeout_s` (30), `health_timeout_s` (20; scanner heartbeat age),
`pass_field` (`pass`), `pass_values`, `fail_values`, optional
`request_topic` / `result_topic` / `health_topic` (`null` = no heartbeat needed).

Ready-made configs for other vendors are in `agents/examples/`: MiR and Universal Robots.

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
| job rejected: `no mobile base has coordinates for [...]` | give those locations a pose: `tools/amr_stations.py assign/teach/set` (3.4) |
| arm refuses: `interlock: base 'amr' is not at '<zone>'` | the base moved, lost localization, or `stations.json` changed since it arrived. `tools/amr_stations.py where` shows what it reports |
| AMR: `ONIX not localized (..% < 70%)` | relocate the robot in the ONIX web app |
| AMR: `did not start moving ... (goal not accepted?)` | goal outside the map or unreachable, robot paused or in manual mode in the web app |
| AMR: `stopped 0.xx m / y deg away from ...` | blocked, or the tolerance is tighter than the robot's own goal tolerance: loosen `xy_tolerance_m`/`yaw_tolerance_deg` for that location |
| AMR: `cannot reach ONIX at ...` | the agent PC is not on the robot's network (TCP 4000/7000) |
| QC: `no result from the scanner ... (no scanner heartbeat)` | start `launch.py scanner qc_01` on the scanner PC; check `--test` matches |
| QC: `scanner error: ...` | the inspection program failed or printed something other than PASS/FAIL/JSON |
| Dobot: `travel_joints must be a list of 6 ...` | fill in `travel_joints` (read them with `tools/dobot_console.py --host <ip> "GetAngle()"`) |
| Dobot: commands refused after `RequestControl` | start the agent while the arm is disabled, or switch DobotStudio Pro to TCP mode (v3) |
| robot in `error` | inspect the robot (part on board?), then `fleetctl reset <robot>` |
| `simulated driver in REAL mode` | use `--test`, or the explicit stand-in config `agents/qc_01_stand_in.json` |

## 12. Files
| File | Role |
|---|---|
| `launch.py` | one command per machine role (`check`, `test`, `server`, `robot`, `agents`, `scanner`), supervises and restarts |
| `scheduler_service.py` | scheduler process: MQTT, 1 s watchdog, status every 5 s |
| `scheduler.py` | the scheduling logic: dispatch, results, retries, failure policy, operator commands, persistence |
| `robot_registry.py` | fleet status + live health + reported locations, atomic claims |
| `stations.py`, `stations.json` | station/location coordinates: load, save, match a pose to a location |
| `task_decomposer.py`, `validation.py` | job → steps; input validation |
| `robot_agent.py` | one subsystem's agent: dedupe, cancel, heartbeat/last will, interlock |
| `drivers/` | `onix.py` (ONIX AMR), `dobot.py`, `mqtt_inspection.py` (QC over MQTT), `sim.py` (+ `http_amr.py`, `mir.py`, `ur.py` for other vendors) |
| `reasoning_agent.py` | prompt → Ollama → validated job, optional confirmation |
| `mqtt_common.py`, `mqtt_client.py`, `config.py`, `modes.py` | MQTT plumbing, settings, run modes |
| `manual_runner.py`, `tools/fleetctl.py` | submit and follow jobs; operator console (`status`, `watch`, `prompt`, `confirm`, `cancel`, `reset`, `maintenance`) |
| `offline_demo.py`, `tools/run_sim_fleet.py` | dry run without a broker; simulated fleet for TEST mode |
| `tools/amr_stations.py` | maintain `stations.json`: pull the map, assign/teach/set locations, where, check |
| `tools/scanner_bridge.py` | scanner PC: MQTT ⇄ your inspection program |
| `tools/onix_simulator.py` | ONIX API simulator (desk testing, automated tests) |
| `amr_tools/` | the ONIX client programs you supplied (live map viewer, GUI, `maps/live_map.json`) |
| `tools/dobot_console.py`, `tools/har_summary.py`, `tools/api_watch.py` | commissioning helpers |
| `deploy/systemd/`, `deploy/windows/`, `deploy/emqx/` | start-on-boot units (server, robot, agent, scanner), Windows launchers, optional EMQX ACL |
| `docs/COMMISSIONING.md` | bring-up checklist |
| `docs/AMR_API_CAPTURE.md` | how the AMR API was found (kept for other web-dashboard robots; the ONIX API is now known) |
