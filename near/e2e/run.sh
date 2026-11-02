#!/bin/bash
# Live NEAR testnet run: fresh accounts from the faucet, contract deploy, mandate, agent payments through the
# GRAM-402 server. Needs: near (near-cli-rs), the contract wasm at $WASM, python deps of server/.
set -euo pipefail
cd "$(dirname "$0")/../.."
SUM=${GITHUB_STEP_SUMMARY:-/dev/stdout}
TAG=g402$(date +%s)
OWNER=$TAG-owner.testnet
CONTRACT=mandate.$OWNER
AGENT=agent.$OWNER
MERCHANT=shop.$OWNER
NET="network-config testnet"
near_tx() { near "$@" $NET sign-with-legacy-keychain send; }

near account create-account sponsor-by-faucet-service "$OWNER" autogenerate-new-keypair save-to-legacy-keychain $NET create
sleep 5
near_tx account create-account fund-myself "$CONTRACT" '3 NEAR' autogenerate-new-keypair save-to-legacy-keychain sign-as "$OWNER"
near_tx account create-account fund-myself "$AGENT" '0.5 NEAR' autogenerate-new-keypair save-to-legacy-keychain sign-as "$OWNER"
near_tx account create-account fund-myself "$MERCHANT" '0.1 NEAR' autogenerate-new-keypair save-to-legacy-keychain sign-as "$OWNER"
near_tx contract deploy "$CONTRACT" use-file "$WASM" with-init-call new json-args '{}' prepaid-gas '30 Tgas' attached-deposit '0 NEAR'

PRICE=1000000000000000000000      # 0.001 NEAR per request
CAP=2000000000000000000000        # cap 0.002 NEAR per payment
near_tx contract call-function as-transaction "$CONTRACT" create_mandate \
  json-args "{\"agent\":\"$AGENT\",\"per_payment_cap\":\"$CAP\",\"valid_for_ms\":3600000,\"merchants\":[\"$MERCHANT\"]}" \
  prepaid-gas '30 Tgas' attached-deposit '0.01 NEAR' sign-as "$OWNER"

( cd server && GRAM402_DB=$(mktemp -d)/e2e.db PAY402_NETWORK=testnet PAY402_ADDRESS=0QDIl6cScCeTVp5GLmbbxh8blQLvRXd-0J4T1fVsz7uEVQ_U \
  NEAR_MANDATE_CONTRACT=$CONTRACT NEAR_MERCHANT=$MERCHANT NEAR_PRICE_YOCTO=$PRICE \
  python3 -m uvicorn app:app --port 8402 --log-level warning ) & S=$!
trap 'kill $S 2>/dev/null' EXIT
for i in $(seq 1 40); do curl -sf -m 1 http://127.0.0.1:8402/health >/dev/null && break; sleep 0.5; done

CONTRACT=$CONTRACT AGENT=$AGENT MANDATE_ID=1 CAP_YOCTO=$CAP python3 near/e2e/agent.py | tee e2e.log

near contract call-function as-read-only "$CONTRACT" get_mandate json-args '{"mandate_id":1}' $NET now | tee -a e2e.log
{
  echo "## NEAR testnet end-to-end"
  echo "- Contract: [$CONTRACT](https://testnet.nearblocks.io/address/$CONTRACT)"
  echo "- Owner: $OWNER · Agent: [$AGENT](https://testnet.nearblocks.io/address/$AGENT) · Merchant: [$MERCHANT](https://testnet.nearblocks.io/address/$MERCHANT)"
  echo "- Two requests paid through the mandate; a payment over the per-payment cap was refused by the contract."
} >> "$SUM"
