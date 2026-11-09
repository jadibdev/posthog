#!/usr/bin/env bash
# Starts a personhog replica and router built from this branch, then runs the replace benchmark.
# Usage: run_bench.sh smoke | removal | replace | all | repeats | memory
set -uo pipefail
# Runs from the checkout that holds this script, so a fresh worktree of this branch measures its own code.
cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
export RUSTUP_HOME=/tmp/rustup CARGO_HOME=/tmp/cargo PATH="/tmp/cargo/bin:$PATH"
# Uses the active Python environment, or the checkout's own .venv when none is active.
[ -n "${VIRTUAL_ENV:-}" ] || . .venv/bin/activate

BENCH_DIR=products/cohorts/backend/benchmarks

start_personhog() {
  if ! (echo > /dev/tcp/127.0.0.1/50052) 2>/dev/null; then
    RUST_LOG=warn GRPC_ADDRESS=127.0.0.1:50051 METRICS_PORT=9110 \
      PRIMARY_DATABASE_URL=postgres://posthog:posthog@localhost:5432/posthog_persons \
      nohup /tmp/cargo-target/release/personhog-replica > /tmp/ph-replica.log 2>&1 &
    for _ in $(seq 1 30); do (echo > /dev/tcp/127.0.0.1/50051) 2>/dev/null && break; sleep 1; done
    RUST_LOG=warn GRPC_ADDRESS=127.0.0.1:50052 METRICS_PORT=9111 REPLICA_URL=http://127.0.0.1:50051 \
      BACKEND_TIMEOUT_MS=30000 nohup /tmp/cargo-target/release/personhog-router > /tmp/ph-router.log 2>&1 &
    for _ in $(seq 1 30); do (echo > /dev/tcp/127.0.0.1/50052) 2>/dev/null && break; sleep 1; done
  fi
  (echo > /dev/tcp/127.0.0.1/50052) 2>/dev/null && echo "personhog up" || { echo "personhog DOWN"; tail -5 /tmp/ph-replica.log /tmp/ph-router.log; exit 1; }
}

# The ClickHouse database the dev stack was migrated into. A bare manage.py migrates into "default", hogli into "posthog".
export CLICKHOUSE_DATABASE="${CLICKHOUSE_DATABASE:-default}"
export DEBUG=1 PERSONHOG_ADDR=127.0.0.1:50052 PERSONHOG_TIMEOUT_MS=60000
export PERSONS_DATABASE_URL=postgres://posthog:posthog@localhost:5432/posthog_persons
start_personhog

BENCH="python $BENCH_DIR/replace_benchmark.py"
case "${1:-smoke}" in
  smoke)
    $BENCH cell removal 1000 per_person
    $BENCH cell removal 1000 bulk
    $BENCH cell replace 10000 0.05
    ;;
  removal) $BENCH matrix --only removal --out $BENCH_DIR/results_removal.json ;;
  replace) $BENCH matrix --only replace --out $BENCH_DIR/results_replace.json ;;
  all) $BENCH matrix --out $BENCH_DIR/results.json ;;
  repeats) $BENCH repeats --n 3 --out $BENCH_DIR/results_repeats.json ;;
  memory) $BENCH memory --n 1 --out $BENCH_DIR/results_memory.json ;;
esac
