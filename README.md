# Robot Task Scheduler

The scheduler turns a request like *"take ABCD from manufacturing_1, check its
quality, good parts to packaging, bad parts to waste"* into robot actions. It
assigns those actions to mobile manipulators (an **ONIX** mobile base with a
**Dobot Nova 2** mounted on it) and the quality-check **conveyor** (the
[conveyor agent](https://github.com/raghulrajg/conveyor_agent): Arduino Opta +
Hikrobot SC3000 camera), then supervises every step until the job is done. All
parts talk over your **EMQX** MQTT broker.

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
                                                   ├── robots/qc_01/scanner/task ─▶ robot_agent (qc_01, conveyor_inspection)
                                                   │                                    ▲ autonomous/product/Transformation
                                                   │                                    └── CONVEYOR: Arduino Opta + SC3000 camera
                                                   ▲                                        (one OK/NG verdict per part)
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
pip install -r amr_tools/requirements.txt   # only for the AMR desktop tools in amr_tools/ (GUI, live map viewer)
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

**Updating an install made before `.env` was untracked.** Older versions of
this repository tracked `.env`, so a plain `git pull` either deletes your
`.env` or refuses to run. Keep a copy across the update:
```bash
cd /opt/robot-fleet
sudo -u fleet cp -p .env .env.keep     # 1. keep your settings (git never touches .env.keep)
sudo -u fleet git checkout -- .env     # 2. let git remove its old copy
sudo -u fleet git pull                 # 3. update; if git complains about another local change, fix it and repeat 3
sudo -u fleet cp -p .env.keep .env     # 4. put your settings back
sudo -u fleet chmod 600 .env && sudo -u fleet rm .env.keep
```
If step 3 fails, your settings are still in `.env.keep`: do step 4 before
anything else, and never repeat step 1 in between. On Windows: `copy .env .env.keep`,
`git checkout -- .env`, `git pull`, `copy /Y .env.keep .env`, `del .env.keep`.
The broker password that used to be committed is public: change it in EMQX,
in every `.env` and in the Opta sketch.

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
| `agents/qc_01_conveyor.json` | `conveyor_inspection`: the **conveyor agent** (Opta + camera) over MQTT | the Opta's topic (`autonomous/product/Transformation`), timeout. **Use this one** (6.3) |
| `agents/qc_01_scanner.json` | `mqtt_inspection`: QC over MQTT with a request/answer bridge | station name (`qc_01`), timeouts, pass/fail values. The scanner PC runs `scanner_bridge.py` (6.3) |
| `agents/qc_01_stand_in.json` | `sim`: stand-in QC that **passes every part** | nothing. Use it until the conveyor is integrated |

Run only **one** `qc_01_*` agent at a time: they all serve the QC station `qc_01`.

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
python3 launch.py test --conveyor --jobs 3     # QC through the real conveyor driver + a simulated Opta
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
To run the **real conveyor** in a test, copy `agents/qc_01_conveyor.json`, set
`"namespaced": false` in the copy (the Opta always publishes on the real topic)
and pass the copy to `--hardware`. Never set it in the config you deploy.
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

### 6.3 Quality-check station: the conveyor
The QC station `qc_01` is the [conveyor agent](https://github.com/raghulrajg/conveyor_agent):
an Arduino Opta runs the belt (sensors S1 entry, S2 camera, S3 exit) and
publishes the camera verdict for every part on `autonomous/product/Transformation`:
```json
{"x":12.500,"y":30.200,"theta":45.000,"result":"OK","valid":true,"cycle":5,"raw":"12.5;30.2;45.0;OK;","product":"ABCD","switch_ok":true}
```

**Multiple products.** The camera has one program per product. For every part
the QC agent publishes the part's product name (the one in the operator's
message, e.g. "take **ABCD** from manufacturing_1 ...") on `qc/qc_01/request`.
The Opta needs it for **every** cycle: once S1 has detected the part it sends
`switch ABCD` to the camera over TCP and waits 10 s for `ok`, so the program
is normally loaded before the part reaches the camera. A job cancelled before
its part is on the belt never switches the camera. Without
`ok` it warns and carries on (`"switch_ok": false`); without a product name
for 30 s it faults (`NO_PRODUCT`). The Opta's warnings
(`autonomous/product/Warning`) are logged by the QC agent and forwarded to the
operator on `hmi/feedback`. A verdict for a different product than the part's
fails the inspection (the part is checked by hand).
Because the part rides the belt, `site.json` marks `quality_station` as a
conveyor, and a job runs like this at the station (8.3):
```
 robot_01 arm: place on the belt at S1 ─▶ Opta: S1 → S2, camera, publishes OK/NG ─▶ qc_01 agent: verdict
 robot_01 arm: pick from the belt exit S3 ─▶ robot_01 drives to package_area (OK) or waste_area (NG)
```
Run the QC agent on the central server (it only needs EMQX):
```bash
python3 launch.py agents agents/qc_01_conveyor.json
```
The Opta does not answer requests and sends no request id, so the agent takes
the **first verdict that arrives after the robot started putting this part
on the belt** (the scheduler sends that time as `not_before`); each verdict is
used once. To make that safe:
- **One job at the belt at a time.** A job's drive to the conveyor and its
  load wait (the robot keeps its part where it is) while another job uses the
  belt, or while `qc_01` is not idle and healthy. The QC
  step always goes to the conveyor's own station (`conveyor.station` in
  `site.json`), never to another QC robot in the zone.
