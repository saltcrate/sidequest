"""Sidequest — MCP (Model Context Protocol) front-end.

Exposes the task board as MCP tools over *streamable HTTP* at ``POST /mcp`` so any
MCP client (Claude Desktop, Cursor, Claude Code, custom agents) can discover and
drive the marketplace without hand-rolling HTTP. The REST API is untouched; this
module is a thin, self-contained adapter over the exact same ledger / worker / MPP
primitives the REST routes use.

Tools
-----
  sidequest_pricing      supported task types + bounty floors (free)
  sidequest_post_task    post a task + bounty to the board (free)
  sidequest_get_task     fetch one task by id (free)
  sidequest_list_tasks   browse the public board (free)
  sidequest_stats        public ledger / reputation stats (free)
  sidequest_run_task     post + execute atomically; **paid** (MPP: card or USDC).
                         Unpaid -> JSON-RPC error ``-32042`` carrying the same
                         ``challenges`` the REST layer emits, so any MPP client
                         that already pays the SaltCrate Oracle works unchanged.

The payment rule is identical to the oracle: an agent passes
``params._meta["org.paymentauth/credential"]`` (or a ``credential`` tool argument)
carrying the echoed challenge + its payment proof.
"""

from __future__ import annotations

import json

import ledger
import worker
import payments as pay

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "sidequest", "title": "Sidequest", "version": "0.2.0"}

INSTRUCTIONS = (
    "Sidequest is a machine-to-machine task marketplace. Post a task with a bounty, "
    "a worker executes it, and settlement rides the MPP rail (Stripe card or Tempo "
    "USDC — no wallet required). Free tools: pricing, post_task, get_task, list_tasks, "
    "stats. The paid tool is run_task: it returns a JSON-RPC -32042 error with payment "
    "challenges when unpaid; echo the challenge back with your payment credential to "
    "settle and receive the result."
)

TOOLS = [
    {
        "name": "sidequest_pricing",
        "description": "List the task types Sidequest accepts, their bounty floors (card + USDC), and the payment methods enabled right now. Free.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sidequest_post_task",
        "description": "Post a task to the public board with a bounty. Unfunded + free — it becomes executable once someone (or run_task) pays. Returns the task id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "Task type: fetch | check_link | check_change | verify"},
                "args": {"type": "object", "description": "Task arguments, e.g. {\"url\": \"https://example.com\"}"},
                "requester": {"type": "string", "description": "Optional requester id (reputation seed)"},
                "bounty_cents": {"type": "integer", "description": "Optional bounty in cents (defaults to the floor)"},
            },
            "required": ["type", "args"],
        },
    },
    {
        "name": "sidequest_get_task",
        "description": "Fetch one task by id — status, worker, result, payment reference. Free.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "sidequest_list_tasks",
        "description": "Browse the public task board (most recent first). Filter by status or type. Free.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "pending | executing | done | failed | cancelled"},
                "type": {"type": "string", "description": "fetch | check_link | check_change | verify"},
                "limit": {"type": "integer", "description": "Max rows (1-500, default 20)"},
            },
        },
    },
    {
        "name": "sidequest_stats",
        "description": "Public ledger stats — tasks by status/type, gross bounty settled, workers. The reputation seed. Free.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "sidequest_run_task",
        "description": "Post AND execute a task atomically (the worker runs now and you get the result). Paid via MPP — card (Stripe) or USDC (Tempo). Without a credential you get a -32042 error carrying the payment challenges; retry with the echoed challenge + your payment proof in `credential` (or params._meta['org.paymentauth/credential']).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "fetch | check_link | check_change | verify"},
                "args": {"type": "object", "description": "e.g. {\"url\": \"https://example.com\"} or {\"claim\": \"...\", \"source_url\": \"...\"}"},
                "credential": {"type": "object", "description": "Echoed MPP challenge + payment proof (SPT for card, tx hash for USDC)"},
            },
            "required": ["type", "args"],
        },
    },
]


def _err(mid, code, message, data=None):
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": mid, "error": e}


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


