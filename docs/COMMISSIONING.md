# Commissioning on real robots

Work through these phases in order. Don't go on to the next phase until the
current one passes. Each phase adds one source of risk.

The examples assume the ONIX mobile robot with a Dobot Nova 2 on top, and the
QC conveyor from the [conveyor agent](https://github.com/raghulrajg/conveyor_agent)
(Arduino Opta + Hikrobot SC3000) as the QC station. For other vendors, write a driver (`drivers/base.py`)
and the phases stay the same.

---

## 0. Before anything moves

- [ ] **Risk assessment** for every cell the robots enter: pick-up, QC,
      packaging, waste and the paths between them. Cover the AMR (ISO 3691-4)
      and the arm on a moving base (ISO 10218-2, ISO/TS 15066 if people work
      nearby). The scheduler is **not** part of the safety function.
- [ ] **Hardware interlock between arm and base.** The base must not drive
      while the arm is outside its travel pose, and the arm must not move while
      the base is driving. Do this in the controllers: wire the Dobot's
      safety I/O to the mobile robot's safety/stop input, or use a safety PLC. `robot_agent.py` has a *software* interlock (`interlock_with`), but
      that is defence in depth only.
- [ ] **Arm on a mobile base.** One e-stop chain must stop *both* the base and
      the arm. The base's "moving" signal should hold the arm in a safe
      stop/reduced mode, and the arm's "outside travel pose" signal should
      inhibit base motion. Use the robot's I/O settings (often in its web
      dashboard) and the Dobot's safety I/O, or a small safety relay/PLC.
      Check the base's payload and stability with the arm extended and
      loaded at full reach.
- [ ] E-stops on both the mobile robot and the Dobot are wired and tested. Everyone in the area
      knows where they are.
- [ ] Reduced-speed / protective-field settings on the mobile robot for each zone.
- [ ] **Change the MQTT password that was committed to git** (`.env`, the
      defaults in the old `config.py`, and the conveyor agent's sketches and
      `mqtt_monitor.py`). `.env` is no longer tracked, but the old one stays in
      git history. Treat that password as public, and put the new one only in
      each machine's `.env` and in the Opta sketch you upload.

## 1. Infrastructure

- [ ] Broker: your **EMQX** (172.25.200.57:1883). Change the password that was
      published on GitHub (EMQX Dashboard → Access Control → Authentication →
      Users). Optional hardening: one user per role and the rules in
      `deploy/emqx/acl.conf.example`.
- [ ] **NTP/chrony on every host** (scheduler, agent PCs, broker). Agents
      reject a task that arrives after its `expires_at`, so clocks must agree
      to within `clock_skew_tolerance_s` (default 5 s).
- [ ] Network: robot PC ↔ ONIX robot (`172.22.74.119`, TCP 3000, 4000, 7000),
      robot PC ↔ Dobot controller (TCP 29999), **Opta (Wi-Fi) ↔ broker**,
      everything ↔ broker (EMQX 1883, or 8883 with TLS). The Opta sketch has
      its own broker address (`MQTT_BROKER_IP`, default 172.16.13.62): it must
      be the same EMQX as `MQTT_BROKER_HOST` in `.env`. The ONIX API is only on the
      robot's own network, so the robot PC needs an interface on it **and**
      one on the factory network (or use a two-interface gateway PC). Put the
      robots on a dedicated VLAN/SSID. Check Wi-Fi roaming along every route.
- [ ] Install on each host:
      ```bash
      sudo useradd -r -m -d /opt/robot-fleet fleet
      sudo -u fleet git clone <repo> /opt/robot-fleet && cd /opt/robot-fleet
      sudo -u fleet python3 -m venv venv && sudo -u fleet venv/bin/pip install -r requirements.txt
      sudo -u fleet cp .env.example .env && sudo -u fleet chmod 600 .env   # then edit
      sudo -u fleet venv/bin/python launch.py check                        # preflight
      ```

## 2. Simulated end-to-end (no hardware)

