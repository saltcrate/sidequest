"""Sidequest worker router.

Maps a task type to a handler and runs it. Phase 1 ships exactly one worker —
the SaltCrate oracle primitives (fetch / check_link / check_change / verify),
which are already the highest-demand machine-executable tasks and already have
a proven MPP rail behind them.

The router is deliberately tiny so Phase 2 (third-party workers) is a matter of
registering more handlers — or proxying the task to an external worker URL.
"""

from __future__ import annotations

import os

import engine

# Bounty floor per task type, in cents (card). Tempo/stablecoin counterpart is
# set by TEMPO_PRICE_RAW. Defaults sit at the Stripe card floor ($0.50).
DEFAULT_BOUNTY_CENTS = {
    "fetch": 50,
    "check_link": 50,
    "check_change": 50,
    "verify": 50,
}

WORKER_ID = os.environ.get("SIDEQUEST_WORKER_ID", "saltcrate-oracle")


class TaskArgError(ValueError):
    """Bad task arguments — the requester's fault, not the worker's."""


def _need(args: dict, key: str) -> str:
    v = args.get(key)
    if not isinstance(v, str) or not v.strip():
        raise TaskArgError(f"args.{key} is required (string)")
    return v.strip()


def _h_fetch(args: dict, state_db: str) -> dict:
    return engine.fetch(_need(args, "url"))


def _h_check_link(args: dict, state_db: str) -> dict:
    return engine.check_link(_need(args, "url"))


def _h_check_change(args: dict, state_db: str) -> dict:
    return engine.check_change(_need(args, "url"), db_path=state_db)


def _h_verify(args: dict, state_db: str) -> dict:
    return engine.verify(_need(args, "claim"), _need(args, "source_url"))


HANDLERS = {
    "fetch": _h_fetch,
    "check_link": _h_check_link,
    "check_change": _h_check_change,
    "verify": _h_verify,
}

SUPPORTED_TYPES = sorted(HANDLERS)


def is_supported(task_type: str) -> bool:
    return task_type in HANDLERS


def default_bounty_cents(task_type: str) -> int:
    return DEFAULT_BOUNTY_CENTS.get(task_type, 50)


def run(task_type: str, args: dict, *, state_db: str) -> dict:
    """Run one task. Raises TaskArgError for bad args, RuntimeError otherwise."""
    if not is_supported(task_type):
        raise TaskArgError(f"unsupported task type {task_type!r}; supported: {SUPPORTED_TYPES}")
    if not isinstance(args, dict):
        raise TaskArgError("args must be an object")
    return HANDLERS[task_type](args, state_db)
