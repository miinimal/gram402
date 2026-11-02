#!/bin/bash
# Agent economy on NEAR testnet: agent A buys a trip plan from agent B; B buys weather data from service C.
# A pays from a mandate its owner funded; B pays from a mandate B opened itself out of its revenue.
# All payments are HTTP 402 invoices settled through the mandate contract. Needs near-cli-rs, $WASM, server deps.
set -euo pipefail
cd "$(dirname "$0")/../.."
SUM=${GITHUB_STEP_SUMMARY:-/dev/stdout}
TAG=g402e$(date +%s)
OWNER=$TAG.testnet
CONTRACT=mandate.$OWNER; AGENT_A=a.$OWNER; SELLER=b.$OWNER; PROCURE=bbuy.$OWNER; DATA=c.$OWNER
NET="network-config testnet"
near_tx() { near "$@" $NET sign-with-legacy-keychain send; }
: > economy.log

near account create-account sponsor-by-faucet-service "$OWNER" autogenerate-new-keypair save-to-legacy-keychain $NET create
sleep 5
near_tx account create-account fund-myself "$CONTRACT" '3 NEAR' autogenerate-new-keypair save-to-legacy-keychain sign-as "$OWNER"
for acc in "$AGENT_A:0.3" "$SELLER:0.05" "$PROCURE:0.1" "$DATA:0.05"; do
  near_tx account create-account fund-myself "${acc%%:*}" "${acc##*:} NEAR" autogenerate-new-keypair save-to-legacy-keychain sign-as "$OWNER"
done
near_tx contract deploy "$CONTRACT" use-file "$WASM" with-init-call new json-args '{}' prepaid-gas '30 Tgas' attached-deposit '0 NEAR'

PLAN_PRICE=10000000000000000000000   # 0.01 NEAR: B's price for a plan
DATA_PRICE=1000000000000000000000    # 0.001 NEAR: C's price for weather
near_tx contract call-function as-transaction "$CONTRACT" create_mandate \
  json-args "{\"agent\":\"$AGENT_A\",\"per_payment_cap\":\"20000000000000000000000\",\"valid_for_ms\":3600000,\"merchants\":[\"$SELLER\"]}" \
  prepaid-gas '30 Tgas' attached-deposit '0.05 NEAR' sign-as "$OWNER"
echo "[owner of A] mandate 1: A may pay B up to 0.02 NEAR per plan" | tee -a economy.log

serve() {  # port merchant price upstream
  ( cd server && GRAM402_DB=$(mktemp -d)/s.db PAY402_NETWORK=testnet PAY402_ADDRESS=0QDIl6cScCeTVp5GLmbbxh8blQLvRXd-0J4T1fVsz7uEVQ_U \
    NEAR_MANDATE_CONTRACT=$CONTRACT NEAR_MERCHANT=$2 NEAR_PRICE_YOCTO=$3 UPSTREAM_URL=$4 \
    python3 -m uvicorn app:app --port $1 --log-level warning ) &
}
serve 8502 "$DATA" $DATA_PRICE ""; C_PID=$!
( CONTRACT=$CONTRACT SELLER=$SELLER PROCURE=$PROCURE DATA=$DATA DATA_URL=http://127.0.0.1:8502 \
  PROC_DEPOSIT='0.006 NEAR' PROC_CAP=2000000000000000000000 ECON_LOG=$PWD/economy.log \
  python3 -m uvicorn --app-dir near/e2e economy_brain:app --port 8601 --log-level warning ) & BRAIN=$!
serve 8402 "$SELLER" $PLAN_PRICE http://127.0.0.1:8601; B_PID=$!
trap 'kill $C_PID $BRAIN $B_PID 2>/dev/null' EXIT
for p in 8502 8601 8402; do for i in $(seq 1 60); do curl -s -m 1 -o /dev/null http://127.0.0.1:$p/ && break; sleep 0.5; done; done

CONTRACT=$CONTRACT AGENT_A=$AGENT_A ECON_LOG=$PWD/economy.log python3 near/e2e/economy_agent.py
near contract call-function as-read-only "$CONTRACT" get_mandate json-args '{"mandate_id":2}' $NET now | tee -a economy.log
{
  echo "## NEAR agent economy (testnet)"
  echo "- Contract: [$CONTRACT](https://testnet.nearblocks.io/address/$CONTRACT)"
  echo '```'; cat economy.log; echo '```'
} >> "$SUM"
