#!/usr/bin/env python3
"""Post-deploy verification for Sidequest: pricing floor + live 402 amounts."""
import json, urllib.request, urllib.error

B = "https://sidequest.saltcrate.com"


def req(path, method="GET", body=None):
    d = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + path, data=d, method=method,
                               headers={"content-type": "application/json",
                                        "user-agent": "jarvis-cron/1.0"})
    try:
        with urllib.request.urlopen(r, timeout=45) as x:
            return x.status, json.loads(x.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


s, b = req("/health")
print("health:", s, json.dumps(b)[:160])
s, b = req("/v1/pricing")
print("pricing:", s, json.dumps(b.get("task_types", b))[:260])
s, t = req("/v1/tasks", "POST", {"type": "check_link", "args": {"url": "https://saltcrate.com"}})
print("create:", s, t.get("bounty_cents"), t.get("id"))
if s == 201:
    s, b = req(f"/v1/tasks/{t['id']}/execute", "POST", {})
    amounts = [(c.get("method"), (c.get("request") or {}).get("amount")) for c in (b.get("challenges") or [])]
    print("unpaid execute:", s, "challenges:", amounts)
    s2, b2 = req(f"/v1/tasks/{t['id']}/cancel", "POST", {})
    print("cancel:", s2, b2.get("status"))
