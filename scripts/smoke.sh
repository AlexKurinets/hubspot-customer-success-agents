#!/usr/bin/env bash
# Runs every command the README documents against a running stack (`docker compose up -d --build --wait`).
set -euo pipefail

API="http://localhost:${API_PORT:-8151}"
HS="http://localhost:${HUBSPOT_PORT:-8150}"
DASH="http://localhost:${DASHBOARD_PORT:-8550}"
PY="docker compose exec -T api python -c"
OUT="$(mktemp -d)"

echo "== health"
curl -fsS "$API/health" && echo
curl -fsS "$HS/health" && echo
curl -fsS "$DASH/_stcore/health" && echo

echo "== seeded figures"
curl -fsS "$API/metrics/summary" > "$OUT/summary.json"
$PY "import json, sys; s = json.load(sys.stdin); a = s['actions']
assert s['today'] == '2026-06-30' and s['accounts'] == 1200 and s['csms'] == 8, (s['today'], s['accounts'], s['csms'])
assert (a['total'], a['autonomous']) == (2989, 2277), (a['total'], a['autonomous'])
assert s['renewal_lead_days']['after_median'] == 101 and s['renewal_lead_days']['before_median'] == 25
assert s['qbr_prep_minutes']['after_median'] == 45 and s['qbr_prep_minutes']['before_median'] == 179
g = {x['action_type']: x for x in s['graduation']}
assert g['send_checkin']['days_after_launch'] == 44, g['send_checkin']
print('ok', a['per_month'], 'actions a month,', round(a['autonomous_share'] * 100), '% with no human touch')" < "$OUT/summary.json"

echo "== README: status"
docker compose exec -T worker csagents status

echo "== README: approve a check-in through the API"
curl -fsS "$API/approvals" > "$OUT/open.json"
ID=$($PY "import json, sys; rows = [r for r in json.load(sys.stdin) if r['action_type'] == 'send_checkin']
print(rows[0]['id'])" < "$OUT/open.json")
echo "approval $ID"
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API/approvals/$ID/decide" -H "Content-Type: application/json" \
  -d '{"decision": "approve", "by": "Maya Okafor"}')
[ "$code" = "401" ] || { echo "expected 401 without a token, got $code"; exit 1; }
curl -fsS -X POST "$API/approvals/$ID/decide" -H "X-Approver-Token: demo-csm" -H "Content-Type: application/json" \
  -d '{"decision": "approve", "by": "Maya Okafor"}'
echo
for _ in $(seq 1 30); do
  curl -fsS "$API/approvals/$ID" > "$OUT/approval.json"
  status=$($PY "import json, sys; print(json.load(sys.stdin)['status'])" < "$OUT/approval.json")
  [ "$status" = "executed" ] && break
  sleep 2
done
$PY "import json, sys; a = json.load(sys.stdin); assert a['status'] == 'executed', a; print('executed:', a['outcome'])" \
  < "$OUT/approval.json"

echo "== README: a renewal date change arrives by webhook"
CID=$(curl -fsS "$API/accounts?tier=green&limit=2000" | docker compose exec -T api python -c "import json, sys
from datetime import date
rows = [r for r in json.load(sys.stdin) if r['renewal_date'] and (date.fromisoformat(r['renewal_date']) - date(2026, 6, 30)).days > 150]
print(rows[0]['company_id'])")
echo "company $CID"
curl -fsS -X PATCH "$HS/crm/v3/objects/companies/$CID" -H "Authorization: Bearer pat-demo-portal-users" \
  -H "Content-Type: application/json" -d '{"properties": {"cs_renewal_date": "2026-09-15"}}' > /dev/null
for _ in $(seq 1 30); do
  curl -fsS "$API/accounts/$CID" > "$OUT/account.json"
  found=$($PY "import json, sys; d = json.load(sys.stdin)
print(any(l['action_key'] == 'create_renewal_deal:$CID:renewal:2026-09-15' for l in d['ledger']))" < "$OUT/account.json")
  [ "$found" = "True" ] && break
  sleep 2
done
$PY "import json, sys; d = json.load(sys.stdin)
row = next(l for l in d['ledger'] if l['action_key'] == 'create_renewal_deal:$CID:renewal:2026-09-15')
assert row['source'] == 'webhook', row
assert any(t['type'] == 'deals' and t.get('closedate') == '2026-09-15' for t in d['timeline'])
print('renewal deal opened from the webhook for company $CID')" < "$OUT/account.json"

echo "== README: an unsigned webhook is rejected"
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API/webhooks/hubspot" -H "Content-Type: application/json" -d '[]')
[ "$code" = "401" ] || { echo "expected 401, got $code"; exit 1; }

echo "== README: an outage, the dead-letter queue and replay"
curl -fsS -X POST "$HS/_mock/faults" -H "Content-Type: application/json" \
  -d '{"status": 503, "count": 1000, "match": "/objects/tasks", "methods": ["POST"]}' > /dev/null
docker compose exec -T worker csagents advance --days 1 > "$OUT/advance.json"
curl -fsS -X DELETE "$HS/_mock/faults" > /dev/null
curl -fsS "$API/metrics/reliability" > "$OUT/rel.json"
$PY "import json, sys; r = json.load(sys.stdin); assert r['dead_letters_open'] > 0, r['dead_letters_open']
print(r['dead_letters_open'], 'dead letters after the outage')" < "$OUT/rel.json"
docker compose exec -T worker csagents replay --dead-letters --stale-claims
curl -fsS "$API/metrics/reliability" > "$OUT/rel.json"
$PY "import json, sys; r = json.load(sys.stdin); assert r['dead_letters_open'] == 0, r['dead_letters_open']
print('dead letters replayed')" < "$OUT/rel.json"
$PY "import json, sys; s = json.loads(sys.stdin.read().splitlines()[-1]); assert s['today'] == '2026-07-01', s" < "$OUT/advance.json"

echo "== README: refit the health score points"
docker compose exec -T worker csagents fit-weights > "$OUT/fit.json"
$PY "import json, sys; f = json.load(sys.stdin); assert f['points'] == {'seat_utilization': 50, 'module_adoption': 30,
'ticket_ok': 10, 'invoice_current': 10}, f['points']; print('held-out AUC', f['held_out_auc'])" < "$OUT/fit.json"

echo "== README: tests inside the stack"
docker compose exec -T db psql -U cs -d cs -tc "SELECT 1 FROM pg_database WHERE datname = 'cs_test'" | grep -q 1 \
  || docker compose exec -T db psql -U cs -d cs -c "CREATE DATABASE cs_test"
docker compose run --rm -T --no-deps -e TEST_DATABASE_URL=postgresql://cs:cs@db:5432/cs_test worker pytest -q

echo "smoke ok"
