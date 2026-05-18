#!/usr/bin/env bash
set -euo pipefail

CONFIG="configs/full-miniswecompat-xhigh.json"
RUN_VERSION="${RUN_VERSION:-}"
PROGRAMBENCH_REPO="${PROGRAMBENCH_REPO:-}"
WATCH=1
FINALIZE=1
SITE=1
PUBLISH=0
DRY_RUN=0
ONCE=0
ALLOW_PARTIAL=0
INCREMENTAL_FINALIZE=0
REFRESH_REPORT=1
REFRESH_TARGET_SET=1
MAX_PARALLEL="${MAX_PARALLEL:-}"
SITE_RESULTS_SCOPE="${SITE_RESULTS_SCOPE:-}"
FINALIZE_LIMIT="${FINALIZE_LIMIT:-}"

usage() {
  cat <<'EOF'
Usage:
  scripts/run-sweep.sh [options]

Options:
  --config PATH              Batch config JSON (default: configs/full-miniswecompat-xhigh.json)
  --programbench-repo PATH   ProgramBench checkout used for target metadata and evaluation
                             (default: PROGRAMBENCH_REPO or sibling ../ProgramBench)
  --skip-watch               Do not run/watch Codex sessions
  --skip-finalize            Do not package/evaluate completed sessions
  --site-only                Only export evidence and rebuild docs
  --no-site                  Do not rebuild docs
  --publish                  Commit docs/ and push after rebuilding the site
  --dry-run                  Print commands without running them
  --once                     Pass --once to scripts/run-config.py watch
  --allow-partial            Allow finalize/report publish before every target is evaluated
  --incremental-finalize      Evaluate/report after each watch tick instead of waiting for all tasks
  --max-parallel N           Override config max_parallel for Codex task concurrency
  --site-results-scope SCOPE  Results included in docs: all or current
                             (default: current for published watch runs, else all)
  --finalize-limit N          Max completed tasks to evaluate per finalize pass
                             (default: 1 for --incremental-finalize, else all)
  --offline-report           Do not refresh OpenAI pricing or ProgramBench baseline rows
  --no-target-refresh        Do not regenerate target_sets/all_tasks.txt
  -h, --help                 Show this help

Examples:
  scripts/run-sweep.sh --dry-run
  scripts/run-sweep.sh
  scripts/run-sweep.sh --config configs/full-miniswecompat-high.json
  scripts/run-sweep.sh --site-only --publish
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG="$2"
      shift 2
      ;;
    --programbench-repo)
      PROGRAMBENCH_REPO="$2"
      shift 2
      ;;
    --skip-watch)
      WATCH=0
      shift
      ;;
    --skip-finalize)
      FINALIZE=0
      shift
      ;;
    --site-only)
      WATCH=0
      FINALIZE=0
      SITE=1
      shift
      ;;
    --no-site)
      SITE=0
      shift
      ;;
    --publish)
      PUBLISH=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --once)
      ONCE=1
      shift
      ;;
    --allow-partial)
      ALLOW_PARTIAL=1
      shift
      ;;
    --incremental-finalize)
      INCREMENTAL_FINALIZE=1
      ALLOW_PARTIAL=1
      shift
      ;;
    --max-parallel)
      MAX_PARALLEL="$2"
      shift 2
      ;;
    --site-results-scope)
      SITE_RESULTS_SCOPE="$2"
      shift 2
      ;;
    --finalize-limit)
      FINALIZE_LIMIT="$2"
      shift 2
      ;;
    --offline-report)
      REFRESH_REPORT=0
      shift
      ;;
    --no-target-refresh)
      REFRESH_TARGET_SET=0
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! -f "$CONFIG" ]]; then
  echo "config not found: $CONFIG" >&2
  exit 1
fi

if [[ -z "$SITE_RESULTS_SCOPE" ]]; then
  SITE_RESULTS_SCOPE="all"
  if [[ "$PUBLISH" -eq 1 && "$WATCH" -eq 1 ]]; then
    SITE_RESULTS_SCOPE="current"
  fi
fi
if [[ "$SITE_RESULTS_SCOPE" != "all" && "$SITE_RESULTS_SCOPE" != "current" ]]; then
  echo "invalid --site-results-scope: $SITE_RESULTS_SCOPE" >&2
  exit 2
fi

if [[ -z "$RUN_VERSION" ]]; then
  RUN_VERSION="$(date -u +%Y%m%dT%H%M%SZ)"
fi
export RUN_VERSION
echo "run_version=$RUN_VERSION"
echo "site_results_scope=$SITE_RESULTS_SCOPE"

