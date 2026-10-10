"""
tools/har_summary.py
--------------------
Turn a browser network capture (HAR file) of the robot's web dashboard into
a readable, REDACTED summary of its API: which requests the page sends when
you click "go to point", which endpoint it polls for status, and whether it
uses WebSockets (and whether those look like ROS rosbridge).

How to record the HAR: see docs/AMR_API_CAPTURE.md.

    python3 tools/har_summary.py robot_capture.har                  # print summary
    python3 tools/har_summary.py robot_capture.har -o summary.txt   # save it (safe to share)
    python3 tools/har_summary.py robot_capture.har --host 192.168.10.10

Passwords, tokens, cookies and session ids are replaced with *** in the
output. Still read the output before sharing it.
"""

import argparse
import base64
import json
import re
import sys
from collections import OrderedDict
from typing import Any, Dict, List
from urllib.parse import parse_qsl, urlencode, urlsplit

SENSITIVE = re.compile(r"(pass(word)?|pwd|token|secret|auth(orization)?|cookie|session(id)?|api[_-]?key|"
                       r"access[_-]?key|credential|jwt|sid)$", re.I)
STATIC_TYPES = {"script", "stylesheet", "image", "font", "media", "manifest", "texttrack"}
STATIC_EXT = re.compile(r"\.(js|mjs|css|png|jpe?g|gif|svg|ico|woff2?|ttf|eot|map|webp|mp4|wasm)$", re.I)
MAX_BODY = 700


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if SENSITIVE.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def redact_text(text: str) -> str:
    """Redact a body that may be JSON, form-encoded or plain text."""
    if not text:
        return ""
    try:
        return json.dumps(redact(json.loads(text)), ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    if "=" in text and "&" in text or re.fullmatch(r"[\w.%-]+=[^\s]*", text or ""):
        pairs = parse_qsl(text, keep_blank_values=True)
        if pairs:
            return urlencode([(k, "***" if SENSITIVE.search(k) else v) for k, v in pairs])
    return re.sub(r"(?i)(bearer\s+)[\w.\-+/=]+", r"\1***", text)


def short(text: str, limit: int = MAX_BODY) -> str:
    return text if len(text) <= limit else text[:limit] + f" ...[{len(text) - limit} more chars]"


def body_of(entry: Dict[str, Any], side: str) -> str:
    if side == "request":
        return (entry["request"].get("postData") or {}).get("text") or ""
    content = entry["response"].get("content") or {}
    text = content.get("text") or ""
    if content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8", "replace")
        except ValueError:
            text = "<binary>"
    return text


def path_query(url: str):
    parts = urlsplit(url)
    query = [(k, "***" if SENSITIVE.search(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return parts.netloc, parts.path or "/", urlencode(query)


def is_static(entry: Dict[str, Any]) -> bool:
    if entry.get("_resourceType") in STATIC_TYPES:
        return True
    _, path, _ = path_query(entry["request"]["url"])
    return bool(STATIC_EXT.search(path))


def summarize(har: Dict[str, Any], host: str = None) -> str:
    entries = har.get("log", {}).get("entries", [])
    out: List[str] = []
    endpoints: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()
    commands, sockets = [], []
    t0 = None

    for e in entries:
        netloc, path, query = path_query(e["request"]["url"])
        if host and host not in netloc:
            continue
        if e.get("_resourceType") == "websocket" or e["request"]["url"].startswith(("ws://", "wss://")):
            sockets.append(e)
            continue
        if is_static(e):
            continue
        method = e["request"]["method"].upper()
        started = e.get("startedDateTime", "")
        t0 = t0 or started
        key = (method, netloc, path)
        ep = endpoints.setdefault(key, {"count": 0, "first": started, "samples": []})
        ep["count"] += 1
        if len(ep["samples"]) < 2:
            ep["samples"].append({
                "query": query, "status": e["response"].get("status"),
                "req": redact_text(body_of(e, "request")), "resp": redact_text(body_of(e, "response")),
                "req_type": (e["request"].get("postData") or {}).get("mimeType", ""),
                "auth_header": next((h["name"] for h in e["request"].get("headers", [])
                                     if h["name"].lower() in ("authorization", "x-token", "token")), None),
                "cookie": any(h["name"].lower() == "cookie" for h in e["request"].get("headers", [])),
            })
        if method != "GET":
            commands.append((started, method, netloc, path, redact_text(body_of(e, "request")),
                             e["response"].get("status"), redact_text(body_of(e, "response"))))

    out.append("=" * 78)
    out.append("ROBOT WEB API SUMMARY (redacted)")
    out.append("=" * 78)
    out.append(f"{len(entries)} requests in capture, {len(endpoints)} API endpoints, {len(sockets)} WebSocket(s)\n")

    out.append("-- COMMANDS (non-GET requests, in the order you clicked) " + "-" * 20)
    if not commands:
        out.append("  none -- if you clicked buttons, the UI probably sends commands over a WebSocket (see below)")
    for started, method, netloc, path, req, status, resp in commands:
        out.append(f"  [{started[11:23]}] {method} http://{netloc}{path}  -> HTTP {status}")
        if req:
            out.append(f"      request : {short(req, 400)}")
        if resp:
            out.append(f"      response: {short(resp, 300)}")

    out.append("\n-- ENDPOINTS (most-polled first; a frequently polled GET is usually the status) " + "-" * 0)
    for (method, netloc, path), ep in sorted(endpoints.items(), key=lambda kv: -kv[1]["count"]):
        out.append(f"\n  {method} http://{netloc}{path}    x{ep['count']}")
        for s in ep["samples"][:1]:
            if s["query"]:
                out.append(f"      query   : {s['query']}")
            auth = []
            if s["auth_header"]:
                auth.append(f"header '{s['auth_header']}'")
            if s["cookie"]:
                auth.append("cookie")
            out.append(f"      status  : HTTP {s['status']}" + (f"   auth: {', '.join(auth)}" if auth else ""))
            if s["req"]:
                out.append(f"      request : ({s['req_type']}) {short(s['req'], 400)}")
            if s["resp"]:
                out.append(f"      response: {short(s['resp'])}")

    out.append("\n-- WEBSOCKETS " + "-" * 64)
    if not sockets:
        out.append("  none")
    for ws in sockets:
        msgs = ws.get("_webSocketMessages") or []
        text = " ".join(str(m.get("data", ""))[:200] for m in msgs[:20])
        kind = ""
        if '"op"' in text and any(op in text for op in ('"subscribe"', '"publish"', '"call_service"', '"advertise"')):
            kind = "  <-- looks like ROS rosbridge (topics/services over JSON)"
        out.append(f"\n  {ws['request']['url']}   ({len(msgs)} messages captured){kind}")
        if not msgs:
            out.append("      (no messages in the HAR -- copy them from DevTools > Network > WS > Messages)")
        shown = {"send": 0, "receive": 0}
        for m in msgs:
            t = m.get("type", "?")
            if shown.get(t, 0) >= 6:
                continue
            shown[t] = shown.get(t, 0) + 1
            arrow = "->" if t == "send" else "<-"
            out.append(f"      {arrow} {short(redact_text(str(m.get('data', ''))), 300)}")

    out.append("\n-- NEXT STEP " + "-" * 65)
    out.append("  Map these into agents/<robot>_amr.json (driver type http_amr):")
    out.append("    login    = the POST that sends your username/password (if any)")
    out.append("    navigate = the command sent when you clicked 'go to <point>'")
    out.append("    cancel   = the command sent when you clicked stop/cancel")
    out.append("    status   = the most-polled GET; find the nav state / target / battery / e-stop fields")
    out.append("  Or send this file to Claude to fill in the config.")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("har")
    p.add_argument("-o", "--output")
    p.add_argument("--host", help="only include requests to this host/IP")
    args = p.parse_args()
    with open(args.har, "r", encoding="utf-8") as f:
        har = json.load(f)
    text = summarize(har, args.host)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"wrote {args.output}")
    else:
        sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
