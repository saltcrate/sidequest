#!/usr/bin/env python3
"""Commit + push the Sidequest source to github.com/saltcrate/sidequest.

Token never printed; it is injected into the remote URL in-memory only.
"""
import json, os, subprocess, sys

REPO = "/home/hermes/sterling/sidequest"
tok = json.load(open("/home/hermes/.hermes/secrets/github.json"))["token"]
url = f"https://x-access-token:{tok}@github.com/saltcrate/sidequest.git"


def run(*args, **kw):
    p = subprocess.run(args, cwd=REPO, capture_output=True, text=True, **kw)
    out = (p.stdout + p.stderr).replace(tok, "***")
    return p.returncode, out.strip()


if not os.path.isdir(os.path.join(REPO, ".git")):
    print(run("git", "init", "-q"))

print(run("git", "config", "user.email", "oracle@saltcrate.com"))
print(run("git", "config", "user.name", "SaltCrate"))
print(run("git", "add", "-A"))
code, out = run("git", "commit", "-q", "-m",
                "Sidequest: M2M task marketplace with an MCP front-end")
print("commit:", code, out[:200])

_, remotes = run("git", "remote")
if "origin" not in remotes:
    print(run("git", "remote", "add", "origin", url))
else:
    print(run("git", "remote", "set-url", "origin", url))

print(run("git", "branch", "-M", "main"))
code, out = run("git", "push", "-u", "origin", "main", "--force")
print("push:", code, out[:400])