if [[ -z "$PROGRAMBENCH_REPO" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  CANDIDATE_PROGRAMBENCH_REPO="$(cd "$SCRIPT_DIR/.." && pwd)/../ProgramBench"
  if [[ -d "$CANDIDATE_PROGRAMBENCH_REPO/src/programbench" ]]; then
    PROGRAMBENCH_REPO="$(cd "$CANDIDATE_PROGRAMBENCH_REPO" && pwd)"
  fi
fi

if [[ -z "$PROGRAMBENCH_REPO" && ( "$FINALIZE" -eq 1 || "$REFRESH_TARGET_SET" -eq 1 ) ]]; then
  echo "ProgramBench repo not found. Set PROGRAMBENCH_REPO or pass --programbench-repo." >&2
  echo "For the default sibling checkout, run: scripts/bootstrap-programbench.sh" >&2
  exit 1
fi

print_cmd() {
  printf '+'
  printf ' %q' "$@"
  printf '\n'
}

run() {
  print_cmd "$@"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    "$@"
  fi
}

config_value() {
  uv run python - "$CONFIG" "$1" <<'PY'
import json
import sys

print(json.loads(open(sys.argv[1]).read())[sys.argv[2]])
PY
}

validate_config_policy() {
  uv run python - "$CONFIG" "$DRY_RUN" <<'PY'
import json
import os
import platform
import sys

config = json.loads(open(sys.argv[1]).read())
dry_run = sys.argv[2] == "1"
mode = config.get("inference_mode")
if mode in {"mini-swe-compatible-nointernet", "paper-prompt-nointernet", "paper-prompt-goal-contract-nointernet"} and not config.get("strict_egress"):
    raise SystemExit(f"{mode} configs must set strict_egress=true")
if dry_run:
    raise SystemExit(0)
if config.get("strict_egress") and platform.system() != "Linux":
    raise SystemExit("strict egress is only implemented for Linux hosts")
if config.get("strict_egress") and os.geteuid() == 0 and not config.get("codex_user"):
    raise SystemExit("strict egress under root requires codex_user so only the Codex UID is firewalled")
if config.get("codex_user") == "root":
    raise SystemExit("strict egress must run Codex as a dedicated non-root user")
PY
}

poll_seconds() {
  uv run python - "$CONFIG" <<'PY'
import json
import sys

print(json.loads(open(sys.argv[1]).read()).get("poll_seconds", 60))
PY
}

collect_results_csvs() {
  if [[ "$SITE_RESULTS_SCOPE" == "current" ]]; then
    uv run python - "$CONFIG" "$RUN_VERSION" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text())
path = Path("local_state/batches") / config["batch_name"] / sys.argv[2] / "results.csv"
if path.is_file():
    print(path)
PY
    return
  fi
  {
    find local_state -maxdepth 1 -type f -name '*results.csv'
    find local_state/batches -mindepth 2 -maxdepth 3 -type f -name 'results.csv' 2>/dev/null || true
  } | sort
}

batch_complete() {
  uv run python - "$CONFIG" "$RUN_VERSION" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text())
version = sys.argv[2] or str(config.get("run_version") or "")
path = (
    Path("local_state/batches") / config["batch_name"] / version / "state.json"
    if version
    else Path("local_state/batches") / f"{config['batch_name']}.json"
)
if not path.is_file():
    raise SystemExit(1)
items = json.loads(path.read_text())["items"].values()
raise SystemExit(
    0
    if items and all(item["status"] in {"evaluated", "failed", "finalize_failed"} for item in items)
    else 1
)
PY
}

state_exists() {
  uv run python - "$CONFIG" "$RUN_VERSION" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text())
version = sys.argv[2] or str(config.get("run_version") or "")
path = (
    Path("local_state/batches") / config["batch_name"] / version / "state.json"
    if version
    else Path("local_state/batches") / f"{config['batch_name']}.json"
)
raise SystemExit(0 if path.is_file() else 1)
PY
}

run_finalize() {
  finalize_cmd=(uv run python scripts/run-config.py finalize "$CONFIG" --programbench-repo "$PROGRAMBENCH_REPO")
  if [[ -n "$FINALIZE_LIMIT" ]]; then
    finalize_cmd+=(--limit "$FINALIZE_LIMIT")
  elif [[ "$INCREMENTAL_FINALIZE" -eq 1 ]]; then
    finalize_cmd+=(--limit 1)
  fi
  if [[ "$ALLOW_PARTIAL" -eq 1 ]]; then
    finalize_cmd+=(--allow-partial)
  fi
  run "${finalize_cmd[@]}"
}

run_site() {
  result_csvs_file="$(mktemp)"
  collect_results_csvs > "$result_csvs_file"

  current_results_csv="local_state/batches/$BATCH_NAME/$RUN_VERSION/results.csv"
  if [[ -f "$current_results_csv" ]]; then
    run uv run python scripts/validate-run-gates.py --repair-audit-pass --results-csv "$current_results_csv"
  fi

  export_cmd=(uv run python scripts/export-public-evidence.py --clean-output)
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    export_cmd+=(--results-csv "$path")
  done < "$result_csvs_file"
  run "${export_cmd[@]}"

  build_cmd=(uv run python scripts/build-report.py)
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    build_cmd+=("$path")
  done < "$result_csvs_file"
  rm -f "$result_csvs_file"
  build_cmd+=(--output-dir docs --clean-output)
  if [[ "$REFRESH_REPORT" -eq 0 ]]; then
    build_cmd+=(--no-refresh-baselines)
  fi
  run "${build_cmd[@]}"
  run uv run python scripts/check-docs-size.py

  print_cmd uv run python scripts/privacy-scan.py
  if [[ "$DRY_RUN" -eq 0 ]] && ! uv run python scripts/privacy-scan.py; then
    echo "privacy scan found local paths in public files" >&2
    exit 1
  fi
}

