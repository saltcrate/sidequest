"""Sidequest — M2M task marketplace. MVP API server.

The wedge: a task board where an agent posts a job + bounty, a worker executes it,
and settlement rides the MPP rail (Stripe card/SPT **and** Tempo USDC — fiat-first,
no wallet required). Phase 1 ships one worker: the SaltCrate oracle primitives.

Endpoints
---------
GET  /                          service card (HTML for humans, JSON for agents)
GET  /health                    liveness + ledger counts
GET  /v1/pricing                supported task types + bounty floors
GET  /v1/stats                  public ledger stats (reputation seed)
POST /v1/tasks                  create a task (unfunded, status=pending)
GET  /v1/tasks                  list tasks (public board; ?status=&type=&limit=)
GET  /v1/tasks/{id}             fetch one task
POST /v1/tasks/{id}/execute     pay + run -> result  (402 w/ MPP challenges if unpaid)
POST /v1/tasks/{id}/cancel      cancel a pending task

Payment model (Phase 1, honest): the bounty is charged **at execute time** — pay on
delivery. Escrow (hold at post, release on verified completion) and third-party
workers land in Phase 2. The MPP challenge shape is identical to the oracle's
(``challenges`` list with stripe + tempo methods), so any MPP client that pays the
oracle can pay Sidequest unchanged.

Env
---
  STRIPE_RESTRICTED_KEY   rk_… (SPT PaymentIntent create+confirm) -> enables card
  STRIPE_NETWORK_ID       profile_…  (MPP networkId)
  MPP_SECRET_KEY          challenge-signing secret (else derived from the Stripe key)
  MPP_REALM               default saltcrate-sidequest.fly.dev
  TEMPO_DEPOSIT_ADDRESS   -> enables USDC
  TEMPO_PRICE_RAW         USDC price in raw units (6dp). Default "50000" = $0.05.
  TEMPO_CHAIN_ID          default 4217 mainnet
  SIDEQUEST_OPEN          "1" forces open (unpaid) mode — dev only.
  PORT / SIDEQUEST_DB / SIDEQUEST_STATE_DB
"""

from __future__ import annotations

import asyncio
import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

import ledger
import worker
import payments as pay
import mcp_http

PORT = int(os.environ.get("PORT", "8080"))
REALM = os.environ.get("MPP_REALM", "saltcrate-sidequest.fly.dev")
RESTRICTED_KEY = os.environ.get(pay.STRIPE_RESTRICTED_KEY)
NETWORK_ID = os.environ.get(pay.STRIPE_NETWORK_ID)
MPP_SECRET = os.environ.get(pay.MPP_SECRET_KEY) or (
    pay.derive_mpp_secret(RESTRICTED_KEY) if RESTRICTED_KEY else None
)

TEMPO_DEPOSIT_ADDRESS = os.environ.get(pay.TEMPO_DEPOSIT_ADDRESS)
TEMPO_PRICE_RAW = os.environ.get("TEMPO_PRICE_RAW", "50000")  # $0.05 USDC for Sidequest
TEMPO_CHAIN_ID = int(os.environ.get(pay.TEMPO_CHAIN_ID, str(pay.TEMPO_MAINNET_CHAIN_ID)))
TEMPO_CURRENCY = os.environ.get(pay.TEMPO_CURRENCY, pay.TEMPO_USDC)

STATE_DB = os.environ.get(
    "SIDEQUEST_STATE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.db"),
)

# Paid mode requires the Stripe rail. No key -> open mode (dev/local only).
OPEN_MODE = (os.environ.get("SIDEQUEST_OPEN") == "1") or (RESTRICTED_KEY is None)
PAYMENT_ENABLED = bool(RESTRICTED_KEY or TEMPO_DEPOSIT_ADDRESS)

# MCP front-end (POST /mcp). Built lazily after the challenge helpers below are
# defined, so it can reuse the exact same MPP wiring as the REST routes.
_mcp_router = None


def _get_mcp_router():
    global _mcp_router
    if _mcp_router is None:
        _mcp_router = mcp_http.Router(
            ledger_mod=ledger, worker_mod=worker, pay_mod=pay,
            state_db=STATE_DB, payment_enabled=PAYMENT_ENABLED, open_mode=OPEN_MODE,
            realm=REALM, mpp_secret=MPP_SECRET,
            challenges_for=_challenges_for, verify_or_challenge=verify_or_challenge,
        )
    return _mcp_router


