"""
tools/api_watch.py
------------------
Poll ONE read-only status URL of the robot and print only the fields that
change. Run it while you drive the robot from its own web dashboard: the
fields that flip when the robot starts moving, arrives, gets e-stopped or
charges are exactly the ones the http_amr driver config needs.

    python3 tools/api_watch.py http://192.168.10.10/api/robot/status
    python3 tools/api_watch.py http://192.168.10.10/api/status \
        --header "Authorization: Bearer <token from the capture>" --interval 0.5
    python3 tools/api_watch.py http://192.168.10.10/api/status --cookie "session=<value>"

Only sends GET requests. It never commands the robot.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict


def flatten(obj: Any, prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list) and len(obj) <= 50:
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}.{i}" if prefix else str(i)))
    else:
        out[prefix] = obj
    return out


def diff(old: Dict[str, Any], new: Dict[str, Any]):
    for k in sorted(set(old) | set(new)):
        if old.get(k, "<absent>") != new.get(k, "<absent>"):
            yield k, old.get(k, "<absent>"), new.get(k, "<absent>")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url")
    p.add_argument("--header", action="append", default=[], help='"Name: value" (repeatable)')
    p.add_argument("--cookie")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--ignore", action="append", default=[],
                   help="substring of field names to ignore, e.g. timestamp, pose (repeatable)")
    args = p.parse_args()

    headers = {"Accept": "application/json"}
    for h in args.header:
        name, _, value = h.partition(":")
        headers[name.strip()] = value.strip()
    if args.cookie:
        headers["Cookie"] = args.cookie

    prev = None
    while True:
        try:
            req = urllib.request.Request(args.url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            print(f"{time.strftime('%H:%M:%S')} HTTP {e.code} (wrong/expired token or cookie?)", flush=True)
            time.sleep(args.interval)
            continue
        except (OSError, ValueError) as e:
            print(f"{time.strftime('%H:%M:%S')} error: {e}", flush=True)
            time.sleep(args.interval)
            continue
        flat = {k: v for k, v in flatten(data).items() if not any(s in k for s in args.ignore)}
        stamp = time.strftime("%H:%M:%S")
        if prev is None:
            print(f"{stamp} initial snapshot ({len(flat)} fields):")
            for k, v in sorted(flat.items()):
                print(f"    {k} = {v!r}")
            print("--- now use the web dashboard (start/stop a move, e-stop, dock...) and watch below ---",
                  flush=True)
        else:
            for k, old, new in diff(prev, flat):
                print(f"{stamp} {k}: {old!r} -> {new!r}", flush=True)
        prev = flat
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
