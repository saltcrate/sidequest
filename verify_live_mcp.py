#!/usr/bin/env python3
"""Verify the live Sidequest MCP endpoint (read-only + paid-gate check)."""
import json, urllib.request, urllib.error

BASE = "https://saltcrate-sidequest." + "fly" + "." + "dev" + "/mcp"

def mcp(method, params=None, mid=1, accept="application/json, text/event-stream", headers=None):
    body = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}).encode()
    h = {"content-type": "application/json", "accept": accept}
    h.update(headers or {})
    r = urllib.request.Request(BASE, data=body, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=45) as resp:
            raw = resp.read().decode(); sid = resp.headers.get("mcp-session-id"); code = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode(); sid = e.headers.get("mcp-session-id"); code = e.code
    if raw.startswith("event:"):
        raw = raw.split("data: ", 1)[1].strip()
    return code, json.loads(raw), sid

def call(method, params=None, mid=1):
    body = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}).encode()
    r = urllib.request.Request(BASE, data=body, headers={"content-type": "application/json", "accept": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=45) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

code, r, sid = mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "verify", "version": "1"}})
print("initialize:", code, "| server:", r.get("result", {}).get("serverInfo"), "| session:", bool(sid))

code, r = call("tools/list")
tools = [t["name"] for t in r.get("result", {}).get("tools", [])]
print("tools/list:", code, "|", len(tools), "tools ->", tools)

code, r = call("tools/call", {"name": "sidequest_pricing", "arguments": {}})
sc = r.get("result", {}).get("structuredContent", {})
print("pricing:", code, "| payment_methods:", sc.get("payment_methods"), "| types:", list(sc.get("task_types", {}).keys()))

code, r = call("tools/call", {"name": "sidequest_post_task",
                              "arguments": {"type": "check_link", "args": {"url": "https://saltcrate.com"}}})
task = r.get("result", {}).get("structuredContent", {})
print("post_task:", code, "|", task.get("id"), task.get("status"), "bounty:", task.get("bounty_cents"))

code, r = call("tools/call", {"name": "sidequest_get_task", "arguments": {"task_id": task.get("id")}})
print("get_task:", code, "|", r.get("result", {}).get("structuredContent", {}).get("status"))

# paid gate: run_task with no credential MUST return -32042 + challenges
code, r = call("tools/call", {"name": "sidequest_run_task",
                              "arguments": {"type": "fetch", "args": {"url": "https://example.com"}}})
err = r.get("error", {})
ch = (err.get("data") or {}).get("challenges", [])
print("run_task unpaid:", code, "| jsonrpc error:", err.get("code"), err.get("message"),
      "| challenges:", [c.get("method") for c in ch])
print("  httpStatus:", (err.get("data") or {}).get("httpStatus"))
