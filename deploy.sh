#!/usr/bin/env bash
# Deploy Sidequest to Fly. Idempotent: safe to re-run.
set -euo pipefail

export FLY_API_TOKEN="$(python3 -c "import json;print(json.load(open('/home/hermes/.hermes/secrets/flyio.json'))['api_token'])")"

python3 - <<'PY' > /tmp/sq.env
import json, secrets
d = json.load(open('/home/hermes/.hermes/secrets/stripe.json'))
print(f"STRIPE_RESTRICTED_KEY={d['restricted_key']}")
print(f"STRIPE_NETWORK_ID={d['business_profile_id']}")
print(f"MPP_SECRET_KEY={secrets.token_hex(32)}")
print(f"TEMPO_DEPOSIT_ADDRESS={d['tempo_deposit_address']}")
PY
set -a; source /tmp/sq.env; set +a; rm -f /tmp/sq.env

cd "$(dirname "$0")"

flyctl apps create saltcrate-sidequest --org personal 2>/dev/null || echo "app exists"
flyctl volumes create sidequest_data -a saltcrate-sidequest --region ord --size 1 --yes 2>/dev/null || echo "volume exists"

flyctl secrets set -a saltcrate-sidequest \
  STRIPE_RESTRICTED_KEY="$STRIPE_RESTRICTED_KEY" \
  STRIPE_NETWORK_ID="$STRIPE_NETWORK_ID" \
  MPP_SECRET_KEY="$MPP_SECRET_KEY" \
  TEMPO_DEPOSIT_ADDRESS="$TEMPO_DEPOSIT_ADDRESS" \
  TEMPO_CHAIN_ID=4217 \
  TEMPO_PRICE_RAW=50000

flyctl deploy -a saltcrate-sidequest --ha=false

echo "--- deployed. smoke test ---"
sleep 5
python3 - <<'PY'
import json, urllib.request, urllib.error, time
B = "https://saltcrate-sidequest.fly.dev"
def req(p, method="GET", body=None):
    d = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B+p, data=d, method=method, headers={"content-type":"application/json"})
    try:
        with urllib.request.urlopen(r, timeout=40) as x:
            return x.status, json.loads(x.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())
    except Exception as e:
        return -1, str(e)
for attempt in range(12):
    s, b = req("/health")
    if s == 200: break
    time.sleep(5)
print("health:", s, b)
s, b = req("/v1/pricing")
print("pricing:", s, json.dumps(b)[:300])
s, t = req("/v1/tasks", "POST", {"type":"check_link","args":{"url":"https://saltcrate.com"}})
print("create:", s, t.get("id"), t.get("status"))
if s == 201:
    s, b = req(f"/v1/tasks/{t['id']}/execute", "POST", {})
    print("unpaid execute:", s, "challenges:", len(b.get("challenges", [])) if isinstance(b, dict) else b)
PY
