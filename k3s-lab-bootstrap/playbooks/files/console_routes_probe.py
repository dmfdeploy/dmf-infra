#!/usr/bin/env python3
"""Read the running console's changes routes from inside its own pod.

The verify summary runs this through ``kubectl exec -i deploy/dmf-cms --
python -I - [port]``. It sends GET requests over loopback to the console's
listening server, so the routes, their session check and the pod's runtime
state are the ones users reach. It signs a session cookie with the pod's own
``DMF_CONSOLE_SECRET_KEY``, in the format of Starlette's session middleware,
for a viewer identity that exists only in this process. The cookie and the key
are never printed. This proves the running routes answer; it does not prove a
person's sign-in through the identity provider.

Prints one JSON object: ``{"routes": {path: {"status", "body"} or {"error"}}}``
or ``{"error": ...}`` when no request could be made.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

ROUTES = ("/api/changes/commits", "/api/changes/pulls", "/api/changes/jobs")
TIMEOUT_SECONDS = 20


def base_path(raw):
    """Mirror the console's DMF_CONSOLE_BASE_PATH normalisation."""
    if not raw:
        return ""
    cleaned = "/" + raw.strip().strip("/")
    return "" if cleaned in ("/", "//") else cleaned


def session_cookie(secret):
    from itsdangerous import TimestampSigner

    identity = {
        "user": {
            "subject": "verify-summary-probe",
            "username": "verify-summary-probe",
            "display_name": "Verify summary probe",
            "role": "viewer",
            "groups": [],
        }
    }
    payload = base64.b64encode(json.dumps(identity).encode("utf-8"))
    return TimestampSigner(str(secret)).sign(payload).decode("utf-8")


def fetch(url, cookie):
    request = urllib.request.Request(url, headers={"Cookie": "session=" + cookie})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (OSError, ValueError) as exc:
        return {"error": "request failed: " + type(exc).__name__}
    try:
        return {"status": status, "body": json.loads(raw)}
    except ValueError:
        return {"status": status, "error": "reply is not JSON"}


def main(argv):
    secret = os.environ.get("DMF_CONSOLE_SECRET_KEY", "")
    if not secret:
        return {"error": "DMF_CONSOLE_SECRET_KEY is not set in the console pod"}
    try:
        cookie = session_cookie(secret)
    except ImportError:
        return {"error": "itsdangerous is not installed in the console image"}
    port = argv[1] if len(argv) > 1 else "8000"
    prefix = base_path(os.environ.get("DMF_CONSOLE_BASE_PATH", ""))
    return {
        "routes": {
            route: fetch("http://127.0.0.1:%s%s%s" % (port, prefix, route), cookie)
            for route in ROUTES
        }
    }


if __name__ == "__main__":
    print(json.dumps(main(sys.argv)))
