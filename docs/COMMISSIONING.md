# Commissioning on real robots

Work through these phases in order. Don't go on to the next phase until the
current one passes. Each phase adds one source of risk.

The examples assume a MiR base + UR arm per mobile manipulator, and an HTTP or
TCP vision station for QC. For other vendors, write a driver (`drivers/base.py`)
and the phases stay the same.

---

## 0. Before anything moves

- [ ] **Risk assessment** for every cell the robots enter: pick-up, QC,
      packaging, waste and the paths between them. Cover the AMR (ISO 3691-4)
      and the arm on a moving base (ISO 10218-2, ISO/TS 15066 if people work
      nearby). The scheduler is **not** part of the safety function.
- [ ] **Hardware interlock between arm and base.** The base must not drive
      while the arm is outside its travel pose, and the arm must not move while
      the base is driving. Do this in the controllers: MiR I/O or PLC ↔ UR safety
      I/O. `robot_agent.py` has a *software* interlock (`interlock_with`), but
      that is defence in depth only.
- [ ] E-stops on both MiR and UR are wired and tested. Everyone in the area
      knows where they are.
- [ ] Reduced-speed / protective-field settings on the MiR for each zone.
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
- [ ] Network: agent PC ↔ MiR (HTTP 80), agent PC ↔ UR (TCP 29999, 502), agent
      PC ↔ QC station, everything ↔ broker (8883). Put the robots on a
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

## 4. MiR base, alone

1. In the MiR web UI, create one mission per location, e.g. *"Go to
   manufacturing_1"*. For stations where the arm works, end the mission with
   **docking to a VL/L-marker**. A plain "move to position" only gets the base
   to within several centimetres, which is not enough for a blind arm program.
2. Fill `agents/robot_01_amr.json` (host, mission GUIDs). Put the credentials
   in `.env` as `MIR_USERNAME` / `MIR_PASSWORD`.
3. Check:
   ```bash
   venv/bin/python tools/mir_missions.py --config agents/robot_01_amr.json   # all locations [OK]
   venv/bin/python robot_agent.py --config agents/robot_01_amr.json --check   # ready: true, battery
   ```
4. With a person at the e-stop, drive each location once:
   ```bash
   venv/bin/python robot_agent.py --config agents/robot_01_amr.json --test navigate manufacturing_1
   ```
   Also test: press the MiR e-stop during `--test` (expect `failed`), and press
   Ctrl+C during `--test` (expect the mission aborted on the robot).

## 5. UR arm, alone

1. On the pendant, write one program per (action, location): e.g.
   `pick_manufacturing_1.urp`, `place_package_area.urp`,
   `place_waste_area.urp`. Each program must:
   - start with `write_port_register(128, 0)`;
   - re-localise against the station marker (or the MiR docking marker) before
     it approaches;
   - check that the part is present in the gripper (gripper object-detect or a
     sensor), not just that the gripper closed;
   - end with `write_port_register(128, 1)` on success and
     `write_port_register(128, 2)` on any failure, then **end**. Don't leave
     it waiting on a popup;
   - return the arm to a safe travel pose before ending.
2. Put the pendant in **Remote Control** mode.
3. Check:
   ```bash
   venv/bin/python robot_agent.py --config agents/robot_01_arm.json --check
   venv/bin/python robot_agent.py --config agents/robot_01_arm.json --test pick manufacturing_1
   ```
   Test a missed pick (no part present). It must come back `failed` with
   "reported failure", not `done`.

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
      stations. MiR Fleet or limit-robots zones should manage right of way
      between bases. This scheduler does not do path or traffic planning.
- [ ] Install the systemd units from `deploy/` and enable them. Check that
      `systemctl stop robot-agent@robot_01_amr` while it drives aborts the
      mission.

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
