#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import pwd
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_ROOT = Path.home() / "pb-goal-runs"
DEFAULT_STATE_ROOT = REPO / "local_state" / "batches"
DONE_MARKERS = ("Goal achieved", "Goal marked complete")
RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "429")
FINALIZE_READY = {"goal_done", "packaged"}
TERMINAL_STATUSES = {"goal_done", "packaged", "evaluated", "failed", "finalize_failed"}
CLEANUP_STATUSES = TERMINAL_STATUSES
SESSION_FAILED_BEFORE_GOAL_DONE = "session_failed_before_goal_done"
GATE_FAILED = "gate_failed"
NO_INTERNET_MODES = {
    "mini-swe-compatible-nointernet",
    "paper-prompt-nointernet",
    "paper-prompt-goal-contract-nointernet",
}
STATUS_RANK = {
    "pending": 0,
    "running": 1,
    "failed": 2,
    "goal_done": 3,
    "finalize_failed": 4,
    "packaged": 5,
    "evaluated": 6,
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_targets(path: Path) -> list[str]:
    return [line.split("#", 1)[0].strip() for line in path.read_text().splitlines() if line.split("#", 1)[0].strip()]


def latest_path(batch_name: str) -> Path:
    return DEFAULT_STATE_ROOT / f"{batch_name}.latest"


def state_path(batch_name: str, run_version: str = "") -> Path:
    return (
        DEFAULT_STATE_ROOT / batch_name / run_version / "state.json"
        if run_version
        else DEFAULT_STATE_ROOT / f"{batch_name}.json"
    )


def resolved_run_version(batch_name: str, run_version: str = "") -> str:
    if run_version or not latest_path(batch_name).is_file():
        return run_version
    return latest_path(batch_name).read_text().strip()


def load_state(batch_name: str, run_version: str = "") -> dict:
    version = resolved_run_version(batch_name, run_version)
    path = state_path(batch_name, version)
    return (
        json.loads(path.read_text())
        if path.is_file()
        else {
            "batch_name": batch_name,
            "run_version": version,
            "created_at": now(),
            "updated_at": now(),
            "items": {},
        }
    )


def record_updated_at(record: dict) -> str:
    return max(
        str(record.get(key, ""))
        for key in (
            "evaluated_at",
            "packaged_at",
            "finalize_failed_at",
            "completed_at",
            "failed_at",
            "started_at",
            "retried_at",
        )
    )


def save_state(state: dict, merge_disk: bool = True) -> None:
    path = state_path(state["batch_name"], state.get("run_version", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    if merge_disk and path.is_file():
        disk = json.loads(path.read_text())
        for instance_id, record in disk["items"].items():
            current = state["items"].get(instance_id, {})
            disk_rank = STATUS_RANK.get(record["status"], 0)
            current_rank = STATUS_RANK.get(current.get("status", ""), 0)
            if disk_rank > current_rank or (
                disk_rank == current_rank and record_updated_at(record) > record_updated_at(current)
            ):
                state["items"][instance_id] = record
    state["updated_at"] = now()
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    if state.get("run_version"):
        latest_path(state["batch_name"]).write_text(state["run_version"] + "\n")


def run(cmd: list[str], cwd: Path = REPO, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check)


def run_with_timeout(cmd: list[str], timeout: int, cwd: Path = REPO) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=output) from e
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, cmd, output=output)
    return subprocess.CompletedProcess(cmd, process.returncode, output)


def tmux_cmd(user: str, *args: str) -> list[str]:
    return ["sudo", "-H", "-u", user, "tmux", *args] if user else ["tmux", *args]


def tmux_has_session(session: str, user: str = "") -> bool:
    return (
        subprocess.run(
            tmux_cmd(user, "has-session", "-t", session), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode
        == 0
    )


def tmux_capture(session: str, user: str = "") -> str:
    if not tmux_has_session(session, user):
        return ""
    return run(tmux_cmd(user, "capture-pane", "-pt", session, "-S", "-220"), check=False).stdout


def transcript_tail(record: dict) -> str:
    path = Path(record.get("instance_dir", "")) / "tmux-transcript.log"
    return path.read_text(errors="replace")[-12000:] if path.is_file() else ""


def record_output(record: dict) -> str:
    return tmux_capture(record["session_name"], record.get("codex_user", "")) + "\n" + transcript_tail(record)


def add_error(record: dict, error: str) -> dict:
    return {
        **record,
        "last_error": error[-4000:],
        "last_error_history": [*record.get("last_error_history", []), {"at": now(), "error": error[-4000:]}],
    }


def instance_dir(record: dict) -> Path:
    return Path(record.get("instance_dir", "")).expanduser()


def prompt_path(record: dict) -> Path:
    return instance_dir(record) / "CODEX_INITIAL_PROMPT.md"


def transcript_path(record: dict) -> Path:
    return instance_dir(record) / "tmux-transcript.log"


def run_json_path(record: dict) -> Path:
    return instance_dir(record) / "run.json"


def submission_path(record: dict) -> Path:
    return instance_dir(record) / "submission.tar.gz"


def eval_json_path(record: dict) -> Path:
    return instance_dir(record) / f"{record['instance_id']}.eval.json"


def audit_pass_path(record: dict) -> Path:
    return instance_dir(record) / "goalbench-audit-pass.json"


def prompt_starts_goal(record: dict) -> bool:
    path = prompt_path(record)
    return path.is_file() and path.read_text(errors="replace").lstrip().startswith("/goal ")


def transcript_shows_goal(record: dict) -> bool:
    path = transcript_path(record)
    return path.is_file() and "/goal" in path.read_text(errors="replace")


def run_json(record: dict) -> dict:
    path = run_json_path(record)
    return json.loads(path.read_text()) if path.is_file() else {}


def run_metadata_failures(record: dict) -> list[str]:
    run = run_json(record)
    missing = [key for key in ("model", "reasoning_effort", "inference_mode", "strict_egress") if key not in run]
    if run.get("inference_mode") in NO_INTERNET_MODES and run.get("strict_egress") is not True:
        missing.append("strict_egress=true")
    return missing


def base_gate_failures(record: dict) -> list[str]:
    failures = []
    if not prompt_starts_goal(record):
        failures.append("CODEX_INITIAL_PROMPT.md does not begin with /goal")
    if not transcript_shows_goal(record):
        failures.append("tmux transcript does not show /goal")
    failures.extend(f"run.json missing/invalid {key}" for key in run_metadata_failures(record))
    return failures


def scored_gate_failures(record: dict) -> list[str]:
    failures = base_gate_failures(record)
    if not submission_path(record).is_file():
        failures.append("missing submission.tar.gz")
    if not (record.get("audit_passed_at") or audit_pass_path(record).is_file()):
        failures.append("missing audit pass marker")
    if not eval_json_path(record).is_file():
        failures.append("missing eval result")
    return failures


def classify_session_failure(record: dict) -> str:
    if "tmux session ended before goal_done" in record.get("last_error", ""):
        return SESSION_FAILED_BEFORE_GOAL_DONE
    if record.get("session_name") and not tmux_has_session(record["session_name"], record.get("codex_user", "")):
        return SESSION_FAILED_BEFORE_GOAL_DONE
    return record.get("failure_class", "")


def retryable_failure_class(record: dict) -> str:
    return record.get("failure_class") or classify_session_failure(record)


def write_audit_pass(record: dict, output: str) -> dict:
    path = audit_pass_path(record)
    path.write_text(
        json.dumps(
            {
                "instance_id": record["instance_id"],
                "run_name": record.get("run_name", ""),
                "passed_at": now(),
                "output_tail": output[-4000:],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return {**record, "audit_passed_at": now()}


def prepare_instance(args: argparse.Namespace, instance_id: str, run_root: Path, attempt: int) -> dict:
    cmd = [
        sys.executable,
        str(REPO / "programbench_goal_runner.py"),
        "prepare",
        instance_id,
        "--run-root",
        str(run_root),
        "--docker-cpus",
        str(args.docker_cpus),
        "--docker-memory",
        args.docker_memory,
        "--inference-mode",
        args.inference_mode,
        "--target-access",
        args.target_access,
        "--target-wrapper-command",
        args.target_wrapper_command,
        "--model",
        args.model,
        "--reasoning-effort",
        args.reasoning_effort,
        "--run-version",
        args.run_version,
    ]
    if args.strict_egress:
        cmd.append("--strict-egress")
    if args.codex_user:
        cmd.extend(["--codex-user", args.codex_user])
    if args.run_name_prefix:
        version = f"{args.run_version}-" if args.run_version else ""
        retry = f"-attempt-{attempt}" if attempt > 1 else ""
        run_name = f"{args.run_name_prefix}-{version}{instance_id.replace('__', '-').split('.', 1)[0]}{retry}"
        cmd.extend(["--run-name", run_name])
    if args.prompt_template:
        cmd.extend(["--prompt-template", args.prompt_template])
    output = run(cmd).stdout.splitlines()
    instance_dir = Path(next(line for line in output if line.startswith("/"))).resolve()
    run_json = json.loads((instance_dir / "run.json").read_text())
    return {
        "instance_id": instance_id,
        "status": "prepared",
        "instance_dir": str(instance_dir),
        "run_name": run_json["run_name"],
        "run_version": run_json.get("run_version", ""),
        "session_name": run_json["session_name"],
        "container_name": run_json["container_name"],
        "inference_mode": run_json["inference_mode"],
        "codex_user": run_json.get("codex_user", ""),
        "prepared_at": now(),
        "attempts": 0,
        "last_error": "",
    }


def start_instance(record: dict) -> dict:
    instance_dir = Path(record["instance_dir"])
    run([str(instance_dir / "start-target.sh")])
    run([str(instance_dir / "check-compliance.sh")])
    run([str(instance_dir / "start-codex-goal.sh")])
    return {**record, "status": "running", "started_at": now(), "last_error": ""}


def refresh_record(record: dict) -> dict:
    if record["status"] != "running":
        return record
    output = record_output(record)
    if any(marker in output for marker in DONE_MARKERS):
        cleanup_target_container(record)
        cleanup_codex_session(record)
        return {**record, "status": "goal_done", "goal_done_at": now(), "last_pane_tail": output[-4000:]}
    if output and any(marker in output.lower() for marker in RATE_LIMIT_MARKERS):
        return {**record, "last_rate_limit_seen_at": now(), "last_pane_tail": output[-4000:]}
    if not tmux_has_session(record["session_name"], record.get("codex_user", "")):
        return add_error(
            {
                **record,
                "status": "failed",
                "failed_at": now(),
                "failure_class": SESSION_FAILED_BEFORE_GOAL_DONE,
            },
            "tmux session ended before goal_done",
        )
    return {**record, "last_rate_limit_seen_at": "", "last_pane_tail": output[-4000:]}


def update_targets(state: dict, targets: list[str]) -> None:
    state["items"] = {
        **{instance_id: {"instance_id": instance_id, "status": "pending", "last_error": ""} for instance_id in targets},
        **state["items"],
    }


def running_count(state: dict) -> int:
    return sum(record["status"] == "running" for record in state["items"].values())


def active_rate_limit(state: dict, cooldown_seconds: int) -> bool:
    current = datetime.now(timezone.utc)
    return any(
        record.get("last_rate_limit_seen_at")
        and record["status"] == "running"
        and (current - datetime.fromisoformat(record["last_rate_limit_seen_at"])).total_seconds() < cooldown_seconds
        for record in state["items"].values()
    )


def launch_ready(args: argparse.Namespace, state: dict, run_root: Path) -> None:
    if active_rate_limit(state, args.rate_limit_cooldown_seconds):
        return
    for instance_id, record in state["items"].items():
        if running_count(state) >= args.max_parallel:
            return
        if record["status"] != "pending":
            continue
        try:
            next_attempt = record.get("attempts", 0) + 1
            prepared = prepare_instance(args, instance_id, run_root, next_attempt)
            state["items"][instance_id] = start_instance(
                {
                    **prepared,
                    "attempts": next_attempt,
                    "attempt_history": record.get("attempt_history", []),
                    "retry_class": record.get("failure_class", ""),
                }
            )
        except subprocess.CalledProcessError as e:
            state["items"][instance_id] = add_error(
                {**record, "status": "failed", "failed_at": now(), "attempts": record.get("attempts", 0) + 1},
                e.stdout,
            )
        save_state(state)


def refresh_state(state: dict) -> None:
    state["items"] = {instance_id: refresh_record(record) for instance_id, record in state["items"].items()}


def cleanup_target_container(record: dict) -> None:
    if record.get("container_name"):
        run(["docker", "rm", "-f", record["container_name"]], check=False)


def cleanup_codex_session(record: dict) -> None:
    if record.get("session_name"):
        run(tmux_cmd(record.get("codex_user", ""), "kill-session", "-t", record["session_name"]), check=False)


def docker_container_ids(name: str) -> set[str]:
    return set(run(["docker", "ps", "-aq", "--filter", f"name={name}"], check=False).stdout.splitlines())


def cleanup_new_eval_containers(before: set[str]) -> None:
    for container in sorted(docker_container_ids("programbench-") - before):
        run(["docker", "rm", "-f", container], check=False)


def cleanup_finished(state: dict) -> None:
    for record in state["items"].values():
        if record["status"] in CLEANUP_STATUSES:
            cleanup_target_container(record)
            cleanup_codex_session(record)


def codex_session_roots(*records: dict) -> list[Path]:
    roots = []
    for user in sorted({record.get("codex_user", "") for record in records if record.get("codex_user")}):
        home = Path(pwd.getpwnam(user).pw_dir)
        roots.extend([home / ".codex" / "sessions", home / ".codex" / "archived_sessions"])
    return roots


def codex_session_args(*records: dict) -> list[str]:
    roots = codex_session_roots(*records)
    return ["--codex-sessions", *[str(path) for path in roots]] if roots else []


def summarize_state(state: dict) -> dict[str, int]:
    return dict(Counter(record["status"] for record in state["items"].values()))


def status_line(record: dict) -> str:
    return ",".join(
        [
            record["instance_id"],
            record["status"],
            record.get("run_name", ""),
            record.get("session_name", ""),
            f"attempts={record.get('attempts', 0)}",
            f"failure_class={record.get('failure_class', '')}",
            record.get("last_error", "").replace("\n", "\\n")[:240],
        ]
    )


def print_status(state: dict) -> None:
    counts = summarize_state(state)
    print(",".join(f"{key}={counts[key]}" for key in sorted(counts)))
    print("\n".join(status_line(record) for record in state["items"].values()))


def watch(args: argparse.Namespace) -> None:
    state = load_state(args.batch_name, args.run_version)
    update_targets(state, read_targets(Path(args.target_file).expanduser()))
    default_runs_root = (
        Path(pwd.getpwnam(args.codex_user).pw_dir) / "pb-goal-runs" if args.codex_user else DEFAULT_RUNS_ROOT
    )
    run_root = (
        Path(args.run_root).expanduser()
        if args.run_root
        else default_runs_root / args.batch_name / args.run_version
        if args.run_version
        else default_runs_root / args.batch_name
    )
    state["run_root"] = str(run_root)
    state["run_version"] = args.run_version
    while True:
        refresh_state(state)
        cleanup_finished(state)
        reconcile_results(state)
        launch_ready(args, state, run_root)
        save_state(state)
        print_status(state)
        if args.once:
            return
        if all(record["status"] in TERMINAL_STATUSES for record in state["items"].values()):
            return
        time.sleep(args.poll_seconds)


def status(args: argparse.Namespace) -> None:
    state = load_state(args.batch_name, args.run_version)
    refresh_state(state)
    cleanup_finished(state)
    reconcile_results(state)
    save_state(state)
    print_status(state)


def finalize_one(args: argparse.Namespace, record: dict) -> dict:
    instance_dir = Path(record["instance_dir"])
    eval_containers = docker_container_ids("programbench-")
    try:
        if failures := base_gate_failures(record):
            return add_error(
                {**record, "status": "finalize_failed", "finalize_failed_at": now(), "failure_class": GATE_FAILED},
                "hard gate failed before package/eval:\n" + "\n".join(failures),
            )
        run([str(instance_dir / "package-submission.sh")])
        if not submission_path(record).is_file():
            return add_error(
                {**record, "status": "finalize_failed", "finalize_failed_at": now(), "failure_class": GATE_FAILED},
                "hard gate failed after package: missing submission.tar.gz",
            )
        audit_cmd = [sys.executable, str(REPO / "scripts" / "audit-run.py")]
        audit_cmd.append(str(instance_dir))
        audit_cmd.extend(codex_session_args(record))
        record = write_audit_pass(record, run(audit_cmd).stdout)
        if args.programbench_repo:
            eval_cmd = [str(instance_dir / "eval-submission.sh"), str(Path(args.programbench_repo).expanduser())]
            run_with_timeout(eval_cmd, args.eval_timeout_seconds) if args.eval_timeout_seconds else run(eval_cmd)
            if not eval_json_path(record).is_file():
                return add_error(
                    {**record, "status": "finalize_failed", "finalize_failed_at": now(), "failure_class": GATE_FAILED},
                    "hard gate failed after eval: missing eval result",
                )
            updated = {**record, "status": "evaluated", "evaluated_at": now(), "last_error": ""}
            updated.pop("failure_class", None)
            return updated
        updated = {**record, "status": "packaged", "packaged_at": now(), "last_error": ""}
        updated.pop("failure_class", None)
        return updated
    except subprocess.TimeoutExpired as e:
        return add_error(
            {**record, "status": "finalize_failed", "finalize_failed_at": now()},
            f"{e.output or ''}\ncommand timed out after {e.timeout}s",
        )
    except subprocess.CalledProcessError as e:
        return add_error({**record, "status": "finalize_failed", "finalize_failed_at": now()}, e.stdout)
    finally:
        cleanup_target_container(record)
        cleanup_codex_session(record)
        if args.programbench_repo:
            cleanup_new_eval_containers(eval_containers)


def results_output_path(state: dict) -> Path:
    return (
        DEFAULT_STATE_ROOT / state["batch_name"] / state["run_version"] / "results.csv"
        if state.get("run_version")
        else DEFAULT_STATE_ROOT / state["batch_name"] / "results.csv"
    )


def evaluated_result_ids(state: dict) -> set[str]:
    output = results_output_path(state)
    if not output.is_file():
        return set()
    with output.open(newline="") as f:
        return {row["instance_id"] for row in csv.DictReader(f) if row.get("instance_id")}


def mark_evaluated_from_results(record: dict) -> dict:
    updated = {**record, "status": "evaluated", "evaluated_at": record.get("evaluated_at") or now(), "last_error": ""}
    updated.pop("failed_at", None)
    updated.pop("finalize_failed_at", None)
    updated.pop("failure_class", None)
    return updated


def reconcile_results(state: dict) -> None:
    for instance_id in evaluated_result_ids(state):
        record = state["items"].get(instance_id)
        if record and record["status"] != "evaluated":
            state["items"][instance_id] = mark_evaluated_from_results(record)


def summarize_and_collect(args: argparse.Namespace, state: dict, records: list[dict] | None = None) -> None:
    if not args.programbench_repo:
        return
    run_root = Path(state["run_root"])
    output = results_output_path(state)
    output.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "uv",
            "run",
            "--project",
            str(Path(args.programbench_repo).expanduser()),
            "python",
            str(REPO / "scripts" / "summarize-results.py"),
            str(run_root),
            "--programbench-repo",
            str(Path(args.programbench_repo).expanduser()),
            *codex_session_args(*state["items"].values()),
            "--output",
            str(output),
        ]
    )
    for record in records or list(state["items"].values()):
        if record["status"] == "evaluated":
            run(
                [
                    sys.executable,
                    str(REPO / "scripts" / "collect-run-artifacts.py"),
                    record["instance_dir"],
                    "--results-csv",
                    str(output),
                ]
            )
    print(output)


def finalize(args: argparse.Namespace) -> None:
    state = load_state(args.batch_name, args.run_version)
    refresh_state(state)
    cleanup_finished(state)
    reconcile_results(state)
    finalized = 0
    wanted = set(args.instance or [])
    for instance_id, record in list(state["items"].items()):
        if wanted and instance_id not in wanted:
            continue
        if record["status"] in FINALIZE_READY or (args.retry_finalize_failed and record["status"] == "finalize_failed"):
            state["items"][instance_id] = finalize_one(args, record)
            save_state(state)
            summarize_and_collect(args, state, [state["items"][instance_id]])
            finalized += 1
            if args.limit and finalized >= args.limit:
                break
    summarize_and_collect(args, state)
    if not args.allow_partial:
        incomplete = [record["instance_id"] for record in state["items"].values() if record["status"] != "evaluated"]
        if incomplete:
            raise SystemExit(
                f"batch is incomplete ({len(incomplete)} not evaluated); pass --allow-partial to publish a partial run"
            )
    save_state(state)
    print_status(state)


def retry(args: argparse.Namespace) -> None:
    state = load_state(args.batch_name, args.run_version)
    wanted = set(args.instance or [])
    state["items"] = {
        instance_id: retry_record(record, args.failed, args.rerun_finalize_failed, args.max_attempts)
        if not wanted or instance_id in wanted
        else record
        for instance_id, record in state["items"].items()
    }
    save_state(state, merge_disk=False)
    print_status(state)


def retry_record(record: dict, failed: bool, rerun_finalize_failed: bool, max_attempts: int) -> dict:
    if rerun_finalize_failed and record["status"] == "finalize_failed":
        return add_error(record, "finalize_failed retries are disabled by benchmark policy")
    failure_class = retryable_failure_class(record)
    if not failed or record["status"] != "failed" or failure_class != SESSION_FAILED_BEFORE_GOAL_DONE:
        return record
    if record.get("attempts", 0) >= max_attempts:
        return add_error(record, f"retry skipped: attempts {record.get('attempts', 0)} >= max_attempts {max_attempts}")
    return {
        **record,
        "status": "pending",
        "failure_class": failure_class,
        "last_error": "",
        "retried_at": now(),
        "attempt_history": [
            *record.get("attempt_history", []),
            {
                "attempt": record.get("attempts", 0),
                "status": record["status"],
                "instance_dir": record.get("instance_dir", ""),
                "run_name": record.get("run_name", ""),
                "session_name": record.get("session_name", ""),
                "failure_class": failure_class,
                "last_error": record.get("last_error", "")[-1000:],
                "ended_at": record.get("failed_at", ""),
            },
        ],
    }


def add_common_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--batch-name", required=True)
    parser.add_argument("--run-version", default="")
    parser.add_argument("--run-root", default="")
    parser.add_argument("--max-parallel", type=int, default=1)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--rate-limit-cooldown-seconds", type=int, default=600)
    parser.add_argument("--docker-cpus", type=int, default=20)
    parser.add_argument("--docker-memory", default="60g")
    parser.add_argument(
        "--inference-mode",
        choices=[
            "mini-swe-compatible-nointernet",
            "paper-prompt-nointernet",
            "paper-prompt-goal-contract-nointernet",
        ],
        default="paper-prompt-nointernet",
    )
    parser.add_argument("--target-access", choices=["direct-docker", "wrapper"], default="direct-docker")
    parser.add_argument("--target-wrapper-command", default="sudo -n /usr/local/bin/pb-target-exec")
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--reasoning-effort", default="xhigh")
    parser.add_argument("--strict-egress", action="store_true")
    parser.add_argument("--codex-user", default="")
    parser.add_argument("--run-name-prefix", default="")
    parser.add_argument("--prompt-template", default="")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run ProgramBench /goal batches with local resumable state")
    subparsers = parser.add_subparsers(required=True)

    watch_parser = subparsers.add_parser("watch")
    watch_parser.add_argument("target_file")
    watch_parser.add_argument("--once", action="store_true")
    add_common_run_args(watch_parser)
    watch_parser.set_defaults(func=watch)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--batch-name", required=True)
    status_parser.add_argument("--run-version", default="")
    status_parser.set_defaults(func=status)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--batch-name", required=True)
    finalize_parser.add_argument("--run-version", default="")
    finalize_parser.add_argument("--programbench-repo", default="")
    finalize_parser.add_argument("--allow-partial", action="store_true")
    finalize_parser.add_argument("--eval-timeout-seconds", type=int, default=0)
    finalize_parser.add_argument("--limit", type=int, default=0)
    finalize_parser.add_argument("--retry-finalize-failed", action="store_true")
    finalize_parser.add_argument("--instance", action="append")
    finalize_parser.set_defaults(func=finalize)

    retry_parser = subparsers.add_parser("retry")
    retry_parser.add_argument("--batch-name", required=True)
    retry_parser.add_argument("--run-version", default="")
    retry_parser.add_argument("--failed", action="store_true")
    retry_parser.add_argument(
        "--rerun-finalize-failed",
        action="store_true",
        help="disabled; kept only for CLI compatibility",
    )
    retry_parser.add_argument("--max-attempts", type=int, default=2)
    retry_parser.add_argument("--instance", action="append")
    retry_parser.set_defaults(func=retry)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
