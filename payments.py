"""MPP gate for SaltCrate Oracle — MCP transport binding (draft-payment-transport-mcp-00).

Dual-method: Stripe card/SPT **and** Tempo stablecoin (USDC), offered as two
challenges in the same ``-32042`` error. The card/SPT path is unchanged.

Why NOT raw HTTP 402?  The oracle is an MCP server (streamable-http JSON-RPC). Standard
MCP clients (Claude, Cursor, Hermes, Stripe's Link agent) do NOT retry on a bare HTTP 402 —
that's the plain-HTTP MPP flow (mpp.dev/protocol, ``WWW-Authenticate: Payment``). For MCP,
the challenge rides inside the JSON-RPC layer per draft-payment-transport-mcp-00:

    unpaid tools/call  ->  JSON-RPC error  -32042 "Payment Required"
                           data.httpStatus = 402
                           data.challenges = [{id, realm, method, intent, request,
                                               expires, description}, ...]
    paid retry         ->  params._meta["org.paymentauth/credential"] =
                           {challenge: <echoed challenge>, payload: {...}, source?}
    fulfilled          ->  result._meta["org.paymentauth/receipt"] =
                           {status, challengeId, method, timestamp, reference, settlement}

Two methods are wired here, each with its own intent + request + credential payload:

  * ``method="stripe"`` — pympp ``mpp.methods.stripe.intents.ChargeIntent`` (SPT
    PaymentIntent create+confirm). Payload: ``{"spt": "spt_..."}``. Card floor $0.50.
  * ``method="tempo"``  — pympp ``mpp.methods.tempo.intents.ChargeIntent`` (on-chain
    USDC transfer verification via the Tempo RPC). Payload: ``{"type": "hash", "hash":
    "0x..."}`` (push) or ``{"type": "transaction", "signature": "0x..."}`` (pull).
    Stablecoin floor $0.01 (10_000 raw units of 6-decimal USDC). Requires the
    ``pympp[tempo]`` extra and a shared atomic replay ``Store``.

The Stripe card/SPT path is untouched. The Tempo path imports ``mpp.methods.tempo``
lazily so a card-only deployment still works without the ``pympp[tempo]`` extra.

pympp's own ``mpp.extensions.mcp`` is deliberately NOT used: it pins ``mcp<2`` and imports
the removed mcp-1.x names ``McpError`` and ``mcp.types.ErrorData`` (both renamed/moved in
the 2.x SDK), so it raises ImportError against this project's ``mcp==2.3.0``.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence

import httpx

# pympp core (no MCP dependency) — used for challenge HMAC + Stripe SPT charge.
from mpp import _constant_time_equal, generate_challenge_id
from mpp.methods.stripe.intents import ChargeIntent

# MCP SDK 2.x error type (note: MCPError, not the mcp-1.x "McpError").
from mcp.shared.exceptions import MCPError

# --- draft-payment-transport-mcp-00 constants ---
META_CREDENTIAL = "org.paymentauth/credential"
META_RECEIPT = "org.paymentauth/receipt"
CODE_PAYMENT_REQUIRED = -32042
CODE_PAYMENT_VERIFICATION_FAILED = -32043
CODE_MALFORMED_CREDENTIAL = -32602
HTTP_STATUS_PAYMENT_REQUIRED = 402

DEFAULT_CHALLENGE_TTL = timedelta(minutes=5)

# The restricted Stripe key (rk_live_… / rk_test_…) can create+confirm SPT PaymentIntents.
STRIPE_RESTRICTED_KEY = "STRIPE_RESTRICTED_KEY"
STRIPE_NETWORK_ID = "STRIPE_NETWORK_ID"  # profile_… (Business Profile = MPP networkId)
MPP_SECRET_KEY = "MPP_SECRET_KEY"
MPP_REALM = "MPP_REALM"

# Tempo / stablecoin configuration (env-var names).
TEMPO_DEPOSIT_ADDRESS = "TEMPO_DEPOSIT_ADDRESS"  # Stripe crypto deposit address (network=tempo)
TEMPO_CHAIN_ID = "TEMPO_CHAIN_ID"                # 4217 mainnet (default) / 42431 Moderato testnet
TEMPO_RPC_URL = "TEMPO_RPC_URL"                  # optional RPC override
TEMPO_CURRENCY = "TEMPO_CURRENCY"                # token contract address (default USDC.e)
TEMPO_PRICE_RAW = "TEMPO_PRICE_RAW"              # raw units of `currency` (default "10000" = $0.01 USDC)
MPP_STORE = "MPP_STORE"                          # replay store: "sqlite" | "redis" | "memory"
MPP_SQLITE_PATH = "MPP_SQLITE_PATH"              # sqlite db path (default "mpp-replay.db")
MPP_REDIS_URL = "MPP_REDIS_URL"                  # redis URL for RedisStore

# Tempo network defaults (from mpp.methods.tempo._defaults).
TEMPO_USDC = "0x20C000000000000000000000b9537d11c60E8b50"  # USDC.e on Tempo mainnet
TEMPO_MAINNET_CHAIN_ID = 4217
TEMPO_TESTNET_CHAIN_ID = 42431
TEMPO_USDC_DECIMALS = 6
# $0.01 USDC in raw units (6 decimals). The card floor is 50 cents ($0.50).
TEMPO_PRICE_RAW_DEFAULT = "10000"


def derive_mpp_secret(stripe_secret_key: str) -> str:
    """Derive the MPP challenge-signing secret from the Stripe key.

    Matches Stripe's TypeScript sample:
        HMAC-SHA256(STRIPE_SECRET_KEY, "mpp-challenge-signing") -> base64
    A standalone random secret also works — the client never sees it; it only
    binds the challenge id to its parameters. Deriving from the Stripe key avoids
    managing a second secret.
    """
    return (
        hmac.new(
            stripe_secret_key.encode(),
            b"mpp-challenge-signing",
            hashlib.sha256,
        )
        .digest()
        .hex()
    )


# --------------------------------------------------------------------------- #
# MCP wire types (self-contained, mcp-2.x compatible)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MCPChallenge:
    id: str
    realm: str
    method: str
    intent: str
    request: dict[str, Any]
    expires: str | None = None
    description: str | None = None
    digest: str | None = None
    opaque: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "realm": self.realm,
            "method": self.method,
            "intent": self.intent,
            "request": self.request,
        }
        if self.expires is not None:
            d["expires"] = self.expires
        if self.description is not None:
            d["description"] = self.description
        if self.digest is not None:
            d["digest"] = self.digest
        if self.opaque is not None:
            d["opaque"] = self.opaque
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MCPChallenge":
        return cls(
            id=data["id"],
            realm=data["realm"],
            method=data["method"],
            intent=data["intent"],
            request=data["request"],
            expires=data.get("expires"),
            description=data.get("description"),
            digest=data.get("digest"),
            opaque=data.get("opaque"),
        )


@dataclass(frozen=True)
class MCPReceipt:
    status: str
    challenge_id: str
    method: str
    timestamp: str
    reference: str | None = None
    settlement: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "challengeId": self.challenge_id,
            "method": self.method,
            "timestamp": self.timestamp,
        }
        if self.reference is not None:
            d["reference"] = self.reference
        if self.settlement is not None:
            d["settlement"] = self.settlement
        return d

    def to_meta(self) -> dict[str, Any]:
        return {META_RECEIPT: self.to_dict()}


# --------------------------------------------------------------------------- #
# Payment method spec (one challenge per method)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PaymentMethod:
    """A single payment method offered in a challenge.

    Attributes:
        method:             MPP method name ("stripe" | "tempo").
        intent:             An intent object with ``.name`` and ``async verify()``
                            (pympp's Stripe or Tempo ``ChargeIntent``, or a test fake).
        request:            The canonical ``request`` dict for this method's challenge.
        description:        Human-readable description attached to the challenge.
        credential_payload: How the client's credential ``payload`` is shaped:
                            "spt"   -> ``{"spt": "spt_..."}``
                            "tempo" -> ``{"type": "hash", "hash": "0x..."}`` or
                                       ``{"type": "transaction", "signature": "0x..."}``
    """

    method: str
    intent: Any
    request: dict[str, Any]
    description: str | None = None
    credential_payload: str = "spt"


# --------------------------------------------------------------------------- #
# Challenge + verification (stateless, HMAC-bound)
# --------------------------------------------------------------------------- #


def create_challenge(
    *,
    request: dict[str, Any],
    realm: str,
    secret_key: str,
    method: str = "stripe",
    intent: str = "charge",
    expires_in: timedelta = DEFAULT_CHALLENGE_TTL,
    description: str | None = None,
) -> MCPChallenge:
    """Build a fresh challenge with a stateless HMAC-bound id."""
    expires = (datetime.now(UTC) + expires_in).isoformat()
    if expires.endswith("+00:00"):
        expires = expires[:-6] + "Z"
    challenge_id = generate_challenge_id(
        secret_key=secret_key,
        realm=realm,
        method=method,
        intent=intent,
        request=request,
        expires=expires,
    )
    return MCPChallenge(
        id=challenge_id,
        realm=realm,
        method=method,
        intent=intent,
        request=request,
        expires=expires,
        description=description,
    )


def _new_challenge(
    *, request: dict[str, Any], realm: str, secret_key: str, method: str, description: str | None
) -> MCPChallenge:
    return create_challenge(
        request=request, realm=realm, secret_key=secret_key, method=method, description=description
    )


def _payment_required_error(challenges: list[MCPChallenge]) -> MCPError:
    return MCPError(
        code=CODE_PAYMENT_REQUIRED,
        message="Payment Required",
        data={
            "httpStatus": HTTP_STATUS_PAYMENT_REQUIRED,
            "challenges": [c.to_dict() for c in challenges],
        },
    )


def _verification_failed_error(
    challenges: MCPChallenge | list[MCPChallenge],
    detail: str,
    reason: str = "verification-failed",
) -> MCPError:
    if isinstance(challenges, MCPChallenge):
        challenges = [challenges]
    return MCPError(
        code=CODE_PAYMENT_VERIFICATION_FAILED,
        message="Payment Verification Failed",
        data={
            "httpStatus": HTTP_STATUS_PAYMENT_REQUIRED,
            "challenges": [c.to_dict() for c in challenges],
            "failure": {"reason": reason, "detail": detail},
        },
    )


def _all_challenges(
    methods: Sequence[PaymentMethod], realm: str, secret_key: str
) -> list[MCPChallenge]:
    return [
        create_challenge(
            request=m.request,
            realm=realm,
            secret_key=secret_key,
            method=m.method,
            intent=getattr(m.intent, "name", "charge"),
            description=m.description,
        )
        for m in methods
    ]


def _find_method(
    methods: Sequence[PaymentMethod], method: str, intent: str | None
) -> PaymentMethod | None:
    """Find the method matching the echoed challenge's method+intent."""
    for m in methods:
        if m.method == method and getattr(m.intent, "name", "charge") == intent:
            return m
    # fallback: method-only match (intents may omit .name)
    for m in methods:
        if m.method == method:
            return m
    return None


def _settlement(method: str, request: dict[str, Any]) -> dict[str, Any]:
    """Build the receipt's ``settlement`` summary for a settled charge."""
    if method == "stripe":
        s = {k: request[k] for k in ("amount", "currency") if k in request}
        return s
    # tempo
    s = {k: request[k] for k in ("amount", "currency", "recipient") if k in request}
    md = request.get("methodDetails")
    if isinstance(md, dict) and "chainId" in md:
        s["chainId"] = md["chainId"]
    s["network"] = "tempo"
    return s


def _parse_credential(
    credential_data: dict[str, Any],
) -> tuple[MCPChallenge, dict[str, Any], Any]:
    """Parse the ``org.paymentauth/credential`` structure (challenge + payload + source)."""
    try:
        challenge = MCPChallenge.from_dict(credential_data["challenge"])
        payload = credential_data["payload"]
        if not isinstance(payload, dict):
            raise KeyError("payload")
        source = credential_data.get("source")
    except (KeyError, TypeError) as e:
        raise MCPError(
            code=CODE_MALFORMED_CREDENTIAL,
            message="Invalid params",
            data={"httpStatus": HTTP_STATUS_PAYMENT_REQUIRED, "detail": f"Invalid credential structure: {e}"},
        ) from e
    return challenge, payload, source


def _malformed_payload(detail: str) -> MCPError:
    return MCPError(
        code=CODE_MALFORMED_CREDENTIAL,
        message="Invalid params",
        data={"httpStatus": HTTP_STATUS_PAYMENT_REQUIRED, "detail": detail},
    )


async def verify_or_challenges(
    *,
    meta: dict[str, Any] | None,
    methods: Sequence[PaymentMethod],
    realm: str,
    secret_key: str,
) -> list[MCPChallenge] | tuple[dict[str, Any], MCPReceipt]:
    """Verify a payment credential, or issue fresh challenges for every method.

    Returns either a ``list[MCPChallenge]`` (no valid credential -> caller must
    surface them as a -32042 error) or ``(credential_dict, MCPReceipt)`` on success.

    The echoed challenge selects the method: its ``method`` + ``intent`` fields are
    matched against the configured ``methods``, and settlement is dispatched to that
    method's intent with the appropriate credential payload shape.

    Raises :class:`MCPError` (-32043 / -32602) on a malformed or failing credential.
    """
    meta = meta or {}
    credential_data = meta.get(META_CREDENTIAL)

    if credential_data is None:
        return _all_challenges(methods, realm, secret_key)

    challenge, payload, source = _parse_credential(credential_data)

    spec = _find_method(methods, challenge.method, challenge.intent)
    if spec is None:
        return _all_challenges(methods, realm, secret_key)

    # --- stateless challenge verification (HMAC-bound id) ---
    try:
        expected_id = generate_challenge_id(
            secret_key=secret_key,
            realm=challenge.realm,
            method=challenge.method,
            intent=challenge.intent,
            request=challenge.request,
            expires=challenge.expires,
            digest=challenge.digest,
            opaque=challenge.opaque,
        )
    except Exception:
        return _all_challenges(methods, realm, secret_key)
    if not _constant_time_equal(challenge.id, expected_id):
        return _all_challenges(methods, realm, secret_key)

    # --- echoed fields must match the server's current values for this method ---
    if challenge.realm != realm:
        return _all_challenges(methods, realm, secret_key)
    if challenge.method != spec.method or challenge.intent != getattr(spec.intent, "name", "charge"):
        return _all_challenges(methods, realm, secret_key)
    if challenge.request != spec.request:
        return _all_challenges(methods, realm, secret_key)

    # --- expiry: fail closed ---
    if not challenge.expires:
        return _all_challenges(methods, realm, secret_key)
    try:
        expires_dt = datetime.fromisoformat(challenge.expires.replace("Z", "+00:00"))
    except ValueError:
        return _all_challenges(methods, realm, secret_key)
    if expires_dt < datetime.now(UTC):
        return _all_challenges(methods, realm, secret_key)

    # --- build the method-appropriate credential payload ---
    if spec.credential_payload == "spt":
        if not isinstance(payload.get("spt"), str):
            raise _malformed_payload("payload.spt is required for method 'stripe'")
        core_payload: dict[str, Any] = {"spt": payload["spt"]}
    elif spec.credential_payload == "tempo":
        if payload.get("type") not in ("hash", "transaction"):
            raise _malformed_payload("payload.type must be 'hash' or 'transaction' for method 'tempo'")
        core_payload = payload  # {"type": "hash", "hash": ...} | {"type": "transaction", "signature": ...}
    else:
        raise _malformed_payload(f"unknown credential payload kind: {spec.credential_payload}")

    # --- settle via the method's intent ---
    from mpp import ChallengeEcho, Credential
    from mpp._canonical import canonical_b64url

    echo = ChallengeEcho(
        id=challenge.id,
        realm=challenge.realm,
        method=challenge.method,
        intent=challenge.intent,
        request=canonical_b64url(challenge.request),
        expires=challenge.expires,
        digest=challenge.digest,
        opaque=canonical_b64url(challenge.opaque) if challenge.opaque is not None else None,
    )
    core_credential = Credential(challenge=echo, payload=core_payload, source=source)

    try:
        core_receipt = await spec.intent.verify(core_credential, spec.request)
    except Exception as e:
        raise _verification_failed_error(
            _all_challenges(methods, realm, secret_key),
            detail=str(e),
        ) from e

    receipt = MCPReceipt(
        status="success",
        challenge_id=challenge.id,
        method=spec.method,
        timestamp=core_receipt.timestamp.isoformat(),
        reference=core_receipt.reference or None,
        settlement=_settlement(spec.method, spec.request) or None,
    )
    return (credential_data, receipt)


async def verify_or_challenge(
    *,
    meta: dict[str, Any] | None,
    intent: ChargeIntent,
    request: dict[str, Any],
    realm: str,
    secret_key: str,
    method: str = "stripe",
    description: str | None = None,
) -> MCPChallenge | tuple[dict[str, Any], MCPReceipt]:
    """Single-method convenience wrapper (Stripe card/SPT path).

    Backward-compatible with the original card-only gate: returns a single
    :class:`MCPChallenge` (not a list) when unpaid, or ``(credential, receipt)``.
    """
    spec = PaymentMethod(
        method=method, intent=intent, request=request, description=description, credential_payload="spt"
    )
    result = await verify_or_challenges(meta=meta, methods=[spec], realm=realm, secret_key=secret_key)
    if isinstance(result, list):
        return result[0]
    return result


# --------------------------------------------------------------------------- #
# Method intents + request builders
# --------------------------------------------------------------------------- #


def make_charge_intent(
    *,
    restricted_key: str | None = None,
    client: Any | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> ChargeIntent:
    """Build the Stripe SPT charge intent.

    Prefer ``client=`` (a configured ``stripe.StripeClient``/``AsyncStripeClient``).
    ``restricted_key=`` uses pympp's raw-HTTP path (no ``stripe`` package needed);
    pass a shared ``http_client`` so a long-running server reuses one connection pool.
    """
    return ChargeIntent(client=client, secret_key=restricted_key, http_client=http_client)


def stripe_request(
    *,
    amount_cents: str,
    network_id: str,
    description: str | None = None,
    payment_method_types: list[str] | None = None,
) -> dict[str, Any]:
    """Build the canonical Stripe charge ``request`` dict for a challenge.

    ``amount_cents`` is the amount in the smallest currency unit ("50" = $0.50).
    The card/SPT floor is 50 cents ($0.50).
    """
    return {
        "amount": amount_cents,
        "currency": "usd",
        "methodDetails": {
            "networkId": network_id,
            "paymentMethodTypes": payment_method_types or ["card"],
        },
        **( {"description": description} if description else {}),
    }


def tempo_request(
    *,
    amount_raw: str,
    currency: str,
    recipient: str,
    chain_id: int = TEMPO_MAINNET_CHAIN_ID,
    description: str | None = None,
) -> dict[str, Any]:
    """Build the canonical Tempo charge ``request`` dict for a challenge.

    Matches ``mpp.methods.tempo.schemas.ChargeRequest``:
      * ``amount_raw`` is the transfer amount in the token's smallest unit
        ("10000" = $0.01 USDC at 6 decimals). The stablecoin floor is $0.01.
      * ``currency`` is the TIP-20 token contract address (USDC.e by default).
      * ``recipient`` is the deposit address the payer sends USDC to (the Stripe
        crypto deposit address, network=tempo).
      * ``chain_id`` is 4217 (mainnet) or 42431 (Moderato testnet).
    """
    return {
        "amount": amount_raw,
        "currency": currency,
        "recipient": recipient,
        "methodDetails": {"chainId": chain_id},
        **( {"description": description} if description else {}),
    }


def make_tempo_charge_intent(
    *,
    chain_id: int | None = None,
    rpc_url: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    store: Any | None = None,
    validate_sender: Any | None = None,
) -> Any:
    """Build the Tempo charge intent (lazy import so card-only deploys stay light).

    Requires the ``pympp[tempo]`` extra (eth-abi, eth-account, eth-hash, pytempo, …).

    ``store`` is the shared atomic replay store (see :func:`make_store`) — REQUIRED
    for correct tx-hash replay protection across replicas/restarts. If omitted the
    intent falls back to an in-memory ``MemoryStore`` (single-process only).
    """
    from mpp.methods.tempo.intents import ChargeIntent as TempoChargeIntent

    return TempoChargeIntent(
        chain_id=chain_id,
        rpc_url=rpc_url,
        http_client=http_client,
        store=store,
        validate_sender=validate_sender,
    )


async def make_store(
    *,
    kind: str = "sqlite",
    path: str = "mpp-replay.db",
    redis_url: str | None = None,
    ttl_seconds: int | None = None,
) -> Any:
    """Build a shared atomic replay store for Tempo tx-hash replay protection.

    * ``sqlite`` — single-instance persistent store (needs the ``pympp[sqlite]`` extra,
      i.e. ``aiosqlite``). Point ``path`` at a persistent volume.
    * ``redis``  — multi-instance store (needs ``pympp[redis]``, i.e. ``redis``). Use for
      multi-replica deployments (Fly.io scaled to N machines).
    * ``memory`` — in-process only; for tests/single-process dev. Do NOT use in prod
      (replay protection is lost on restart and across replicas).
    """
    if kind == "memory":
        from mpp.store import MemoryStore

        return MemoryStore()
    if kind == "sqlite":
        from mpp.stores.sqlite import SQLiteStore

        return await SQLiteStore.create(path, ttl_seconds=ttl_seconds)
    if kind == "redis":
        from mpp.stores.redis import RedisStore
        from redis.asyncio import from_url

        if not redis_url:
            raise ValueError("redis_url is required for kind='redis'")
        return RedisStore(from_url(redis_url), ttl_seconds=ttl_seconds)
    raise ValueError(f"Unknown MPP store kind: {kind!r}")