- **Wait for the exit.** The Opta publishes the verdict while the part is at
  the camera (S2) and only then runs it to S3, so the agent reports the
  verdict `exit_travel_s` later (8 s; set it to your measured S2→S3 time plus
  a margin). Only then is the unload pick sent to the arm.
- **Fail closed.** No verdict within `timeout_s` (60 s) fails the inspection.
  An invalid camera message (`"valid": false`) is an **NG** verdict (the part
  goes to `fail_dest`), never a pass. A verdict that may belong to another part
  fails the inspection as **ambiguous** instead of routing the part: when
  two results arrive for one part, or when an earlier inspection never got
  its verdict (e.g. the Opta was offline and still holds it in its send queue).
- **A job that ends with its part on the belt** (QC failed, operator cancel,
  failed load or unload) puts `qc_01` in `error`, and nothing is loaded until
  a person has cleared the belt and run `tools/fleetctl.py reset qc_01`.
  **Before the reset**, check the Opta's Serial Monitor `STATUS` shows
  `mqtt=1 mqtt_queue=0`: the reset makes the QC agent forget verdicts it is
  still missing, so no old verdict may still be waiting in the Opta to be sent.

On the Opta (deploy sketch, see the conveyor agent's README):
- `MQTT_BROKER_IP` / `MQTT_USERNAME` / `MQTT_PASSWORD` must be **the same EMQX as
  `.env` here**, and `MQTT_TOPIC` must match `result_topic` in `agents/qc_01_conveyor.json`;
- `MQTT_PRODUCT_TOPIC` must be `qc/qc_01/request` (the QC agent's request, keep
  `announce: true`) and `MQTT_WARNING_TOPIC` must match `warning_topic`;
- each camera program is named exactly like the product, and SCMVS answers
  `ok` to `switch <program>`;
- the deploy sketch for your camera mode must have `USE_MQTT = true`;
- prefer `CAMERA_TRIGGER_MODE = TRIGGER_TCP` so the camera sends exactly one result per part;
- recommended sketch change: add `"age_ms"` (milliseconds since the result was
  queued) to the published JSON. The agent then dates each verdict by when the
  part was inspected, so a verdict sent late from the Opta's offline queue can
  never be taken for a newer part. The agent works without it.

The Dobot needs two more projects at the station (`agents/robot_01_arm.json`):
`place_conveyor_entry` (put the part on the belt at S1) and `pick_conveyor_exit`
(take it off at S3). `pick_conveyor_exit` should **wait until the part is at
S3** before it reaches in (wire the S3 sensor also to a Dobot DI, or add an
output to the Opta sketch that is on in `AT_EXIT`, and wait on it with a timeout), and must **check the grasp**, so
an empty gripper is reported as a failure (`SetOutputInt(0, 2)`), never as done.
If the arm cannot reach the exit from the same base
pose, add a location for it in `site.json` and `stations.json` and set
`"conveyor": {"station": "qc_01", "exit_location": "<that location>"}`: the base then drives there
before the pick. Inspecting in the gripper instead (no conveyor)? Remove
`"conveyor"` from `quality_station` in `site.json`.

**Alternatives** for `qc_01` (run one of them instead of the conveyor agent):
```bash
# a camera PC with your own inspection program: request/answer over MQTT
python3 launch.py agents agents/qc_01_scanner.json                       # central server
python3 launch.py scanner qc_01 --command "python3 /opt/vision/check.py"  # scanner PC; prints PASS / FAIL

# no camera yet: stand-in that passes every part
python3 launch.py agents agents/qc_01_stand_in.json
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
sudo systemctl enable --now robot-fleet-agent@qc_01_conveyor    # QC agent for the conveyor (central server)
# alternatives: robot-fleet-agent@qc_01_scanner (+ robot-fleet-scanner@qc_01 on the scanner PC) or @qc_01_stand_in
journalctl -u robot-fleet-server -f                             # logs
```
**Windows.** Use `deploy\windows\server.bat`, `agents.bat agents\qc_01_conveyor.json`,
`robot.bat robot_01`, `scanner.bat qc_01`, `test.bat` and `check.bat`. To start one at boot, create a Task Scheduler task
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
`dobot` driver against a fake controller, and QC through the `conveyor_inspection`
driver and a simulated Opta (and, once more, through `scanner_bridge.py`).
The tests never read your `.env`.

---

## 8. How it works, end to end

### 8.1 One job, step by step
```
 time ─────────────────────────────────────────────────────────────────────────────────────────────▶
 reasoning   prompt ─▶ Ollama ─▶ JSON ─▶ validate ─▶ (operator confirms) ─▶ scheduler/instructions
 scheduler   validate (+ every location has x/y/theta) ─▶ decompose (6 steps + 2 branches) ─▶ ALLOCATE carrier ─▶ dispatch step 1
 robot_01    amr: navigate→manufacturing_1   arm: pick   amr: navigate→quality_station   arm: place on belt
 qc_01                                                                          quality_check → OK/NG (conveyor, MQTT)
 robot_01                                                                          arm: pick from belt exit
 robot_01                                                                          amr: navigate→package_area|waste_area
 robot_01                                                                          arm: place ─▶ robot released
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
| `T-3` | navigate (**approach**: waits while another job uses the belt) | QC zone | navigate | carrier | no |
| `T-4` | place (**load** belt) | QC zone | place (manipulator) | carrier | no |
| `T-5` | quality_check | QC zone | quality_check | inspector | **yes** |
| (`T-6`) | navigate | `conveyor.exit_location` (only if set) | navigate | carrier | no |
| `T-6`/`T-7` | pick (**unload** belt) | QC zone or exit | pick (manipulator) | carrier | no |
| `T-nav-success` / `T-nav-fail` | navigate | success / fail dest | navigate | carrier | no |
| `T-place-success` / `T-place-fail` | place | success / fail dest | place (manipulator) | carrier | **yes** |

The load/unload steps exist because `site.json` marks the QC location as a
`conveyor`; without it the QC step follows `T-3` directly and the part is
inspected in the gripper. Each step depends on the previous one. Both
branches depend on the last common step (the unload pick), and the QC result
decides which branch runs. The other branch is never dispatched. The carrier
stays claimed while the belt runs, so the robot that loaded the part unloads it.

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
   - at a conveyor QC location: only the conveyor's own station (`conveyor.station`);
   - for the carrier: its base has **coordinates for every location of the
     job**, because it keeps the job until the final place.

   Among the robots left, it picks the one with the **most battery**, marks it
   `busy`, and releases the lock. Two jobs arriving at the same moment can
   never get the same robot.
3. If nothing qualifies, the step joins a **FIFO waiting queue**, and a
   `subtask_queued` event (with the reason) is published once. The drive to a
   conveyor and the load onto it also wait here while another job uses the
   belt or the conveyor's station is not idle and healthy. The queue is retried:
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
   - **Conveyor (`conveyor_inspection`):**
     1. Keep every message from the Opta's topic (`autonomous/product/Transformation`)
        as it arrives, with its arrival time (retained messages are ignored).
     2. Take the first unused verdict that arrived at or after the step's
        `not_before` (when the load step was dispatched, minus `clock_skew_s`),
        waiting up to `timeout_s` for it. Each verdict is used once. With
        `age_ms` in the message, "arrived" means "inspected".
     3. Wait until `exit_travel_s` after the verdict arrived (the part travels
        from the camera to the exit). A second result for the same part in the
        meantime makes the verdict **ambiguous** (failed).
     4. A verdict whose `product` is not the part's product is a **failed**
        inspection (and so is the retry): the camera ran another product's program.
        `switch_ok: false` (the camera did not confirm the program) is kept as a
        warning in the result details.
     5. `result` `OK` → pass, `NG` → fail, `valid: false` → fail; anything else,
        or nothing in time, is a **failed** inspection. If an earlier
        inspection ended without its verdict and that verdict has not turned
        up, the next verdict is **ambiguous** (failed), and so is the retry of
        that part: a person checks it. The verdict's x, y, theta and cycle are
        in the result and in the `qc_result` event.
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
  - A pick marks the part as **on board**; a place (also the load onto the
    conveyor) marks it as off board.
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
  - **QC station failure:** the station goes to `error` and the job is failed. The
    carrier goes to `error` if it holds the part; with a conveyor the part is on
    the belt, so the empty carrier is released and a human removes the part.
  - **Any job that ends with its part possibly on a conveyor** (from the load
    step until the unload pick is done: QC failure, cancel, failed load or
    unload) puts the conveyor's station in `error`: no part is loaded until a
    person has cleared the belt and run `fleetctl reset qc_01`.
  - **After the unload pick** a failed drive is never reassigned to another
    robot: the job is failed, as the part is no longer at the pick-up point.
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
| `hmi/feedback` | conveyor QC agent → HMI | `{request_id: null, ok: false, source: "conveyor", station, warning, product, detail, ts}` |
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
| `autonomous/product/Transformation` | conveyor (Opta) → QC agent | `{x, y, theta, result: "OK"/"NG", valid, cycle, raw, product, switch_ok}`, QoS 0, one per part |
| `autonomous/product/Warning` | conveyor (Opta) → QC agent | `{warning: "SWITCH_NO_ACK"/"NO_PRODUCT"/"PRODUCT_REJECTED"/"PRODUCT_STALE", product, cycle, state, detail}` |

With the conveyor, the QC agent also publishes `qc/<station>/request` when it starts
waiting. The Opta takes the part's product name from it and switches the camera
program; dashboards and `tools/conveyor_sim.py` read it too.

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
| `locations.<qc>.conveyor` | `site.json` | QC on a conveyor: `station` (its QC robot id, e.g. `qc_01`), `exit_location` if the exit needs another base pose |
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

**`conveyor_inspection` driver** (`agents/qc_01_conveyor.json`):

| Key | Default | Meaning |
|---|---|---|
| `result_topic` | `autonomous/product/Transformation` | `MQTT_TOPIC` in the Opta sketch |
| `namespaced` | `true` | add the run-mode prefix (`sim/` in TEST mode). `false` only to test with the real Opta |
| `verdict_field`, `pass_values`, `fail_values` | `result`, `["OK"]`, `["NG"]` | what the Opta sends |
| `invalid_is_fail` | `true` | `valid: false` = NG verdict (else a failed inspection) |
| `timeout_s` | 60 | from the QC dispatch |
| `exit_travel_s` | 0 (8 in the shipped config) | measured S2→S3 travel + margin; the verdict is reported this long after it arrived. Keep `timeout_s + exit_travel_s` below site.json `quality_check` (90) |
| `clock_skew_s`, `lookback_s` | 2, 30 | tolerance on `not_before`; window used when a step has none |
| `announce` | `true` | publish `qc/<station>/request` when waiting. **Keep `true`:** the Opta takes each part's product name from it |
| `warning_topic` | `autonomous/product/Warning` | `MQTT_WARNING_TOPIC` in the Opta sketch (`""` = off) |
| `forward_warnings` | `true` | forward the Opta's warnings to the operator on `hmi/feedback` |

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
| QC: `no conveyor result on autonomous/product/Transformation within 60s` | is the Opta on the same broker (`MQTT_BROKER_IP` in the sketch vs `.env`), `USE_MQTT = true`, WiFi connected? Watch the topic: `mosquitto_sub -h <broker> -u <user> -P <pw> -t autonomous/product/Transformation -v` or the conveyor repo's `python/mqtt_monitor.py`. Conveyor in FAULT (Opta Serial Monitor `STATUS`)? |
| conveyor part routed to waste with `reason: camera message was not x;y;theta;result` | the camera sent another format; check it with `SNIFF_ONLY` in the conveyor repo |
| QC: `ambiguous conveyor verdict (...)` | a verdict may belong to another part (Opta was offline, two results for one part). Check the part by hand, clear the belt, check the Opta `STATUS` shows `mqtt_queue=0`, then `fleetctl reset qc_01`. Camera in continuous mode? Use `TRIGGER_TCP` |
| job waits: `conveyor of qc_01 not free: ...` | another job is using the belt, or `qc_01` is in `error`/offline: after a failed or cancelled conveyor job, clear the belt, check the Opta `STATUS` shows `mqtt_queue=0`, then `fleetctl reset qc_01` |
| `reset qc_01` refused: `the conveyor of qc_01 is still in use` | a cancelled job's arm is still at the belt: wait for its robot to confirm the cancel, then clear the belt |
| arm: `no Dobot project configured for place at 'quality_station'` | add `place_conveyor_entry` / `pick_conveyor_exit` to `agents/robot_01_arm.json` (6.3) |
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
| `drivers/` | `onix.py` (ONIX AMR), `dobot.py`, `conveyor.py` (QC conveyor, Opta), `mqtt_inspection.py` (QC over MQTT), `sim.py` (+ `http_amr.py`, `mir.py`, `ur.py` for other vendors) |
| `reasoning_agent.py` | prompt → Ollama → validated job, optional confirmation |
| `mqtt_common.py`, `mqtt_client.py`, `config.py`, `modes.py` | MQTT plumbing, settings, run modes |
| `manual_runner.py`, `tools/fleetctl.py` | submit and follow jobs; operator console (`status`, `watch`, `prompt`, `confirm`, `cancel`, `reset`, `maintenance`) |
| `offline_demo.py`, `tools/run_sim_fleet.py` | dry run without a broker; simulated fleet for TEST mode |
| `tools/amr_stations.py` | maintain `stations.json`: pull the map, assign/teach/set locations, where, check |
| `tools/scanner_bridge.py` | scanner PC: MQTT ⇄ your inspection program |
| `tools/conveyor_sim.py` | simulated Opta for TEST mode (`launch.py test --conveyor`) |
| `tools/onix_simulator.py` | ONIX API simulator (desk testing, automated tests) |
| `amr_tools/` | the AMR agent you supplied: ONIX desktop app and CLI (`amr_agent.py`, GUI, live map viewer, `maps/live_map.json`); `pip install -r amr_tools/requirements.txt` |
| `tools/dobot_console.py`, `tools/har_summary.py`, `tools/api_watch.py` | commissioning helpers |
| `deploy/systemd/`, `deploy/windows/`, `deploy/emqx/` | start-on-boot units (server, robot, agent, scanner), Windows launchers, optional EMQX ACL (`deploy/mosquitto/` only if you run Mosquitto instead of EMQX) |
| `docs/COMMISSIONING.md` | bring-up checklist |
| `docs/AMR_API_CAPTURE.md` | how the AMR API was found (kept for other web-dashboard robots; the ONIX API is now known) |