- [ ] `python3 offline_demo.py` passes all scenarios (no broker needed).
- [ ] `pytest` passes on the scheduler host. Point the broker tests at your
      EMQX with `TEST_MQTT_HOST/PORT/USERNAME/PASSWORD`. They run in their own
      `pytest/<id>/` topic namespace and clean up after themselves.
- [ ] `python3 launch.py test --jobs 5` completes 5 jobs on your EMQX, with
      simulated robots under the `sim/` topics.
- [ ] `python3 launch.py test --conveyor --jobs 5 --qc-pass-rate 0.5`: the same
      through the real conveyor driver and a simulated Opta; jobs end in
      `package_area` or `waste_area`.
- [ ] `python3 launch.py test --fail-rate 0.2 --qc-pass-rate 0.5` for 30
      minutes with jobs submitted regularly (`tools/fleetctl.py --test submit`).
      Check with `tools/fleetctl.py --test status` that failures end in `error`
      plus an operator reset, and that nothing gets stuck `busy`.
- [ ] Stop and restart the test stack mid-job. It should resume
      (`Restored scheduler state ...` in the log).

## 3. Configure the site

- [ ] `site.json`: one entry per real location. Set timeouts from measured
      cycle times plus margin. The driver-level timeouts in `agents/*.json`
      (`mission_timeout_s`, `program_timeout_s`) must be a little **shorter**
      than the matching scheduler timeout, so the driver reports the clearer
      error first.
- [ ] `robots_metadata.json`: the real fleet, with QC stations' `zones` set to
      their location.

## 4. ONIX mobile robot, alone

1. Set the robot IP in `agents/robot_01_amr.json` (`"host"`). Check the
   robot PC reaches it: `python3 tools/amr_stations.py where` prints the pose
   and localization.
2. **Coordinates for every location** (`stations.json`, README 3.4):
   ```bash
   venv/bin/python tools/amr_stations.py pull            # stations from the live map
   venv/bin/python tools/amr_stations.py list
   venv/bin/python tools/amr_stations.py assign manufacturing_1 LM1   # if LM1 really is that place
   # drive the robot to each remaining place in the ONIX web app, then:
   venv/bin/python tools/amr_stations.py teach package_area
   venv/bin/python tools/amr_stations.py check           # must exit 0
   ```
   The shipped file links `manufacturing_1` → LM1 and `quality_station` → LM2.
   **That is an assumption**: confirm it on the map, or re-assign.
   Where the arm works, make the stop repeatable. Use a mechanical locating
   fixture or marker if you can, and let each Dobot project re-localise.
3. Arrival tolerance: start with 0.10 m / 5°. Drive to each place a few times
   and look at `error_m` / `error_deg` in the result. Tighten per location
   (`set ... --xy-tol 0.05 --yaw-tol 3`) only as far as the robot reliably
   reaches. If the robot stops outside the tolerance, the move fails after
   `stall_timeout_s` with `stopped 0.xx m away`.
4. Check:
   ```bash
   venv/bin/python robot_agent.py --config agents/robot_01_amr.json --check   # pose, localization, at_location
   ```
5. With a person at the e-stop, drive to each location once:
   ```bash
   venv/bin/python robot_agent.py --config agents/robot_01_amr.json --run navigate manufacturing_1
   ```
   Expect `done` with `"verified": true` and a small `error_m`. Also test:
   - press the e-stop during `--run`: expect `failed`, and the goal must not
     resume when the e-stop is released;
   - press Ctrl+C during `--run`: expect the robot to stop;
   - block the path: expect `failed` (`stopped ... away`) and a cancel;
   - a location with no pose: expect `failed` (`assign or teach it`) and no motion.

## 5. Dobot Nova 2, alone

