#!/usr/bin/env python3
"""Health + state probe for the live Sidequest service. Reads URL from BASE env or default."""
import json, urllib.request, urllib.error, os, time

B = os.environ.get("SQ_BASE", "https://saltcrate-sidequest.fly.dev")

def req(p, method="GET", body=None, timeout=40):
    d = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + p, data=d, method=method,
                               headers={"content-type": "application/json",
                                        "accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as x:
            return x.status, x.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"ERR {type(e).__name__}: {e}"

print("=== SIDEQUEST LIVE PROBE ===")
for attempt in range(10):
    s, b = req("/health")
    if s == 200:
        break
    time.sleep(4)
print("/health:", s, b[:400])

s, b = req("/v1/stats")
print("/v1/stats:", s, b[:400])

s, b = req("/v1/tasks?limit=10")
print("/v1/tasks:", s, b[:600])

s, b = req("/v1/pricing")
print("/v1/pricing:", s, b[:300])
