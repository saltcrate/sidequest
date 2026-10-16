#!/usr/bin/env python3
"""Sidequest MVP end-to-end test — real HTTP against a locally spawned server.

Exercises: health, pricing, create -> execute (open mode), 402 challenge path,
bad-arg path, cancel, ledger stats. Run: python3 test_mvp.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get("TEST_PORT", "8099"))
BASE = f"http://127.0.0.1:{PORT}"


def req(path: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def start_server(env_extra: dict) -> subprocess.Popen:
    env = os.environ.copy()
    tmp = tempfile.mkdtemp(prefix="sidequest-test-")
    env.update({
        "PORT": str(PORT),
        "SIDEQUEST_DB": os.path.join(tmp, "sq.db"),
        "SIDEQUEST_STATE_DB": os.path.join(tmp, "state.db"),
        "MPP_REALM": "test.local",
        **env_extra,
    })
    p = subprocess.Popen([sys.executable, "server.py"], env=env, cwd=os.path.dirname(os.path.abspath(__file__)),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(60):
        time.sleep(0.25)
        try:
            urllib.request.urlopen(BASE + "/health", timeout=2).read()
            return p
        except Exception:
            if p.poll() is not None:
                print("server died:", p.stdout.read() if p.stdout else "")
                raise SystemExit(1)
    raise SystemExit("server didn't come up")


def stop(p):
    p.terminate()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


def main() -> int:
    fails = []

    def check(name, cond, extra=""):
        print(("  PASS " if cond else "  FAIL ") + name + (f"  {extra}" if extra and not cond else ""))
        if not cond:
            fails.append(name)

    # ---- open mode ------------------------------------------------------- #
    print("== open mode ==")
    p = start_server({"SIDEQUEST_OPEN": "1"})
    try:
        s, b = req("/health")
        check("health 200 + ok", s == 200 and b.get("ok") is True, str(b))
        s, b = req("/v1/pricing")
        check("pricing lists 4 types", s == 200 and len(b["task_types"]) == 4, str(b))

        s, b = req("/v1/tasks", "POST", {"type": "check_link", "args": {"url": "https://example.com"}})
        check("create task 201", s == 201, str(b))
        tid = b.get("id")
        check("task pending", b.get("status") == "pending")

        s, b = req(f"/v1/tasks/{tid}/execute", "POST", {})
        check("execute 200", s == 200, str(b))
        check("executed ok + alive", b.get("status") == "done" and (b.get("result") or {}).get("alive") is True,
              str(b.get("result"))[:200])

        s, b = req(f"/v1/tasks/{tid}/execute", "POST", {})  # replay
        check("replay blocked 409", s == 409, str(b))

        s, b = req("/v1/tasks", "POST", {"type": "nope", "args": {}})
        check("unknown type 400", s == 400, str(b))

        s, b = req("/v1/tasks", "POST", {"type": "fetch", "args": {}})
        tid2 = b.get("id")
        s, b = req(f"/v1/tasks/{tid2}/execute", "POST", {})
        check("bad args 422", s == 422, str(b))

        s, b = req("/v1/tasks", "POST", {"type": "verify", "args": {"claim": "Example Domain is a domain", "source_url": "https://example.com"}})
        tid3 = b.get("id")
        s, b = req(f"/v1/tasks/{tid3}/execute", "POST", {})
        check("verify 200 + verdict", s == 200 and (b.get("result") or {}).get("verdict") in ("supported", "partial", "unsupported"),
              str(b.get("result"))[:200])

        s, b = req("/v1/tasks", "POST", {"type": "check_link", "args": {"url": "https://example.com"}})
        s2, b2 = req(f"/v1/tasks/{b['id']}/cancel", "POST", {})
        check("cancel pending 200", s2 == 200 and b2.get("cancelled") is True, str(b2))

        s, b = req("/v1/stats")
        check("stats counted done tasks", s == 200 and b["by_status"].get("done", {}).get("count", 0) >= 2, str(b))

        s, b = req("/v1/tasks?status=cancelled")
        check("board filter works", s == 200 and b["count"] >= 1, str(b))
    finally:
        stop(p)

    # ---- paid mode (challenge path, dummy key, no network settle) -------- #
    print("== paid mode (402 challenges) ==")
    p = start_server({"SIDEQUEST_OPEN": "0", "STRIPE_RESTRICTED_KEY": "rk_test_dummy",
                      "STRIPE_NETWORK_ID": "profile_test", "MPP_SECRET_KEY": "s3cr3t"})
    try:
        s, b = req("/health")
        check("paid mode health", s == 200 and b.get("payment_enabled") is True and b.get("open_mode") is False, str(b))
        s, b = req("/v1/tasks", "POST", {"type": "check_link", "args": {"url": "https://example.com"}})
        tid = b["id"]
        s, b = req(f"/v1/tasks/{tid}/execute", "POST", {})
        check("unpaid execute 402", s == 402, str(b))
        ch = b.get("challenges") or []
        check("has stripe challenge", any(c.get("method") == "stripe" for c in ch), str(ch)[:300])
        check("challenge has id+expires", all(c.get("id") and c.get("expires") for c in ch), str(ch)[:200])
        check("challenge amount 50", any((c.get("request") or {}).get("amount") == "50" for c in ch), str(ch)[:300])
        s, b = req(f"/v1/tasks/{tid}", "GET")
        check("task still pending", b.get("status") == "pending", str(b))
    finally:
        stop(p)

    print()
    if fails:
        print(f"FAILED ({len(fails)}): {fails}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