1. In DobotStudio Pro, note:
   - the **controller IP** (LAN1 is often `192.168.5.1`);
   - the **firmware generation**: DobotStudio Pro / controller **4.x → `"protocol": "v4"`**, 3.x → `"v3"`.

   On v3, set the robot to **TCP/IP secondary development** mode. On v4.6+,
   the agent sends `RequestControl()` itself, which the controller accepts
   only while the arm is **disabled**. So start the agent with the arm
   disabled, then enable it, or set `auto_enable`.
2. Make one **project** per (action, location), e.g.
   `pick_manufacturing_1`, `place_conveyor_entry` (part onto the belt at S1),
   `pick_conveyor_exit` (part off the belt at S3), `place_package_area`,
   `place_waste_area`.
   Blockly projects are addressed as `blockly_<name>`. Each project must:
   - **re-localise first** (camera, marker or fixture), because the base never
     stops in exactly the same place;
   - grasp, then **check the part is really in the gripper** (gripper feedback
     or a sensor on a tool DI);
   - finish in the **travel pose** (arm tucked in over the base);
   - report its verdict with `SetOutputInt(0, 1)` on success or
     `SetOutputInt(0, 2)` on failure, then end. Don't wait on a dialog.

   Sketch (adapt to the script syntax of your DobotStudio Pro version, or
   build the same in Blockly):
   ```lua
   SetOutputInt(0, 0)
   -- re-localise here (vision/marker -> user frame)
   MovJ(P_above)  MovL(P_pick)
   ToolDO(1, ON)                      -- close gripper (your gripper's I/O)
   -- wait ~0.3 s
   if ToolDI(1) == ON then            -- part present?
       MovL(P_above)  MovJ(P_travel)  SetOutputInt(0, 1)
   else
       ToolDO(1, OFF)  MovL(P_above)  MovJ(P_travel)  SetOutputInt(0, 2)
   end
   ```
3. **Travel pose (required: the arm rides on the base).** Choose a tucked-in
   pose with the arm and any held part close over the base, inside its
   footprint and low. That limits tipping and collisions while driving. Read
   its six joint angles with `tools/dobot_console.py --host <ip> "GetAngle()"`
   and put them in `travel_joints`. From then on:
   - a project only counts as done if it ends in that pose;
   - the base agent (`require_travel_pose: ["arm"]`) refuses to drive unless
     the arm positively reports it is there. "Unknown" counts as "not tucked
     in".

4. Check. First by hand, then through the driver. On v4.6+ the driver's
   check sends `RequestControl()`, which puts the controller in TCP mode:
   ```bash
   venv/bin/python tools/dobot_console.py --host 192.168.5.1 "RobotMode()" "GetErrorID()" "GetAngle()"
   venv/bin/python robot_agent.py --config agents/robot_01_arm.json --check
   ```
   Expect `"ready": true`, `"travel_pose": true` and `"result_channel": "ok"`.
   The arm agent also has `"require_base_at_zone": "amr"`: under scheduler
   control it only runs a project when the base reports it is at that
   project's location. (`--run` drives the driver directly and skips the
   agent's interlocks, so stand the base at the right place yourself.)
   If the result channel is **not** usable on your controller, tell us. The
   project can report through a spare DO or Modbus instead.
5. With a person at the e-stop:
   `--run pick manufacturing_1`. Also test a missed pick (no part). It must
   come back `failed` with "reported failure", not `done`.

> Other vendors: example configs for MiR (`agents/examples/mir_amr.json`) and
> Universal Robots (`agents/examples/ur_arm.json`) are kept for reference.

## 6. QC conveyor, alone

- [ ] Bring up the conveyor on its own first: the conveyor agent's README,
      "Test procedure" 1-3 (TCP link, deploy sketch with `fake_camera.py`, MQTT).
- [ ] In the deploy sketch you upload: `USE_MQTT = true`, `MQTT_BROKER_IP` /
      user / password = your EMQX (same as `.env`), `MQTT_TOPIC =
      "autonomous/product/Transformation"` (= `result_topic` in
      `agents/qc_01_conveyor.json`). Prefer `CAMERA_TRIGGER_MODE = TRIGGER_TCP`
      (one result per part).
