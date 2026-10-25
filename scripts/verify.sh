#!/usr/bin/env bash
# @exports: none (executable script); exit 0 = every check passed, 1 = any FAIL
# @imports: package.json scripts compile, node, test, deploy:local; gridproof/cli.py:fetch, train, judge, issue, verify, evaluate, report; gridproof/claims.py:main (python -m gridproof.claims)
# @env: RPC_URL (optional, default http://127.0.0.1:8545; read only to wait for the local node)
# @schema: CLI `bash scripts/verify.sh [--live]`; default = offline snapshot mode on committed data/snapshots; --live also re-fetches public APIs (C01, live half of C10)
# @schema: prints "PASS <check>" or "FAIL <check>: <reason>" (claims print PASS/FAIL/SKIP per ledger id); writes results/*.json, results/verify.log; starts and stops its own local hardhat node and refuses to run if one is already listening
set -u
cd "$(dirname "$0")/.." || exit 1
export RPC_URL=http://127.0.0.1:8545 CHAIN_ID=31337 DEPLOYMENT_FILE=deployments/localhost.json
export ISSUER_PRIVATE_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
LIVE=0
for arg in "$@"; do
  case "$arg" in
    --live) LIVE=1 ;;
    *) echo "FAIL arguments: unknown option '$arg' (only --live is supported)"; exit 1 ;;
  esac
done

mkdir -p results data
LOG=results/verify.log
: > "$LOG"
FAILS=0
RPC="${RPC_URL:-http://127.0.0.1:8545}"
NODE_PID=""
RECEIPTS_BAK=""

pass() { echo "PASS $1"; }
fail() { echo "FAIL $1: $2"; FAILS=$((FAILS + 1)); }

restore_receipts() {
  if [ -n "$RECEIPTS_BAK" ] && [ -f "$RECEIPTS_BAK" ]; then
    cp "$RECEIPTS_BAK" data/receipts.json && rm -f "$RECEIPTS_BAK"
    RECEIPTS_BAK=""
  fi
}

cleanup() {
  if [ -n "$NODE_PID" ]; then
    kill -- "-$NODE_PID" 2>/dev/null || kill "$NODE_PID" 2>/dev/null
    wait "$NODE_PID" 2>/dev/null
  fi
  restore_receipts
}
trap cleanup EXIT

finish() {
  if [ "$FAILS" -eq 0 ]; then
    echo "ALL CHECKS PASSED (log: $LOG)"
    exit 0
  fi
  echo "$FAILS CHECK(S) FAILED (log: $LOG)"
  exit 1
}

step() {
  local name="$1"
  shift
  echo "== $name: $*" >> "$LOG"
  if "$@" >> "$LOG" 2>&1; then
    pass "$name"
    return 0
  fi
  fail "$name" "'$*' exited non-zero; last log lines: $(tail -n 4 "$LOG" | tr '\n' ' ')"
  return 1
}

rpc_up() {
  curl -s -m 2 -X POST -H 'content-type: application/json' \
    --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' "$RPC" 2>/dev/null | grep -q '"result"'
}

# ---- prerequisites
command -v node >/dev/null 2>&1 || { fail "prerequisite node" "node not found. Fix: install Node 20"; finish; }
NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
[ "$NODE_MAJOR" -ge 20 ] || { fail "prerequisite node" "Node $NODE_MAJOR found, 20+ required"; finish; }
command -v npm >/dev/null 2>&1 || { fail "prerequisite npm" "npm not found. Fix: install npm"; finish; }
command -v curl >/dev/null 2>&1 || { fail "prerequisite curl" "curl not found. Fix: apt install curl"; finish; }
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
"$PY" -c 'import numpy, requests, yaml, web3, matplotlib, tzdata' >> "$LOG" 2>&1 \
  || { fail "prerequisite python packages" "missing imports. Fix: $PY -m pip install -r requirements.txt"; finish; }
pass "prerequisites (node $NODE_MAJOR, $($PY -V 2>&1))"

# ---- toolchain
if [ ! -d node_modules/hardhat ]; then
  if [ -f package-lock.json ]; then step "npm ci" npm ci --no-audit --no-fund || finish
  else step "npm install" npm install --no-audit --no-fund || finish; fi
fi
step "compile contract" npm run compile || finish
step "hardhat contract tests" npm test || finish

# ---- local chain
if rpc_up; then
  fail "local node" "something already answers at $RPC. Fix: stop it; stale chain state would break the exactly-once checks"
  finish
fi
setsid npm run node >> results/node.log 2>&1 &
NODE_PID=$!
for _ in $(seq 1 80); do rpc_up && break; sleep 0.5; done
rpc_up || { fail "local node" "no answer at $RPC after 40 s; see results/node.log"; finish; }
pass "local node started"
step "deploy contract (local)" npm run deploy:local || finish

# ---- pipeline (committed snapshots; fetch only when none exist)
if ls data/snapshots/*.json >/dev/null 2>&1; then
  pass "snapshots present (offline mode)"
else
  step "fetch public data" "$PY" -m gridproof.cli fetch || finish
fi
if [ -f data/receipts.json ]; then
  RECEIPTS_BAK=$(mktemp)
  cp data/receipts.json "$RECEIPTS_BAK"
  rm -f data/receipts.json
fi
step "train envelope" "$PY" -m gridproof.cli train || finish
step "judge issue month" "$PY" -m gridproof.cli judge || finish
step "issue batches" "$PY" -m gridproof.cli issue || finish
step "verify roots (snapshot mode)" "$PY" -m gridproof.cli verify --mode snapshot || finish
if [ "$LIVE" -eq 1 ]; then
  step "verify roots (live mode)" "$PY" -m gridproof.cli verify --mode live || true
fi
step "evaluate" "$PY" -m gridproof.cli evaluate || finish

# ---- claims ledger C01..C14
if [ "$LIVE" -eq 1 ]; then
  "$PY" -m gridproof.claims --live
else
  "$PY" -m gridproof.claims
fi
RC=$?
[ "$RC" -eq 0 ] || FAILS=$((FAILS + 1))

# ---- report
restore_receipts
step "build report and web/data" "$PY" -m gridproof.cli report

finish
