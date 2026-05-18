#!/usr/bin/env bash
set -euo pipefail

CONFIG="${1:-configs/cpx62-paperprompt-xhigh.json}"
RUN_VERSION="${RUN_VERSION:-${2:-}}"
PROGRAMBENCH_REPO="${PROGRAMBENCH_REPO:-}"
SESSION="${3:-}"
MODEL="${BABYSITTER_MODEL:-gpt-5.5}"
REASONING_EFFORT="${BABYSITTER_REASONING_EFFORT:-high}"

usage() {
  cat <<'EOF'
Usage:
  RUN_VERSION=VERSION scripts/start-babysitter-tmux.sh [config] [run-version] [tmux-session]

Starts a detached Codex /goal babysitter for an existing ProgramBench sweep.
It monitors state/logs, patches harness issues, and avoids launching duplicate
sweeps. PROGRAMBENCH_REPO defaults to sibling ../ProgramBench when present.

Environment:
  RUN_VERSION=VERSION              Existing run version to babysit
  PROGRAMBENCH_REPO=PATH           ProgramBench checkout
  BABYSITTER_MODEL=MODEL           Default: gpt-5.5
  BABYSITTER_REASONING_EFFORT=EFF  Default: high
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
if [[ -z "$RUN_VERSION" ]]; then
  echo "RUN_VERSION is required for babysitting an existing sweep" >&2
  exit 1
fi

if [[ -z "$PROGRAMBENCH_REPO" ]]; then
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  candidate="$(cd "$script_dir/.." && pwd)/../ProgramBench"
  if [[ -d "$candidate/src/programbench" ]]; then
    PROGRAMBENCH_REPO="$(cd "$candidate" && pwd)"
  fi
fi
if [[ -z "$PROGRAMBENCH_REPO" ]]; then
  echo "ProgramBench repo not found. Set PROGRAMBENCH_REPO." >&2
  exit 1
fi

batch_name="$(uv run python - "$CONFIG" <<'PY'
import json
import sys

print(json.loads(open(sys.argv[1]).read())["batch_name"])
PY
)"
run_root="$(uv run python - "$CONFIG" "$RUN_VERSION" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text())
print(config.get("run_root") or str(Path("~/pb-goal-runs").expanduser() / config["batch_name"] / sys.argv[2]))
PY
)"

if [[ -z "$SESSION" ]]; then
  SESSION="pb-goal-babysitter-${batch_name}-${RUN_VERSION}"
fi
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "tmux session already exists: $SESSION" >&2
  echo "attach with: tmux attach -t $SESSION" >&2
  exit 1
fi

mkdir -p "local_state/babysitter/$RUN_VERSION" local_state/logs
prompt="local_state/babysitter/$RUN_VERSION/GLOBAL_GOAL_PROMPT.md"
notes="local_state/babysitter/$RUN_VERSION/notes.md"
log="local_state/logs/${SESSION}.log"
coordinator_session="pb-goal-${batch_name}-watch"
coordinator_log="local_state/logs/${coordinator_session}.log"
if [[ ! -f "$coordinator_log" ]]; then
  coordinator_log="local_state/logs/pb-goal-${batch_name}-${RUN_VERSION}.log"
fi
touch "$notes"

uv run python - \
  prompts/sweep_babysitter_goal.md \
  "$prompt" \
  "$CONFIG" \
  "$batch_name" \
  "$RUN_VERSION" \
  "$PROGRAMBENCH_REPO" \
  "$run_root" \
  "$coordinator_log" <<'PY'
import sys
from pathlib import Path

template, output, config, batch_name, run_version, programbench_repo, run_root, coordinator_log = sys.argv[1:]
text = Path(template).read_text()
for key, value in {
    "CONFIG": config,
    "BATCH_NAME": batch_name,
    "RUN_VERSION": run_version,
    "PROGRAMBENCH_REPO": programbench_repo,
    "RUN_ROOT": run_root,
    "COORDINATOR_LOG": coordinator_log,
}.items():
    text = text.replace("{{" + key + "}}", value)
Path(output).write_text(text)
PY

CODEX_BYPASS_FLAG="--yolo"
if ! codex --yolo --version >/dev/null 2>&1; then
  CODEX_BYPASS_FLAG="--dangerously-bypass-approvals-and-sandbox"
fi

tmux new-session -d -s "$SESSION" -c "$PWD" \
  "codex --enable goals --disable plugins --disable apps -m $(printf '%q' "$MODEL") -c model_reasoning_effort=$(printf '%q' "$REASONING_EFFORT") -c trust_level=trusted -C $(printf '%q' "$PWD") $CODEX_BYPASS_FLAG --no-alt-screen"
tmux pipe-pane -o -t "$SESSION" "cat >> $(printf '%q' "$log")"
sleep 4
tmux send-keys -t "$SESSION" "/goal Babysit ProgramBench sweep ${batch_name}/${RUN_VERSION}; keep it running, patch harness issues, and stop only when terminal or blocked." Enter
sleep 2
tmux load-buffer "$prompt"
tmux paste-buffer -t "$SESSION"
tmux send-keys -t "$SESSION" Enter

echo "started babysitter tmux session: $SESSION"
echo "prompt: $prompt"
echo "notes: $notes"
echo "log: $log"
echo "attach: tmux attach -t $SESSION"
