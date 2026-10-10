# Commissioning on real robots

Work through these phases in order. Don't go on to the next phase until the
current one passes. Each phase adds one source of risk.

The examples assume a mobile robot driven through its web-dashboard API with a
Dobot Nova 2 on top, and an HTTP or TCP vision station for QC. For other vendors, write a driver (`drivers/base.py`)
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
- [ ] **Change the MQTT password that was committed to git** (`.env` and the
      defaults in the old `config.py`). Removing the file doesn't remove it from
      git history. Treat it as public.

## 1. Infrastructure

- [ ] Broker: install mosquitto on a wired, always-on host. Copy
      `deploy/mosquitto/fleet.conf` and `acl.example`. Create TLS certs and one
      account per role (`scheduler`, `reasoning`, `hmi`, and one account named
      after each robot_id, e.g. `robot_01`, `qc_01`).
- [ ] **NTP/chrony on every host** (scheduler, agent PCs, broker). Agents
      reject a task that arrives after its `expires_at`, so clocks must agree
      to within `clock_skew_tolerance_s` (default 5 s).
- [ ] Network: agent PC ↔ mobile robot API (its dashboard port), agent PC ↔
      Dobot controller (TCP 29999), agent PC ↔ QC station, everything ↔
      broker (8883). See "Network layout" in AMR_API_CAPTURE.md. Put the robots on a
      dedicated VLAN/SSID. Check Wi-Fi roaming along every route.
- [ ] Install on each host:
      ```bash
      sudo useradd -r -m -d /opt/robot-fleet fleet
      sudo -u fleet git clone <repo> /opt/robot-fleet && cd /opt/robot-fleet
      sudo -u fleet python3 -m venv venv && sudo -u fleet venv/bin/pip install -r requirements.txt
      sudo -u fleet cp .env.example .env && sudo -u fleet chmod 600 .env   # then edit
      ```

## 2. Simulated end-to-end (no hardware)

- [ ] `pytest` passes on the scheduler host. This includes the end-to-end tests
      over a real broker.
- [ ] `python3 scheduler_service.py` + `python3 tools/run_sim_fleet.py` +
      `python3 manual_runner.py` completes a job.
- [ ] `python3 tools/run_sim_fleet.py --fail-rate 0.2 --qc-pass-rate 0.5` for
      30 minutes with several jobs queued. Check with `tools/fleetctl.py
      status` that failures end in `error` + operator reset, and that nothing
      gets stuck `busy`.
- [ ] Kill and restart `scheduler_service.py` mid-job. It should resume
      (`Restored scheduler state ...` in the log).

## 3. Configure the site

- [ ] `site.json`: one entry per real location. Set timeouts from measured
      cycle times plus margin. The driver-level timeouts in `agents/*.json`
      (`mission_timeout_s`, `program_timeout_s`) must be a little **shorter**
      than the matching scheduler timeout, so the driver reports the clearer
      error first.
- [ ] `robots_metadata.json`: the real fleet, with QC stations' `zones` set to
      their location.

## 4. Mobile robot (web-dashboard API), alone

1. Capture the dashboard's API and fill in `agents/robot_01_amr.json` by
   following **[AMR_API_CAPTURE.md](AMR_API_CAPTURE.md)**.
2. In the dashboard, make a drop point for every location in `site.json`.
   Where the arm works, make the stop repeatable. Use the robot's precise
   docking/marker or reflector feature if it has one, or a mechanical
   locating fixture. A normal navigation stop is usually only accurate to a
   few centimetres.
3. Check:
   ```bash
   venv/bin/python robot_agent.py --config agents/robot_01_amr.json --check   # ready, battery, nav_state
   ```
4. With a person at the e-stop, drive to each location once:
   ```bash
   venv/bin/python robot_agent.py --config agents/robot_01_amr.json --test navigate manufacturing_1
   ```
   Also test: press the e-stop during `--test` (expect `failed`, and the goal
   must not resume when the e-stop is released). Press Ctrl+C during
   `--test` (expect the robot to stop).

## 5. Dobot Nova 2, alone

1. In DobotStudio Pro, note:
   - the **controller IP** (LAN1 is often `192.168.5.1`);
   - the **firmware generation**: DobotStudio Pro / controller **4.x → `"protocol": "v4"`**, 3.x → `"v3"`.

   On v3, set the robot to **TCP/IP secondary development** mode. On v4.6+,
   the agent sends `RequestControl()` itself, which the controller accepts
   only while the arm is **disabled**. So start the agent with the arm
   disabled, then enable it, or set `auto_enable`.
2. Make one **project** per (action, location), e.g.
   `pick_manufacturing_1`, `place_package_area`, `place_waste_area`.
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
   If the result channel is **not** usable on your controller, tell us. The
   project can report through a spare DO or Modbus instead.
5. With a person at the e-stop:
   `--test pick manufacturing_1`. Also test a missed pick (no part). It must
   come back `failed` with "reported failure", not `done`.

> Other vendors: example configs for MiR (`agents/examples/mir_amr.json`) and
> Universal Robots (`agents/examples/ur_arm.json`) are kept for reference.

## 6. QC station, alone

- [ ] Fill `agents/qc_01_scanner.json` (HTTP) or use the TCP example.
- [ ] `--check`, then `--test quality_check quality_station` with a known-good
      part and a known-bad part. Also unplug the camera: the result must be
      `failed`, never a pass.

## 7. One robot, full job, supervised

- [ ] Start the broker, `scheduler_service.py`, and the agents for robot_01
      (`amr`, `arm`) and qc_01. Put every other robot in maintenance
      (`tools/fleetctl.py maintenance robot_02 on`) or don't list it yet.
- [ ] `tools/fleetctl.py status` shows robot_01 and qc_01 `idle` and healthy.
- [ ] `tools/fleetctl.py watch` in one terminal, `manual_runner.py` in another.
      Run 20 jobs with an operator at the e-stop. Include deliberate failures:
      remove the part before pick, block the path, unplug the QC camera, and
      cut the robot's Wi-Fi for 10 s while it is driving.
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
- [ ] Install the systemd units from `deploy/` and enable them. Check that
      `systemctl stop robot-agent@robot_01_amr` while it drives stops the
      robot.

## Operations cheat-sheet

```bash
tools/fleetctl.py status                     # who is doing what, health problems
tools/fleetctl.py watch                      # live events
tools/fleetctl.py cancel <task_id>           # stop a job (robots stop their current action)
tools/fleetctl.py reset <robot_id>           # after a human has recovered a robot in 'error'
tools/fleetctl.py maintenance <robot_id> on  # take a robot out of rotation
journalctl -u task-scheduler -f              # scheduler log
journalctl -u robot-agent@robot_01_amr -f    # agent log
```

A robot in **`error`** may be holding a product. Check it physically before
resetting.
