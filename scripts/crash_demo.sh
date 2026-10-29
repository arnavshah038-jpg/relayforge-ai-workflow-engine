#!/usr/bin/env bash
# Kills the server with kill -9 while a refund is being retried, starts it again and checks
# that the workflow still finishes and the provider refunded exactly once.
#
# Needs: migrations applied, DATABASE_URL and SECRET_KEY set (env or .env), curl, python3.
# Run from the project root with the virtualenv active:  bash scripts/crash_demo.sh
set -u

PORT=${PORT:-8767}
BASE=http://127.0.0.1:$PORT
JSON='content-type: application/json'
N=$RANDOM

# short lease and fast sweeper so the demo takes seconds instead of minutes
export LEASE_SECONDS=5 SWEEP_INTERVAL_S=2 RETRY_BASE_DELAY_S=1

field() { python3 -c "import sys, json; print(json.load(sys.stdin)$1)"; }
token() {
  curl -s -X POST $BASE/auth/login -H "$JSON" \
    -d "{\"email\":\"$1\",\"password\":\"password123\"}" | field "['access_token']"
}
show() {
  curl -s $BASE/workflows/$WF -H "authorization: Bearer $AGENT" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('  status:', d['status'], ' steps:', [(s['name'], s['status'], s['attempts']) for s in d['steps']])"
}

echo "1. start server, provider is down (every refund call fails)"
GATEWAY_FAILURE_RATE=1.0 uvicorn app.main:app --port $PORT >/tmp/relayforge_demo_a.log 2>&1 &
PID=$!
sleep 3

curl -s -o /dev/null -X POST $BASE/auth/signup -H "$JSON" \
  -d "{\"company_name\":\"Demo $N\",\"email\":\"admin$N@demo.test\",\"password\":\"password123\"}"
ADMIN=$(token admin$N@demo.test)
curl -s -o /dev/null -X POST $BASE/auth/users -H "authorization: Bearer $ADMIN" -H "$JSON" \
  -d "{\"email\":\"agent$N@demo.test\",\"password\":\"password123\",\"role\":\"agent\"}"
AGENT=$(token agent$N@demo.test)

WF=$(curl -s -X POST $BASE/workflows -H "authorization: Bearer $AGENT" \
  -H "Idempotency-Key: crash-demo-$N" -H "$JSON" \
  -d '{"customer_email":"ravi@example.com","subject":"Refund","body":"Charged twice, please refund Rs 2000"}' | field "['id']")
sleep 1
curl -s -o /dev/null -X POST $BASE/workflows/$WF/approve -H "authorization: Bearer $ADMIN"
sleep 2
echo "2. manager approved, the refund step is retrying:"
show

echo "3. kill -9 the server"
kill -9 $PID; wait $PID 2>/dev/null

echo "4. start a new server, provider is healthy again"
GATEWAY_FAILURE_RATE=0 uvicorn app.main:app --port $PORT >/tmp/relayforge_demo_b.log 2>&1 &
PID2=$!

for i in $(seq 1 30); do
  sleep 1
  STATUS=$(curl -s $BASE/workflows/$WF -H "authorization: Bearer $AGENT" 2>/dev/null | field "['status']" 2>/dev/null)
  if [ "$STATUS" = "completed" ]; then echo "5. picked up and finished after about ${i}s:"; break; fi
done
show

COUNT=$(python3 -c "
from sqlalchemy import select, func
from app.db import SessionLocal
from app.models import GatewayRefund
with SessionLocal() as db:
    print(db.scalar(select(func.count()).select_from(GatewayRefund).where(GatewayRefund.key == 'wf-$WF-refund')))")
echo "6. refunds the provider recorded for this workflow: $COUNT (should be 1)"

kill $PID2 2>/dev/null
[ "$STATUS" = "completed" ] && [ "$COUNT" = "1" ]
