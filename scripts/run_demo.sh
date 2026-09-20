#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src:$ROOT${PYTHONPATH:+:$PYTHONPATH}"

MODE="genuine"
if [[ "${1:-}" == "--fixture" ]]; then
  MODE="fixture"
elif [[ $# -gt 0 ]]; then
  echo "usage: scripts/run_demo.sh [--fixture]" >&2
  exit 64
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "missing .venv; run: uv sync --all-extras" >&2
  exit 2
fi
if [[ ! -x .venv/bin/playwright ]]; then
  echo "Playwright is not installed; run: uv sync --all-extras" >&2
  exit 2
fi
if [[ "$MODE" == "genuine" ]]; then
  if ! .venv/bin/python -c 'from computer_use.config import Settings; settings = Settings(); raise SystemExit(0 if settings.anthropic_api_key or settings.openai_api_key else 1)'; then
    echo "Genuine discovery is required but neither ANTHROPIC_API_KEY nor OPENAI_API_KEY is configured." >&2
    echo "Set one key in the environment or untracked .env, then rerun: scripts/run_demo.sh" >&2
    echo "For an explicitly non-submission scripted exercise: scripts/run_demo.sh --fixture" >&2
    exit 2
  fi
fi

ORIGIN="${COMPUTER_USE_DEMO_ORIGIN:-http://127.0.0.1:8765}"
PORT="${ORIGIN##*:}"
if [[ ! "$PORT" =~ ^[0-9]+$ ]]; then
  echo "COMPUTER_USE_DEMO_ORIGIN must end in a numeric port: $ORIGIN" >&2
  exit 2
fi
if curl --silent --show-error --max-time 1 "$ORIGIN/health" >/dev/null 2>&1; then
  echo "port $PORT is already serving an application; refusing to reuse it" >&2
  exit 2
fi

mkdir -p runtime
.venv/bin/python -m computer_use.cli demo-app --port "$PORT" >runtime/demo-app.log 2>&1 &
DEMO_PID=$!
cleanup() {
  if kill -0 "$DEMO_PID" 2>/dev/null; then
    kill "$DEMO_PID" 2>/dev/null || true
    wait "$DEMO_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

ready=0
for _ in {1..100}; do
  if curl --silent --show-error --max-time 1 "$ORIGIN/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.05
done
if [[ "$ready" -ne 1 ]]; then
  echo "demo app failed its health check; see runtime/demo-app.log" >&2
  exit 1
fi

reset_generated_evidence() {
  local directory="$1"
  case "$directory" in
    evidence/discovery|evidence/fixture-discovery|evidence/replay-success|evidence/replay-not-found|evidence/replay-retry|evidence/replay-permission-denied|evidence/handoff) ;;
    *)
      echo "refusing to clear unexpected evidence path: $directory" >&2
      exit 2
      ;;
  esac
  mkdir -p "$directory"
  find "$directory" -mindepth 1 -delete
}

if [[ "$MODE" == "genuine" ]]; then
  TRACE_DIR="evidence/discovery"
  reset_generated_evidence "$TRACE_DIR"
  scripts/cu discover \
    --goal-file artifacts/examples/lookup_goal.json \
    --input member_id=12345 \
    --output "$TRACE_DIR" \
    --headless
else
  TRACE_DIR="evidence/fixture-discovery"
  reset_generated_evidence "$TRACE_DIR"
  scripts/cu fixture-discover --member-id 12345 --output "$TRACE_DIR" --headless
fi

for directory in \
  evidence/replay-success \
  evidence/replay-not-found \
  evidence/replay-retry \
  evidence/replay-permission-denied \
  evidence/handoff; do
  reset_generated_evidence "$directory"
done

scripts/cu compile \
  --trace "$TRACE_DIR/discovery-trace.json" \
  --output artifacts/examples/lookup_member_balance.v1.json
scripts/cu approve \
  --artifact artifacts/examples/lookup_member_balance.v1.json \
  --approved-by local-demo-reviewer

curl --silent --show-error --request POST "$ORIGIN/__dev__/reset" >/dev/null
scripts/cu replay \
  --artifact artifacts/examples/lookup_member_balance.v1.json \
  --input member_id=77777 \
  --evidence-dir evidence/replay-success \
  --headless
scripts/cu replay \
  --artifact artifacts/examples/lookup_member_balance.v1.json \
  --input member_id=99999 \
  --evidence-dir evidence/replay-not-found \
  --headless
curl --silent --show-error --request POST "$ORIGIN/__dev__/reset" >/dev/null
scripts/cu replay \
  --artifact artifacts/examples/lookup_member_balance.v1.json \
  --input member_id=77777 \
  --evidence-dir evidence/replay-retry \
  --headless
if scripts/cu replay \
  --artifact artifacts/examples/lookup_member_balance.v1.json \
  --input member_id=55555 \
  --evidence-dir evidence/replay-permission-denied \
  --headless; then
  echo "permission-denied fixture unexpectedly succeeded" >&2
  exit 1
fi
scripts/cu handoff-demo \
  --artifact artifacts/examples/open_sub_account.v1.json \
  --evidence-dir evidence/handoff \
  --headless \
  --automated-fixture-human

echo
echo "Demo complete ($MODE discovery)."
echo "  trace:    $TRACE_DIR/discovery-trace.json"
echo "  artifact: artifacts/examples/lookup_member_balance.v1.json"
echo "  success:  evidence/replay-success/result.json"
echo "  outcome:  evidence/replay-not-found/result.json"
echo "  retry:    evidence/replay-retry/result.json"
echo "  failure:  evidence/replay-permission-denied/result.json"
echo "  handoff:  evidence/handoff/control-events.jsonl"
if [[ "$MODE" == "fixture" ]]; then
  echo "  warning: scripted fixture discovery is not genuine provider evidence"
fi
