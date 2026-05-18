#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import pwd
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATE_ROOT = REPO / "local_state" / "batches"
NO_INTERNET_MODES = {
    "no-internet",
    "mini-swe-compatible-nointernet",
    "paper-prompt-nointernet",
    "no-internet-local-tools",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def state_path_for_results(results_csv: Path) -> Path:
    parts = results_csv.resolve().parts
    marker = ("local_state", "batches")
    for index in range(len(parts) - 3):
        if parts[index : index + 2] == marker:
            return Path(*parts[: index + 4]) / "state.json"
    raise SystemExit(f"cannot infer batch state from results path: {results_csv}")


def rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def codex_session_args(run: dict) -> list[str]:
    user = run.get("codex_user", "")
    if not user:
        return ["--codex-sessions", str(Path.home() / ".codex" / "sessions")]
    home = Path(pwd.getpwnam(user).pw_dir)
    return [
        "--codex-sessions",
        str(home / ".codex" / "sessions"),
        str(home / ".codex" / "archived_sessions"),
    ]


def transcript_shows_goal(instance_dir: Path) -> bool:
    path = instance_dir / "tmux-transcript.log"
    return path.is_file() and "/goal " in path.read_text(errors="replace")


def prompt_starts_goal(instance_dir: Path) -> bool:
    path = instance_dir / "CODEX_INITIAL_PROMPT.md"
    return path.is_file() and path.read_text(errors="replace").lstrip().startswith("/goal ")


def audit_pass_path(instance_dir: Path) -> Path:
    return instance_dir / "goalbench-audit-pass.json"


def repair_audit_pass(instance_dir: Path, run: dict) -> None:
    output = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "audit-run.py"),
            str(instance_dir),
            *codex_session_args(run),
        ],
        cwd=REPO,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    ).stdout
    audit_pass_path(instance_dir).write_text(
        json.dumps(
            {
                "instance_id": run["instance_id"],
                "run_name": run.get("run_name", ""),
                "passed_at": now(),
                "output_tail": output[-4000:],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def gate_failures(row: dict, record: dict, repair_audit: bool) -> list[str]:
    instance_dir = Path(record.get("instance_dir", "")).expanduser()
    run = read_json(instance_dir / "run.json")
    failures = []
    if not prompt_starts_goal(instance_dir):
        failures.append("CODEX_INITIAL_PROMPT.md does not begin with /goal")
    if not transcript_shows_goal(instance_dir):
        failures.append("tmux transcript does not show /goal")
    for key in ("model", "reasoning_effort", "inference_mode", "strict_egress"):
        if key not in run:
            failures.append(f"run.json missing {key}")
    if run.get("inference_mode") in NO_INTERNET_MODES and run.get("strict_egress") is not True:
        failures.append("run.json does not have strict_egress=true")
    if not (instance_dir / "submission.tar.gz").is_file():
        failures.append("missing submission.tar.gz")
    if not (instance_dir / f"{row['instance_id']}.eval.json").is_file():
        failures.append("missing eval result")
    if not audit_pass_path(instance_dir).is_file():
        if repair_audit:
            repair_audit_pass(instance_dir, run)
        else:
            failures.append("missing audit pass marker")
    return failures


def validate_results(results_csv: Path, repair_audit: bool) -> list[str]:
    state_path = state_path_for_results(results_csv)
    state = read_json(state_path)
    state_items = state.get("items", {})
    errors = []
    for row in rows(results_csv):
        record = state_items.get(row["instance_id"])
        if not record:
            errors.append(f"{results_csv}:{row['instance_id']}: missing state record")
            continue
        if row.get("run_name") and record.get("run_name") and row["run_name"] != record["run_name"]:
            errors.append(f"{results_csv}:{row['instance_id']}: run_name differs from state")
            continue
        for failure in gate_failures(row, record, repair_audit):
            errors.append(f"{results_csv}:{row['instance_id']}: {failure}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate hard gates before scoring/publishing GoalBench rows")
    parser.add_argument("--results-csv", action="append", required=True)
    parser.add_argument("--repair-audit-pass", action="store_true")
    args = parser.parse_args()

    errors = [
        error
        for path in args.results_csv
        for error in validate_results(Path(path).expanduser(), args.repair_audit_pass)
    ]
    if errors:
        print("\n".join(errors))
        raise SystemExit(1)
    print("OK run gates passed")


if __name__ == "__main__":
    main()
