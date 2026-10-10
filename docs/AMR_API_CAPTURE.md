# Capturing the mobile robot's web API

The mobile robot is driven from a web dashboard that it hosts itself. That
page talks to an API on the robot, and the `http_amr` driver can use the same
API. This guide records what the page sends, so that the driver config can be
filled in without changing any code.

Plan about 45 minutes, with a second person watching the robot near its
e-stop.

## 0. First, try the easy way

- **Find the manufacturer.** Look for a label or nameplate on the chassis and
  for an "About", "Help" or "System" page in the dashboard. Also check the
  browser tab title and logo, and the login page footer. In DevTools, open
  any API request and look at the `Server` response header.
- **Ask the manufacturer or distributor for the "REST API / SDK
  documentation"** and the service login. Many AMR vendors send this on
  request. An official document is better than reverse engineering, because
  it lists the error codes and the fields that might change.
- **Do not try to guess or brute-force the SSH password.** It can lock the
  controller or void support, and the robot's safety configuration lives on
  that system. The web API is enough for this integration.

## 1. Set up the recording

1. Connect a laptop to the robot's network and open the dashboard in
   **Chrome or Edge**.
2. Press **F12**, open the **Network** tab, and tick **Preserve log** and
   **Disable cache**.
3. Keep a notepad open. Write down the **clock time** of each action below,
   because it makes the capture much easier to read.

## 2. Do these actions, in this order

| # | Action in the dashboard | What it reveals |
|---|---|---|
| 1 | Reload the page (Ctrl+R) and log in | login request, token or cookie |
| 2 | Leave it idle for 15 s | the status request it polls, plus idle values |
| 3 | Open the map / list of drop locations | location ids or names |
| 4 | Send the robot to drop location **A** and wait until it arrives | the **navigate** command, plus running → arrived values |
| 5 | Send it to location **B**, then press **Stop/Cancel** while it moves | the **cancel** command, plus the cancelled value |
| 6 | Send it to **B** again and let it arrive | confirms the pattern |
| 7 | Send it to a spot it cannot reach, if there is a safe way to do that | the failed value and error message |
| 8 | With the robot stopped, press the **physical e-stop** for 5 s, then release it | the e-stop field |
| 9 | Pause/resume and dock/charge, if those buttons exist | optional extras |
| 10 | Write down the battery % shown on screen | which field is battery, and its scale |

## 3. Save the capture

- In the Network tab, click the **download arrow ("Export HAR")**, or
  right-click the request list and choose **Save all as HAR with content**.
- Also open the **WS** filter. If you see a `ws://` connection, click it, open
  **Messages**, and check that you can see the traffic. Newer Chrome versions
  include it in the HAR. If you see `ws://<robot-ip>:9090` with messages like
  `{"op":"subscribe", ...}`, the robot runs **ROS with rosbridge**. That's a
  standard protocol and good news.

## 4. Summarize it (secrets are removed)

```bash
python3 tools/har_summary.py capture.har --host <robot-ip> -o summary.txt
```

`summary.txt` lists, with passwords, tokens and cookies replaced by `***`:
- the commands you clicked;
- the polled status endpoint with a sample reply;
- any WebSocket traffic.

Read it, then share **summary.txt** (not the raw `.har`, which contains your
password and session token) to get the config filled in.

## 5. Find the status fields live (read-only)

While someone drives the robot from the dashboard, run:

```bash
python3 tools/api_watch.py http://<robot-ip>/<status path> \
    --header "Authorization: Bearer <token>" --ignore time --ignore pose
```

It only sends GET requests and prints just the fields that change. Note:
- the **navigation state** values for idle, running, arrived, failed and
  cancelled;
- the field that holds the **current target** name or id;
- the **e-stop** field;
- the **battery** field. If it shows 0.87 for 87 %, set `battery_scale` to 100.

## 6. Fill in the config and test one move

Edit `agents/robot_01_amr.json`. Every `REPLACE` maps to something from steps 4
and 5. Put the dashboard login in `.env` as `AMR_USERNAME` and
`AMR_PASSWORD`. Delete the `login` block if the API doesn't need one.

```bash
python3 robot_agent.py --config agents/robot_01_amr.json --check
python3 robot_agent.py --config agents/robot_01_amr.json --run navigate manufacturing_1
```

Then test pressing Ctrl+C during `--run`. The robot must stop, because that
proves the cancel request works.

## Things to look out for

- **Ids vs names vs coordinates.** Some dashboards send a point name, some an
  id, some `x, y, theta`. Put whatever the page sends into `locations` (e.g.
  `{"id": 12}` or `{"x": 1.2, "y": 3.4, "theta": 0}`), and use the same keys
  as `{placeholders}` in `navigate.json`.
- **Task ids.** If the goto reply returns a task id, and the status lives
  under a per-task URL, mention it so the driver can follow that task.
- **WebSocket-only control.** If clicking "go" sends nothing in Fetch/XHR, the
  commands go over the WebSocket. A small WebSocket/rosbridge variant of the
  driver will be needed. The capture tells exactly what it needs to send.
- **Firmware updates** can change an unofficial API. Note the robot's software
  version, and re-run the `--check` / `--run` steps after any update.
- **Token lifetime.** The driver logs in again automatically on 401/403. If the
  token goes into a cookie instead of a header, remove `token_path` and the
  cookie is handled automatically.

## Network layout

The robot API is reachable only from the robot's own network, so the `amr`
agent must run on a computer that can reach **both** that network and the
MQTT broker on the central server. Typical options:

1. **The manipulator's computer**, if it rides on the mobile robot: cable or
   Wi-Fi to the robot network, plus a second interface (Wi-Fi/LTE) to the
   factory network. Run both `robot_01_amr` and `robot_01_arm` agents there.
2. **Put the robot on the factory Wi-Fi** (client mode), if its network
   settings allow it. Then the agent can run on the central server.
3. **A small gateway PC** with two network interfaces.

Check that the route survives Wi-Fi roaming along the whole path the robot
drives.