run_publish() {
  run git add docs
  if [[ "$DRY_RUN" -eq 1 ]]; then
    print_cmd git commit -m "Update GoalBench report"
    print_cmd git push
  elif git diff --cached --quiet -- docs; then
    echo "no docs changes to publish"
  else
    run git commit -m "Update GoalBench report"
    run git push
  fi
}

TARGET_FILE="$(config_value target_file)"
BATCH_NAME="$(config_value batch_name)"
TARGET_ACCESS="$(config_value target_access)"
TARGET_WRAPPER_COMMAND="$(config_value target_wrapper_command)"
validate_config_policy

if [[ "$WATCH" -eq 1 && "$TARGET_ACCESS" == "wrapper" && "$DRY_RUN" -eq 0 ]]; then
  set +e
  bash -lc "$TARGET_WRAPPER_COMMAND __pb-wrapper-check true" >/tmp/pb-target-wrapper-check.out 2>/tmp/pb-target-wrapper-check.err
  wrapper_status=$?
  set -e
  if [[ "$wrapper_status" -ne 126 ]]; then
    echo "target wrapper is not available through: $TARGET_WRAPPER_COMMAND" >&2
    echo "Install it with: scripts/install-target-wrapper.sh" >&2
    cat /tmp/pb-target-wrapper-check.err >&2
    exit 1
  fi
fi

if [[ "$REFRESH_TARGET_SET" -eq 1 && "$TARGET_FILE" == "target_sets/all_tasks.txt" ]]; then
  run uv run python scripts/write-target-set.py "$PROGRAMBENCH_REPO" --output "$TARGET_FILE"
fi

if [[ -n "$PROGRAMBENCH_REPO" && "$TARGET_FILE" == "target_sets/all_tasks.txt" ]]; then
  run uv run python scripts/validate-target-set.py "$TARGET_FILE" --programbench-repo "$PROGRAMBENCH_REPO"
fi

if [[ -n "$PROGRAMBENCH_REPO" ]]; then
  run env -u VIRTUAL_ENV uv run --project "$PROGRAMBENCH_REPO" python scripts/check-metric-parity.py --programbench-repo "$PROGRAMBENCH_REPO"
fi

if [[ "$REFRESH_REPORT" -eq 1 ]]; then
  run uv run python scripts/refresh-openai-pricing.py
  if [[ "$SITE" -eq 1 || "$PUBLISH" -eq 1 ]]; then
    run uv run python scripts/refresh-programbench-baselines.py
    run uv run python scripts/refresh-programbench-task-baselines.py --target-set "$TARGET_FILE" --merge-existing
  fi
fi

if [[ "$WATCH" -eq 1 && "$INCREMENTAL_FINALIZE" -eq 1 ]]; then
  while true; do
    watch_cmd=(uv run python scripts/run-config.py watch "$CONFIG" --once)
    if [[ -n "$MAX_PARALLEL" ]]; then
      watch_cmd+=(--max-parallel "$MAX_PARALLEL")
    fi
    run "${watch_cmd[@]}"
    if [[ "$FINALIZE" -eq 1 ]]; then
      run_finalize
    fi
    if [[ "$SITE" -eq 1 ]]; then
      run_site
    fi
    if [[ "$PUBLISH" -eq 1 ]]; then
      run_publish
    fi
    if [[ "$ONCE" -eq 1 ]] || batch_complete; then
      break
    fi
    sleep "$(poll_seconds)"
  done
elif [[ "$WATCH" -eq 1 ]]; then
  watch_cmd=(uv run python scripts/run-config.py watch "$CONFIG")
  if [[ -n "$MAX_PARALLEL" ]]; then
    watch_cmd+=(--max-parallel "$MAX_PARALLEL")
  fi
  if [[ "$ONCE" -eq 1 ]]; then
    watch_cmd+=(--once)
  fi
  run "${watch_cmd[@]}"
fi

if [[ "$FINALIZE" -eq 1 && "$INCREMENTAL_FINALIZE" -eq 0 ]]; then
  run_finalize
fi

if [[ "$SITE" -eq 1 && "$INCREMENTAL_FINALIZE" -eq 0 ]]; then
  run_site
fi

if [[ "$PUBLISH" -eq 1 && "$INCREMENTAL_FINALIZE" -eq 0 ]]; then
  run_publish
fi