- [ ] On the central server, watch the topic while a part runs through:
      `mosquitto_sub -h <broker> -u <user> -P <pw> -t autonomous/product/Transformation -v`.
      Every part gives exactly one message with `"result":"OK"` or `"NG"`.
- [ ] `venv/bin/python robot_agent.py --config agents/qc_01_conveyor.json --run quality_check quality_station`,
      then put a part on the belt within 30 s (`lookback_s`) or while it waits:
      expect `done` with `pass: true` for a good part and `pass: false` for a bad one.
      With no part, it must end `failed` after `timeout_s`, never a pass.
- [ ] Time the belt: from S1 to the verdict, and S1 to S3. The QC step's
      `timeout_s` (60) must comfortably cover entry-to-verdict.
- [ ] Teach the arm the two belt projects (phase 5) and check them with the base
      at `quality_station`: `--run place quality_station`, then
      `--run pick quality_station` once the part is at S3.

Using a camera PC with your own program instead (`agents/qc_01_scanner.json`):
`SCANNER_COMMAND` in `.env`, `python3 launch.py scanner qc_01`, then
`python3 launch.py check` shows `scanner bridge qc_01: online`.

## 7. One robot, full job, supervised

- [ ] Start the broker, `scheduler_service.py`, and the agents for robot_01
      (`amr`, `arm`) and qc_01 (`launch.py agents agents/qc_01_conveyor.json`). Put every other robot in maintenance
      (`tools/fleetctl.py maintenance robot_02 on`) or don't list it yet.
- [ ] `tools/fleetctl.py status` shows robot_01 and qc_01 `idle` and healthy.
- [ ] `tools/fleetctl.py watch` in one terminal, `manual_runner.py` in another.
      Run 20 jobs with an operator at the e-stop. Include deliberate failures:
      remove the part before pick, block the path, unplug the QC camera (the
      Opta faults with RESULT_TIMEOUT; the job fails with the part on the belt
      and the empty robot is released), and cut the robot's Wi-Fi for 10 s while
      it is driving.
- [ ] After each induced failure, confirm the robot shows `error` with a clear
      reason. Recover the product by hand, then run `tools/fleetctl.py reset
      robot_01`.

## 8. Add the LLM front end

- [ ] Run `reasoning_agent.py` with `REQUIRE_CONFIRMATION=1`. The HMI shows the
      parsed instruction from `hmi/feedback`, and the operator confirms on
      `hmi/confirm`.
- [ ] Try ambiguous, misspelled and nonsense prompts. They must come back on
      `hmi/feedback` with `ok: false` and never reach `scheduler/instructions`.

## 9. Scale out

- [ ] Add robots one at a time, repeating phases 4–7 for each.
- [ ] Run several jobs in parallel and watch for traffic conflicts at shared
      stations. The mobile robots' own fleet/traffic manager (if any) should
      manage right of way between bases. This scheduler does not do path or traffic planning.
- [ ] Install the systemd units from `deploy/systemd/` (`robot-fleet-server`,
      `robot-fleet-robot@robot_01`) and enable them. Check that
      `systemctl stop robot-fleet-robot@robot_01` while it drives stops the
      robot. The conveyor QC agent: `robot-fleet-agent@qc_01_conveyor` on the
      central server.

## Operations cheat-sheet

```bash
tools/fleetctl.py status                     # who is doing what, health problems
tools/fleetctl.py watch                      # live events
tools/fleetctl.py cancel <task_id>           # stop a job (robots stop their current action)
tools/fleetctl.py reset <robot_id>           # after a human has recovered a robot in 'error'
tools/fleetctl.py maintenance <robot_id> on  # take a robot out of rotation
journalctl -u robot-fleet-server -f          # scheduler + reasoning agent log
journalctl -u robot-fleet-robot@robot_01 -f  # robot_01 agents log
```

A robot in **`error`** may be holding a product. Check it physically before
resetting.
