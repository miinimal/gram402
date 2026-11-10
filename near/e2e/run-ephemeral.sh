#!/bin/bash
# Real-money (mainnet) or rehearsal (testnet) end-to-end run with throwaway accounts.
#
# Every key is generated inside this CI job and never leaves it. On mainnet the job prints one funding address and
# waits; the owner sends NEAR to it from his own wallet. The job then deploys the mandate contract, lets an agent pay
# HTTP 402 invoices through it, proves the agent's restricted key cannot move money directly, revokes the mandate,
# deletes the throwaway accounts and returns everything that is left to the owner's account.
# Real cost: network fees plus the two paid requests (about 0.002 NEAR) and the small balance left on the agent.
#
# Env: NET=mainnet|testnet, OWNER_ACCOUNT (where the rest goes back; testnet defaults to the faucet account),
# WASM (contract), MAX_WAIT_MIN (mainnet only).
# Author: Aleksandr Shcherbakov (@miinimal).
set -euo pipefail
cd "$(dirname "$0")/../.."
NET=${NET:-testnet}
OWNER_ACCOUNT=${OWNER_ACCOUNT:-}
[ "$NET" = testnet ] || [ -n "$OWNER_ACCOUNT" ] || { echo "set OWNER_ACCOUNT for mainnet"; exit 1; }
MAX_WAIT_MIN=${MAX_WAIT_MIN:-60}
SUM=${GITHUB_STEP_SUMMARY:-/dev/stdout}
CRED=$HOME/.near-credentials/$NET
RPC=$([ "$NET" = mainnet ] && echo https://rpc.mainnet.near.org || echo https://rpc.testnet.near.org)
EXPLORER=$([ "$NET" = mainnet ] && echo https://nearblocks.io || echo https://testnet.nearblocks.io)
LOG=e2e.log
: > $LOG
mkdir -p "$CRED"
near_tx() { near "$@" network-config "$NET" sign-with-legacy-keychain send; }
yocto_balance() {
  curl -s -m 20 "$RPC" -H 'content-type: application/json' -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"query\",\"params\":{\"request_type\":\"view_account\",\"finality\":\"final\",\"account_id\":\"$1\"}}" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("result",{}).get("amount","0"))'
}
new_implicit() {  # generates a key pair into the legacy keychain and prints the implicit account id
  local before after
  before=$(ls "$CRED" | sort)
  near account create-account fund-later use-auto-generation save-to-folder "$CRED" >/dev/null 2>&1
  after=$(ls "$CRED" | sort)
  comm -13 <(echo "$before") <(echo "$after") | grep -E '^[0-9a-f]{64}\.json$' | head -1 | sed 's/\.json$//'
}

FUNDER=$(new_implicit); CONTRACT=$(new_implicit); AGENT=$(new_implicit); MERCHANT=$(new_implicit)
for a in FUNDER CONTRACT AGENT MERCHANT; do
  v=${!a}
  [ ${#v} -eq 64 ] || { echo "key generation failed for $a"; exit 1; }
  echo "$a=$v" | tee -a $LOG
done

NEED=1730000000000000000000000   # 1.73 NEAR: contract 1.5 + agent 0.1 + merchant 0.05 + mandate 0.05 + fees
if [ "$NET" = mainnet ]; then
  {
    echo "## Waiting for funding (mainnet)"
    echo "Send **2 NEAR** to \`$FUNDER\` within $MAX_WAIT_MIN minutes. Do not send after this job has stopped."
  } >> "$SUM"
  echo "SEND 2 NEAR TO: $FUNDER (waiting up to $MAX_WAIT_MIN min)" | tee -a $LOG
  deadline=$(( $(date +%s) + MAX_WAIT_MIN * 60 ))
  until python3 -c "import sys; sys.exit(0 if int('$(yocto_balance "$FUNDER")') >= $NEED else 1)"; do
    [ "$(date +%s)" -lt "$deadline" ] || { echo "no funding arrived; nothing was spent" | tee -a $LOG; exit 1; }
    sleep 15
  done
else
  TAG=g402e$(date +%s)
  SPONSOR=$TAG.testnet
  near account create-account sponsor-by-faucet-service "$SPONSOR" autogenerate-new-keypair save-to-legacy-keychain network-config testnet create
  sleep 5
  near_tx tokens "$SPONSOR" send-near "$FUNDER" '1.9 NEAR'
  OWNER_ACCOUNT=${OWNER_ACCOUNT:-$SPONSOR}
  until python3 -c "import sys; sys.exit(0 if int('$(yocto_balance "$FUNDER")') >= $NEED else 1)"; do sleep 3; done
fi
echo "== funded: $(yocto_balance "$FUNDER") yocto on $FUNDER" | tee -a $LOG

near_tx tokens "$FUNDER" send-near "$CONTRACT" '1.5 NEAR'
near_tx tokens "$FUNDER" send-near "$AGENT" '0.1 NEAR'
near_tx tokens "$FUNDER" send-near "$MERCHANT" '0.05 NEAR'
near_tx contract deploy "$CONTRACT" use-file "$WASM" with-init-call new json-args '{}' prepaid-gas '30 Tgas' attached-deposit '0 NEAR'

PRICE=1000000000000000000000      # 0.001 NEAR per request
CAP=2000000000000000000000        # 0.002 NEAR per payment
near_tx contract call-function as-transaction "$CONTRACT" create_mandate \
  json-args "{\"agent\":\"$AGENT\",\"per_payment_cap\":\"$CAP\",\"valid_for_ms\":3600000,\"merchants\":[\"$MERCHANT\"]}" \
  prepaid-gas '30 Tgas' attached-deposit '0.02 NEAR' sign-as "$FUNDER" | tee -a $LOG

( cd server && GRAM402_DB=$(mktemp -d)/e2e.db PAY402_NETWORK=testnet PAY402_ADDRESS=0QDIl6cScCeTVp5GLmbbxh8blQLvRXd-0J4T1fVsz7uEVQ_U \
  NEAR_NETWORK=$NET NEAR_MANDATE_CONTRACT=$CONTRACT NEAR_MERCHANT=$MERCHANT NEAR_PRICE_YOCTO=$PRICE \
  python3 -m uvicorn app:app --port 8402 --log-level warning ) & S=$!
trap 'kill $S 2>/dev/null || true' EXIT
for i in $(seq 1 40); do curl -sf -m 1 http://127.0.0.1:8402/health >/dev/null && break; sleep 0.5; done

NEAR_NET=$NET CONTRACT=$CONTRACT AGENT=$AGENT MANDATE_ID=1 CAP_YOCTO=$CAP python3 near/e2e/agent.py | tee -a $LOG

# The agent keeps only a key that may call `pay` on the mandate contract; its direct transfer must fail.
near_tx tokens "$AGENT" send-near "$FUNDER" '0.07 NEAR'    # leave the agent only what it needs for gas
FULL_KEY=$(python3 -c "import json;print(json.load(open('$CRED/$AGENT.json'))['public_key'])")
near_tx account add-key "$AGENT" grant-function-call-access --allowance '0.02 NEAR' --contract-account-id "$CONTRACT" \
  --function-names pay autogenerate-new-keypair save-to-legacy-keychain
near_tx account delete-keys "$AGENT" public-keys "$FULL_KEY"
FC_FILE=$(ls "$CRED/$AGENT"/*.json | grep -v "${FULL_KEY#ed25519:}" | head -1)
cp "$FC_FILE" "$CRED/$AGENT.json"
echo "== agent keys after sandboxing:" | tee -a $LOG
near account list-keys "$AGENT" network-config "$NET" now 2>&1 | tee -a $LOG
NEAR_NET=$NET CONTRACT=$CONTRACT AGENT=$AGENT MANDATE_ID=1 CAP_YOCTO=$CAP ONLY_ONE=1 python3 near/e2e/agent.py | tee -a $LOG
if near_tx tokens "$AGENT" send-near "$OWNER_ACCOUNT" '0.005 NEAR' >steal.log 2>&1; then
  echo "SECURITY FAIL: the restricted agent moved NEAR outside the mandate" | tee -a $LOG; exit 1
fi
echo "== restricted agent could not send NEAR directly (only pay via the mandate is allowed)" | tee -a $LOG

near contract call-function as-read-only "$CONTRACT" get_mandate json-args '{"mandate_id":1}' network-config "$NET" now | tee -a $LOG
near_tx contract call-function as-transaction "$CONTRACT" revoke json-args '{"mandate_id":1}' \
  prepaid-gas '30 Tgas' attached-deposit '0 NEAR' sign-as "$FUNDER" | tee -a $LOG

# Return everything: the merchant and the contract go to the funder, the funder goes to the owner.
near_tx account delete-account "$MERCHANT" beneficiary "$FUNDER"
if ! near_tx account delete-account "$CONTRACT" beneficiary "$FUNDER"; then
  # an account holding a large contract may be refused deletion: replace the code with an empty module first
  printf '\x00asm\x01\x00\x00\x00' > empty.wasm
  near_tx contract deploy "$CONTRACT" use-file empty.wasm without-init-call
  near_tx account delete-account "$CONTRACT" beneficiary "$FUNDER"
fi
near_tx account delete-account "$FUNDER" beneficiary "$OWNER_ACCOUNT"
echo "== returned the rest to $OWNER_ACCOUNT; left on the restricted agent: $(yocto_balance "$AGENT") yocto" | tee -a $LOG

{
  echo "## NEAR $NET end-to-end with throwaway accounts"
  echo "- Contract (deleted after the run, history stays): [$CONTRACT]($EXPLORER/address/$CONTRACT)"
  echo "- Agent: [$AGENT]($EXPLORER/address/$AGENT) · Merchant: [$MERCHANT]($EXPLORER/address/$MERCHANT) · Funder: [$FUNDER]($EXPLORER/address/$FUNDER)"
  echo "- Two requests paid through the mandate; a payment over the per-payment cap was refused by the contract."
  echo "- The agent, left with a pay-only key, paid again; its direct NEAR transfer was rejected."
  echo "- Mandate revoked; throwaway accounts deleted; the rest returned to $OWNER_ACCOUNT."
} >> "$SUM"