def _challenges_for(bounty_cents: int, description: str) -> list[pay.PaymentMethod]:
    methods: list[pay.PaymentMethod] = []
    if RESTRICTED_KEY:
        methods.append(
            pay.PaymentMethod(
                method="stripe",
                intent=None,  # bound per-request with a live http client
                request=pay.stripe_request(
                    amount_cents=str(bounty_cents), network_id=NETWORK_ID, description=description
                ),
                description=description,
                credential_payload="spt",
            )
        )
    if TEMPO_DEPOSIT_ADDRESS:
        methods.append(
            pay.PaymentMethod(
                method="tempo",
                intent=None,
                request=pay.tempo_request(
                    amount_raw=TEMPO_PRICE_RAW,
                    currency=TEMPO_CURRENCY,
                    recipient=TEMPO_DEPOSIT_ADDRESS,
                    chain_id=TEMPO_CHAIN_ID,
                    description=description,
                ),
                description=description,
                credential_payload="tempo",
            )
        )
    return methods


async def _verify_async(meta: dict, bounty_cents: int, description: str):
    """Build live intents (own loop-bound http client) and verify-or-challenge."""
    async with httpx.AsyncClient(timeout=30) as client:
        specs = _challenges_for(bounty_cents, description)
        for s in specs:
            if s.method == "stripe":
                object.__setattr__(
                    s, "intent", pay.make_charge_intent(restricted_key=RESTRICTED_KEY, http_client=client)
                )
            elif s.method == "tempo":
                from mpp.store import MemoryStore

                object.__setattr__(
                    s,
                    "intent",
                    pay.make_tempo_charge_intent(
                        chain_id=TEMPO_CHAIN_ID, http_client=client, store=MemoryStore()
                    ),
                )
        return await pay.verify_or_challenges(
            meta=meta, methods=specs, realm=REALM, secret_key=MPP_SECRET
        )


def verify_or_challenge(credential: dict | None, bounty_cents: int, description: str):
    """Return ("challenges", [dict,...]) or ("receipt", receipt_dict)."""
    meta = {pay.META_CREDENTIAL: credential} if credential else {}
    try:
        result = asyncio.run(_verify_async(meta, bounty_cents, description))
    except pay.MCPError as e:  # malformed credential / failed settlement
        data = getattr(e, "data", None) or {}
        return "challenges", data.get("challenges", []), data.get("detail") or getattr(e, "message", "")
    if isinstance(result, list):
        return "challenges", [c.to_dict() for c in result], None
    _, receipt = result
    return "receipt", receipt.to_dict() if hasattr(receipt, "to_dict") else receipt, None


