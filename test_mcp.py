#!/usr/bin/env python3
"""Local MCP front-end test: boot the server in open mode, exercise /mcp."""
import json, os, subprocess, sys, time, urllib.request, urllib.error, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8099
env = dict(os.environ, SIDEQUEST_OPEN="1", PORT=str(PORT),
           SIDEQUEST_DB=os.path.join(tempfile.gettempdir(), "sq_test.db"),
           SIDEQUEST_STATE_DB=os.path.join(tempfile.gettempdir(), "sq_state_test.db"))
for p in (env["SIDEQUEST_DB"], env["SIDEQUEST_STATE_DB"]):
    if os.path.exists(p):
        os.remove(p)

proc = subprocess.Popen([sys.executable, "server.py"], cwd=HERE, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
base = f"http://127.0.0.1:{PORT}"
for _ in range(50):
    try:
        urllib.request.urlopen(base + "/health", timeout=1)
        break
    except Exception:
        time.sleep(0.2)

def mcp(method, params=None, mid=1, accept="application/json"):
    body = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}).encode()
    r = urllib.request.Request(base + "/mcp", data=body,
                               headers={"content-type": "application/json", "accept": accept})
    with urllib.request.urlopen(r, timeout=20) as resp:
        raw = resp.read().decode()
        sid = resp.headers.get("mcp-session-id")
    if raw.startswith("event:"):
        raw = raw.split("data: ", 1)[1].strip()
    return json.loads(raw), sid

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + str(extra)[:160]) if not cond else ""))
    if not cond:
        fails.append(name)

try:
    r, sid = mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
    check("initialize", "result" in r and r["result"]["serverInfo"]["name"] == "sidequest", r)
    check("session header", bool(sid), sid)

    r, _ = mcp("tools/list", accept="application/json, text/event-stream")
    names = [t["name"] for t in r["result"]["tools"]]
    check("tools/list has 6 tools", len(names) == 6, names)
    for n in ["sidequest_pricing", "sidequest_post_task", "sidequest_run_task",
              "sidequest_get_task", "sidequest_list_tasks", "sidequest_stats"]:
        check("tool " + n, n in names, names)

    r, _ = mcp("tools/call", {"name": "sidequest_pricing", "arguments": {}})
    check("pricing call", not r["result"].get("isError") and "task_types" in r["result"]["structuredContent"], r)

    r, _ = mcp("tools/call", {"name": "sidequest_list_tasks", "arguments": {"limit": 5}})
    check("list_tasks call", "tasks" in r["result"]["structuredContent"], r)

    r, _ = mcp("tools/call", {"name": "sidequest_post_task",
                              "arguments": {"type": "check_link", "args": {"url": "https://saltcrate.com"}}})
    task = r["result"]["structuredContent"]
    check("post_task", task.get("id", "").startswith("tq_") and task["status"] == "pending", task)

    r, _ = mcp("tools/call", {"name": "sidequest_get_task", "arguments": {"task_id": task["id"]}})
    check("get_task", r["result"]["structuredContent"]["id"] == task["id"], r)

    r, _ = mcp("tools/call", {"name": "sidequest_run_task",
                              "arguments": {"type": "fetch", "args": {"url": "https://example.com"}}})
    sc = r["result"]["structuredContent"]
    check("run_task (open mode) executes", sc.get("status") == "done" and sc.get("result"), sc)

    r, _ = mcp("tools/call", {"name": "sidequest_stats", "arguments": {}})
    check("stats", r["result"]["structuredContent"]["tasks_total"] >= 2, r)

    # unsupported type -> isError, not a crash
    r, _ = mcp("tools/call", {"name": "sidequest_run_task", "arguments": {"type": "nope", "args": {}}})
    check("bad type -> isError", r["result"].get("isError") is True, r)

    # unknown method
    r, _ = mcp("nope/method")
    check("unknown method -32601", r.get("error", {}).get("code") == -32601, r)

    # REST still works
    with urllib.request.urlopen(base + "/v1/pricing", timeout=10) as resp:
        check("REST /v1/pricing intact", json.loads(resp.read())["payment_methods"] is not None)
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

print("\n" + ("ALL PASS (" + str(0) + " failures)" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