class Router:
    """Stateless MCP request router. One instance per process, built from server config."""

    def __init__(self, *, ledger_mod, worker_mod, pay_mod, state_db,
                 payment_enabled, open_mode, realm, mpp_secret,
                 challenges_for, verify_or_challenge):
        self.ledger = ledger_mod
        self.worker = worker_mod
        self.pay = pay_mod
        self.state_db = state_db
        self.payment_enabled = payment_enabled
        self.open_mode = open_mode
        self.realm = realm
        self.mpp_secret = mpp_secret
        self.challenges_for = challenges_for
        self.verify_or_challenge = verify_or_challenge

    # -- tool implementations ------------------------------------------------ #
    def _t_pricing(self, a):
        return {
            "payment_methods": [
                m for m, on in (("stripe", self.payment_enabled and not self.open_mode),
                                ("tempo", self.payment_enabled and not self.open_mode))
                if on
            ],
            "currency_card": "usd",
            "task_types": {
                t: {"bounty_cents_card": self.worker.default_bounty_cents(t),
                    "worker": self.worker.WORKER_ID}
                for t in self.worker.SUPPORTED_TYPES
            },
            "docs": "https://saltcrate-sidequest.fly.dev/",
        }

    def _t_post(self, a):
        ttype = a.get("type")
        if not isinstance(ttype, str) or not self.worker.is_supported(ttype):
            raise ValueError(f"unsupported type {ttype!r}; supported: {self.worker.SUPPORTED_TYPES}")
        args = a.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError("args must be an object")
        bounty = a.get("bounty_cents")
        if not isinstance(bounty, int) or bounty <= 0:
            bounty = self.worker.default_bounty_cents(ttype)
        t = self.ledger.create(
            task_type=ttype, args=args, bounty_cents=bounty,
            requester=a.get("requester") if isinstance(a.get("requester"), str) else None,
        )
        t["next"] = f"POST /v1/tasks/{t['id']}/execute  (or call sidequest_run_task)"
        return t

    def _t_get(self, a):
        tid = a.get("task_id")
        t = self.ledger.get(tid) if isinstance(tid, str) else None
        if t is None:
            raise ValueError(f"task not found: {tid}")
        return t

    def _t_list(self, a):
        limit = a.get("limit")
        limit = limit if isinstance(limit, int) else 20
        tasks = self.ledger.list_tasks(
            status=a.get("status") if isinstance(a.get("status"), str) else None,
            task_type=a.get("type") if isinstance(a.get("type"), str) else None,
            limit=limit,
        )
        return {"tasks": tasks, "count": len(tasks)}

    def _t_stats(self, a):
        return self.ledger.stats()

    def _t_run(self, a, credential):
        ttype = a.get("type")
        if not isinstance(ttype, str) or not self.worker.is_supported(ttype):
            raise ValueError(f"unsupported type {ttype!r}; supported: {self.worker.SUPPORTED_TYPES}")
        args = a.get("args") or {}
        if not isinstance(args, dict):
            raise ValueError("args must be an object")
        bounty = a.get("bounty_cents")
        if not isinstance(bounty, int) or bounty <= 0:
            bounty = self.worker.default_bounty_cents(ttype)

        desc = f"{ttype} (Sidequest)"
        payment_method, payment_ref = "open", None
        if self.payment_enabled and not self.open_mode:
            if credential is None:
                specs = self.challenges_for(bounty, desc)
                return ("challenge", {
                    "httpStatus": 402,
                    "challenges": [self.pay.create_challenge(
                        request=s.request, realm=self.realm, secret_key=self.mpp_secret,
                        method=s.method, intent="charge", description=s.description,
                    ).to_dict() for s in specs],
                    "retry_with": ("call sidequest_run_task again with the echoed challenge "
                                   "+ your payment proof in `credential`"),
                })
            kind, payload, detail = self.verify_or_challenge(credential, bounty, desc)
            if kind != "receipt":
                return ("challenge", {
                    "httpStatus": 402,
                    "detail": detail,
                    "challenges": payload,
                    "retry_with": "echo the challenge + a valid payment proof as `credential`",
                })
            payment_method = (payload or {}).get("method", "stripe")
            payment_ref = (payload or {}).get("reference")

        t = self.ledger.create(task_type=ttype, args=args, bounty_cents=bounty, requester="mcp")
        claimed = self.ledger.claim(t["id"], self.worker.WORKER_ID)
        if claimed is None:
            raise ValueError("could not claim task")
        try:
            result = self.worker.run(ttype, args, state_db=self.state_db)
            return ("ok", self.ledger.complete(
                t["id"], result=result, payment_method=payment_method, payment_ref=payment_ref))
        except self.worker.TaskArgError as e:
            return ("ok", self.ledger.complete(
                t["id"], error=f"bad_args: {e}", payment_method=payment_method,
                payment_ref=payment_ref, db_path=None))

    # -- dispatch ------------------------------------------------------------ #
    def handle(self, msg: dict):
        """Handle one JSON-RPC message. Returns a response dict, or None for notifications."""
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            return _ok(mid, {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            })
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            meta = params.get("_meta") or {}
            credential = args.get("credential")
            if credential is None:
                credential = meta.get("org.paymentauth/credential")
            try:
                if name == "sidequest_pricing":
                    out = self._t_pricing(args)
                elif name == "sidequest_post_task":
                    out = self._t_post(args)
                elif name == "sidequest_get_task":
                    out = self._t_get(args)
                elif name == "sidequest_list_tasks":
                    out = self._t_list(args)
                elif name == "sidequest_stats":
                    out = self._t_stats(args)
                elif name == "sidequest_run_task":
                    kind, out = self._t_run(args, credential)
                    if kind == "challenge":
                        # MCP payment binding: -32042 carries httpStatus 402 + challenges
                        return _err(mid, -32042, "Payment Required", out)
                else:
                    return _err(mid, -32602, f"unknown tool: {name}")
            except ValueError as e:
                return _ok(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
            except Exception as e:  # worker/ledger failure — surface, don't crash the transport
                return _ok(mid, {"content": [{"type": "text", "text": f"error: {type(e).__name__}: {e}"}], "isError": True})
            return _ok(mid, self._wrap(out))
        return _err(mid, -32601, f"method not found: {method}")

    @staticmethod
    def _wrap(out):
        """MCP tool result: text summary + structuredContent for machine callers."""
        if isinstance(out, dict):
            text = json.dumps(out, indent=2)
        else:
            text = json.dumps(out, default=str)
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": out if isinstance(out, (dict, list)) else {"value": out},
        }