LANDING_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sidequest — machine-to-machine task marketplace</title>
<style>
body{font:16px/1.6 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
max-width:760px;margin:0 auto;padding:2.5rem 1.25rem;color:#111;background:#faf9f7}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85em}
pre{background:#111;color:#eee;padding:1rem;border-radius:8px;overflow:auto}
h1{margin:0 0 .25rem;font-size:1.6rem}h2{margin-top:2rem;font-size:1.1rem}
.tag{color:#666;margin-top:0}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid #e5e2dd;text-align:left;padding:.4rem .5rem}
a{color:#0b6}
</style></head><body>
<h1>Sidequest</h1><p class="tag">Post a task. Pay a bounty. Get a verified result &mdash; machine to machine.</p>
<p>An open task board for agent labour. Pay per task with a <strong>card</strong> (MPP / Stripe)
or <strong>USDC</strong> &mdash; no wallet required. The first worker is the
<a href="https://saltcrate-oracle.fly.dev/">SaltCrate Oracle</a>, running fetch,
check_link, check_change and verify primitives.</p>
<h2>Post a task</h2>
<pre>curl -sX POST https://saltcrate-sidequest.fly.dev/v1/tasks \\
  -H 'content-type: application/json' \\
  -d '{"type":"check_link","args":{"url":"https://example.com"}}'</pre>
<p>Then execute it &mdash; the response carries the MPP payment challenges; pay and retry
with the credential to settle and receive the result.</p>
<h2>Use it as MCP</h2>
<p>Point any MCP client at <code>https://saltcrate-sidequest.fly.dev/mcp</code>
(streamable HTTP). Tools: <code>sidequest_pricing</code>,
<code>sidequest_post_task</code>, <code>sidequest_get_task</code>,
<code>sidequest_list_tasks</code>, <code>sidequest_stats</code> (all free) and
<code>sidequest_run_task</code> (paid &mdash; returns a <code>-32042</code> payment
challenge when unpaid, identical to the Oracle's).</p>
<h2>Task types</h2><table><tr><th>type</th><th>bounty floor</th><th>args</th></tr>
<tr><td>fetch</td><td>$0.50 / $0.05</td><td>url</td></tr>
<tr><td>check_link</td><td>$0.50 / $0.05</td><td>url</td></tr>
<tr><td>check_change</td><td>$0.50 / $0.05</td><td>url</td></tr>
<tr><td>verify</td><td>$0.50 / $0.05</td><td>claim, source_url</td></tr>
</table>
<p style="margin-top:2rem;color:#888;font-size:.85em">Bounty floors: card (Stripe MPP) /
USDC (Tempo). Phase 1 charges on delivery; escrow and third-party workers are Phase 2.</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "sidequest/0.1"
    protocol_version = "HTTP/1.1"

    # --- helpers ----------------------------------------------------------- #
    def _send(self, code: int, payload, ctype: str = "application/json") -> None:
        body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers", "content-type,authorization,x-payment-credential")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json_body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        if not n:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode() or "{}")
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def log_message(self, fmt, *a):  # quieter logs
        pass

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, "")

    # --- MCP (streamable HTTP) -------------------------------------------- #
    def _send_raw(self, code: int, body: bytes, ctype: str, session: bool = False) -> None:
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("access-control-allow-origin", "*")
        self.send_header(
            "access-control-allow-headers",
            "content-type,authorization,x-payment-credential,mcp-session-id,mcp-protocol-version",
        )
        self.send_header("access-control-expose-headers", "mcp-session-id")
        if session:
            import uuid as _uuid

            sid = self.headers.get("mcp-session-id") or ("mcp-" + _uuid.uuid4().hex)
            self.send_header("mcp-session-id", sid)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _mcp(self) -> None:
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            msg = json.loads(raw.decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._send_raw(
                400,
                json.dumps({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "parse error"}}).encode(),
                "application/json",
            )
        router = _get_mcp_router()
        if isinstance(msg, list):
            out = [r for r in (router.handle(m) for m in msg) if r is not None]
        else:
            r = router.handle(msg)
            if r is None:  # notification — no response body
                return self._send_raw(202, b"", "application/json", session=True)
            out = r
        payload = json.dumps(out)
        if "text/event-stream" in (self.headers.get("accept") or ""):
            body = ("event: message\ndata: " + payload + "\n\n").encode()
            return self._send_raw(200, body, "text/event-stream", session=True)
        return self._send_raw(200, payload.encode(), "application/json", session=True)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/":
                if "text/html" in (self.headers.get("accept") or ""):
                    return self._send(200, LANDING_HTML, "text/html; charset=utf-8")
                return self._send(200, self._service_card())
            if path == "/mcp":
                self.send_response(405)
                self.send_header("allow", "POST, OPTIONS")
                self.send_header("content-length", "0")
                self.end_headers()
                return
            if path == "/health":
                s = ledger.stats()
                return self._send(200, {
                    "ok": True,
                    "service": "sidequest",
                    "payment_enabled": PAYMENT_ENABLED,
                    "open_mode": OPEN_MODE,
                    "tasks_total": s["tasks_total"],
                    "mcp": "/mcp",
                })
            if path == "/v1/pricing":
                return self._send(200, {
                    "currency_card": "usd",
                    "payment_methods": [m for m, on in (("stripe", bool(RESTRICTED_KEY)), ("tempo", bool(TEMPO_DEPOSIT_ADDRESS))) if on],
                    "task_types": {
                        t: {"bounty_cents_card": worker.default_bounty_cents(t),
                            "bounty_raw_usdc": TEMPO_PRICE_RAW,
                            "worker": worker.WORKER_ID}
                        for t in worker.SUPPORTED_TYPES
                    },
                })
            if path == "/v1/stats":
                return self._send(200, ledger.stats())
            if path == "/v1/tasks":
                from urllib.parse import parse_qs, urlparse

                q = parse_qs(urlparse(self.path).query)
                tasks = ledger.list_tasks(
                    status=(q.get("status") or [None])[0],
                    task_type=(q.get("type") or [None])[0],
                    limit=int((q.get("limit") or ["50"])[0]),
                )
                return self._send(200, {"tasks": tasks, "count": len(tasks)})
            if path.startswith("/v1/tasks/"):
                t = ledger.get(path.rsplit("/", 1)[-1])
                if t is None:
                    return self._send(404, {"error": "not_found"})
                return self._send(200, t)
            return self._send(404, {"error": "not_found", "path": path})
        except Exception:
            traceback.print_exc()
            return self._send(500, {"error": "internal_error"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/mcp":
            return self._mcp()
        body = self._json_body()
        try:
            if path == "/v1/tasks":
                return self._create_task(body)
            if path.startswith("/v1/tasks/") and path.endswith("/execute"):
                return self._execute(path.split("/")[3], body)
            if path.startswith("/v1/tasks/") and path.endswith("/cancel"):
                ok = ledger.cancel(path.split("/")[3])
                return self._send(200 if ok else 409, {"cancelled": ok})
            return self._send(404, {"error": "not_found", "path": path})
        except Exception:
            traceback.print_exc()
            return self._send(500, {"error": "internal_error"})

    # --- route logic ------------------------------------------------------- #
    def _service_card(self) -> dict:
        return {
            "service": "sidequest",
            "version": "0.2.0",
            "what": "Machine-to-machine task marketplace. Post a task + bounty; a worker runs it; settle via MPP (card or USDC).",
            "mcp": "POST /mcp — MCP (streamable HTTP) front-end: sidequest_pricing, sidequest_post_task, sidequest_run_task, sidequest_get_task, sidequest_list_tasks, sidequest_stats",
            "endpoints": {
                "POST /v1/tasks": "create a task",
                "GET /v1/tasks/{id}": "task status",
                "POST /v1/tasks/{id}/execute": "pay + run (402 with MPP challenges when unpaid)",
                "GET /v1/pricing": "task types + bounties",
                "GET /v1/stats": "public ledger stats",
            },
            "task_types": worker.SUPPORTED_TYPES,
            "payment_methods": [m for m, on in (("stripe", bool(RESTRICTED_KEY)), ("tempo", bool(TEMPO_DEPOSIT_ADDRESS))) if on],
            "open_mode": OPEN_MODE,
        }

    def _create_task(self, body: dict):
        task_type = body.get("type")
        args = body.get("args") or {}
        if not isinstance(task_type, str) or not worker.is_supported(task_type):
            return self._send(400, {
                "error": "unsupported_type",
                "supported": worker.SUPPORTED_TYPES,
            })
        bounty = body.get("bounty_cents")
        if not isinstance(bounty, int) or bounty <= 0:
            bounty = worker.default_bounty_cents(task_type)
        t = ledger.create(
            task_type=task_type,
            args=args,
            bounty_cents=bounty,
            requester=body.get("requester") if isinstance(body.get("requester"), str) else None,
        )
        t["next"] = f"POST /v1/tasks/{t['id']}/execute"
        return self._send(201, t)

    def _execute(self, task_id: str, body: dict):
        t = ledger.get(task_id)
        if t is None:
            return self._send(404, {"error": "not_found"})
        if t["status"] != "pending":
            return self._send(409, {"error": "not_pending", "status": t["status"]})

        credential = body.get("credential")
        if credential is None:
            hdr = self.headers.get("x-payment-credential")
            if hdr:
                try:
                    import base64

                    credential = json.loads(base64.b64decode(hdr).decode())
                except Exception:
                    credential = None

        payment_method = "open"
        payment_ref = None
        if PAYMENT_ENABLED and not OPEN_MODE:
            if credential is None:
                specs = _challenges_for(t["bounty_cents"], f"{t['type']} (Sidequest)")
                return self._send(402, {
                    "error": "payment_required",
                    "httpStatus": 402,
                    "task_id": t["id"],
                    "bounty_cents": t["bounty_cents"],
                    "challenges": [pay.create_challenge(
                        request=s.request, realm=REALM, secret_key=MPP_SECRET,
                        method=s.method, intent="charge", description=s.description,
                    ).to_dict() for s in specs],
                    "retry_with": "POST the same body plus {\"credential\": <echoed challenge + payload>}",
                })
            kind, payload, detail = verify_or_challenge(
                credential, t["bounty_cents"], f"{t['type']} (Sidequest)"
            )
            if kind == "challenges":
                return self._send(402, {
                    "error": "payment_verification_failed",
                    "httpStatus": 402,
                    "detail": detail,
                    "challenges": payload,
                })
            payment_method = (payload or {}).get("method", "stripe")
            payment_ref = (payload or {}).get("reference")

        claimed = ledger.claim(t["id"], worker.WORKER_ID)
        if claimed is None:
            return self._send(409, {"error": "not_pending"})
        try:
            result = worker.run(t["type"], t["args"], state_db=STATE_DB)
            done = ledger.complete(
                t["id"], result=result, payment_method=payment_method, payment_ref=payment_ref
            )
            return self._send(200, done)
        except worker.TaskArgError as e:
            done = ledger.complete(t["id"], error=f"bad_args: {e}", payment_method=payment_method,
                                   payment_ref=payment_ref)
            return self._send(422, done)
        except Exception as e:  # worker crashed — record it, don't leak a 500 into the ledger
            traceback.print_exc()
            done = ledger.complete(t["id"], error=f"worker_error: {type(e).__name__}: {e}",
                                   payment_method=payment_method, payment_ref=payment_ref)
            return self._send(502, done)


def main() -> None:
    ledger._conn().close()  # ensure schema exists before we listen
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(
        f"sidequest listening on :{PORT} | realm={REALM} | payments={'on' if (PAYMENT_ENABLED and not OPEN_MODE) else 'OFF (open mode)'}"
    )
    srv.serve_forever()


if __name__ == "__main__":
    main()
