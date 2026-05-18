#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-configs/linux-smoke-miniswecompat-xhigh.json}"
SESSION="${2:-}"
PROGRAMBENCH_REPO="${PROGRAMBENCH_REPO:-}"
SKIP_DOCTOR=0
ALLOW_PARTIAL="${ALLOW_PARTIAL:-0}"
PUBLISH="${PUBLISH:-0}"
OFFLINE_REPORT="${OFFLINE_REPORT:-0}"
NO_TARGET_REFRESH="${NO_TARGET_REFRESH:-0}"
SYNC_REPO="${SYNC_REPO:-1}"
INCREMENTAL_FINALIZE="${INCREMENTAL_FINALIZE:-0}"
MAX_PARALLEL="${MAX_PARALLEL:-}"

usage() {
  cat <<'EOF'
Usage:
  scripts/start-sweep-tmux.sh [config] [tmux-session]

Starts scripts/run-sweep.sh in a detached tmux session and logs output under
local_state/logs/. PROGRAMBENCH_REPO is passed through when set; otherwise
run-sweep.sh auto-detects a sibling ../ProgramBench checkout.

Environment toggles:
  RUN_VERSION=VERSION  Resume/write a specific run version instead of creating a new one.
  SKIP_DOCTOR=1       Skip the pre-launch scripts/doctor.sh check.
  ALLOW_PARTIAL=1     Permit finalize/report output before all targets finish.
  PUBLISH=1           Commit and push docs/ after rebuilding the report.
  OFFLINE_REPORT=1    Use cached pricing and ProgramBench baseline data.
  NO_TARGET_REFRESH=1 Do not refresh target_sets/all_tasks.txt.
  SYNC_REPO=0          Do not fetch and fast-forward this repo before launch.
  INCREMENTAL_FINALIZE=1 Evaluate/report after each watch tick.
  MAX_PARALLEL=N       Override config max_parallel for this launch.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "config not found: $CONFIG" >&2
  exit 1
fi

if [[ "$SYNC_REPO" == "1" ]]; then
  if [[ -n "$(git status --porcelain)" ]]; then
    echo "repo has local changes; refusing to auto-sync before launch" >&2
    echo "commit/stash them, or launch with SYNC_REPO=0 for an intentional local run" >&2
    git status --short >&2
    exit 1
  fi
  git fetch origin main
  git merge --ff-only origin/main
fi

if [[ "${SKIP_DOCTOR:-0}" != "1" ]]; then
  scripts/doctor.sh "$CONFIG"
fi

if [[ -z "$SESSION" ]]; then
  batch_name="$(uv run python - "$CONFIG" <<'PY'
import json
import sys

print(json.loads(open(sys.argv[1]).read())["batch_name"])
PY
)"
  SESSION="pb-goal-${batch_name}"
fi

mkdir -p local_state/logs
log="local_state/logs/${SESSION}.log"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "tmux session already exists: $SESSION" >&2
  echo "attach with: tmux attach -t $SESSION" >&2
  exit 1
fi

cmd=(scripts/run-sweep.sh --config "$CONFIG")
if [[ -n "$PROGRAMBENCH_REPO" ]]; then
  cmd+=(--programbench-repo "$PROGRAMBENCH_REPO")
fi
if [[ "$ALLOW_PARTIAL" == "1" ]]; then
  cmd+=(--allow-partial)
fi
if [[ "$PUBLISH" == "1" ]]; then
  cmd+=(--publish)
fi
if [[ "$OFFLINE_REPORT" == "1" ]]; then
  cmd+=(--offline-report)
fi
if [[ "$NO_TARGET_REFRESH" == "1" ]]; then
  cmd+=(--no-target-refresh)
fi
if [[ "$INCREMENTAL_FINALIZE" == "1" ]]; then
  cmd+=(--incremental-finalize)
fi
if [[ -n "$MAX_PARALLEL" ]]; then
  cmd+=(--max-parallel "$MAX_PARALLEL")
fi

env_prefix=()
if [[ -n "${RUN_VERSION:-}" ]]; then
  env_prefix+=(RUN_VERSION="$RUN_VERSION")
fi
for key in PB_CODEX_PROXY_URL PB_CODEX_NO_PROXY; do
  if [[ -n "${!key:-}" ]]; then
    env_prefix+=("$key=${!key}")
  fi
done

tmux new-session -d -s "$SESSION" -c "$PWD" "$(printf '%q ' "${env_prefix[@]}" "${cmd[@]}") 2>&1 | tee -a $(printf '%q' "$log")"

echo "started tmux session: $SESSION"
echo "log: $log"
echo "attach: tmux attach -t $SESSION"
echo "status: uv run python scripts/run-config.py status $CONFIG"
