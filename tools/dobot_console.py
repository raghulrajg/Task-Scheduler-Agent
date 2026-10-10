"""
tools/dobot_console.py
----------------------
Type Dobot TCP/IP commands by hand and see the raw replies -- the quickest
way to confirm the controller IP, firmware generation and TCP mode before
using the agent.

    python3 tools/dobot_console.py --host 192.168.5.1
    dobot> RobotMode()          -> 0,{5},RobotMode();   (5 = enabled & idle)
    dobot> GetErrorID()
    dobot> GetAngle()           -> joint angles, e.g. to fill travel_joints
    dobot> GetOutputInt(0)      -> -10000 means "unknown command" (v3 firmware)

Commands you type are sent as-is: motion and enable commands WILL move or
power the arm. Keep the e-stop within reach.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drivers.dobot import MODE_NAMES, DobotDashboard, DobotError  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="192.168.5.1")
    p.add_argument("--port", type=int, default=29999)
    p.add_argument("command", nargs="*", help="run these commands and exit, e.g. 'RobotMode()'")
    args = p.parse_args()
    dash = DobotDashboard(args.host, args.port)

    def send(cmd):
        try:
            err, values, raw = dash.cmd(cmd)
        except DobotError as e:
            print(f"!! {e}")
            return
        note = ""
        if cmd.lower().startswith("robotmode") and err == 0 and values.isdigit():
            note = f"   ({MODE_NAMES.get(int(values), '?')})"
        elif err == -10000:
            note = "   (command not supported by this firmware)"
        print(raw + note)

    if args.command:
        for c in args.command:
            send(c)
        return
    print(f"Connected to {args.host}:{args.port}. Ctrl+D to quit.")
    while True:
        try:
            line = input("dobot> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line:
            send(line)
    dash.close()


if __name__ == "__main__":
    main()
