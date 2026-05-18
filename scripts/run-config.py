#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
from itertools import chain
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
RUN_BATCH = REPO / "scripts" / "run-batch.py"
NO_INTERNET_MODES = {
    "no-internet",
    "mini-swe-compatible-nointernet",
    "paper-prompt-nointernet",
    "paper-prompt-goal-contract-nointernet",
    "no-internet-local-tools",
}


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def validate_config(config: dict[str, Any], action: str, dry_run: bool) -> None:
    if config.get("inference_mode") in NO_INTERNET_MODES and not config.get("strict_egress"):
        raise SystemExit(f"{config['inference_mode']} configs must set strict_egress=true")
    if dry_run or action != "watch" or not config.get("strict_egress"):
        return
    if platform.system() != "Linux":
        raise SystemExit("strict egress is only implemented for Linux hosts")
    if os.geteuid() == 0 and not config.get("codex_user"):
        raise SystemExit("strict egress under root requires codex_user so only the Codex UID is firewalled")
    if config.get("codex_user") == "root":
        raise SystemExit("strict egress must run Codex as a dedicated non-root user")


def option_args(name: str, value: Any) -> list[str]:
    return [] if value == "" or value is None else [f"--{name.replace('_', '-')}", str(value)]


def flag_args(name: str, value: bool) -> list[str]:
    return [f"--{name.replace('_', '-')}"] if value else []


def run_version(config: dict[str, Any]) -> str:
    return os.environ.get("RUN_VERSION") or str(config.get("run_version") or "")


def programbench_repo(config: dict[str, Any], args: argparse.Namespace) -> str:
    for candidate in (args.programbench_repo, config.get("programbench_repo"), os.environ.get("PROGRAMBENCH_REPO")):
        if candidate:
            return str(candidate)
    sibling = REPO.parent / "ProgramBench"
    return str(sibling) if (sibling / "src" / "programbench").is_dir() else ""


def common_watch_args(config: dict[str, Any], args: argparse.Namespace) -> list[str]:
    names = (
        "run_root",
        "poll_seconds",
        "docker_cpus",
        "docker_memory",
        "inference_mode",
        "target_access",
        "target_wrapper_command",
        "model",
        "reasoning_effort",
        "codex_user",
        "run_name_prefix",
        "prompt_template",
    )
    return [
        sys.executable,
        str(RUN_BATCH),
        "watch",
        config["target_file"],
        "--batch-name",
        config["batch_name"],
        *option_args("run_version", run_version(config)),
        *option_args(
            "max_parallel",
            args.max_parallel if args.max_parallel is not None else config.get("max_parallel"),
        ),
        *chain.from_iterable(option_args(name, config.get(name)) for name in names),
        *flag_args("strict_egress", bool(config.get("strict_egress"))),
    ]


def command(config: dict[str, Any], args: argparse.Namespace) -> list[str]:
    if args.action == "watch":
        return [*common_watch_args(config, args), *flag_args("once", args.once)]
    if args.action == "status":
        return [
            sys.executable,
            str(RUN_BATCH),
            "status",
            "--batch-name",
            config["batch_name"],
            *option_args("run_version", run_version(config)),
        ]
    if args.action == "retry":
        return [
            sys.executable,
            str(RUN_BATCH),
            "retry",
            "--batch-name",
            config["batch_name"],
            *option_args("run_version", run_version(config)),
            *chain.from_iterable(option_args("instance", instance) for instance in args.instance or []),
            *flag_args("failed", args.failed),
            *flag_args("rerun_finalize_failed", args.retry_finalize_failed),
            *option_args("max_attempts", args.max_attempts),
        ]
    return [
        sys.executable,
        str(RUN_BATCH),
        "finalize",
        "--batch-name",
        config["batch_name"],
        *option_args("run_version", run_version(config)),
        *option_args("programbench_repo", programbench_repo(config, args)),
        *option_args("eval_timeout_seconds", config.get("eval_timeout_seconds")),
        *option_args("limit", args.limit),
        *chain.from_iterable(option_args("instance", instance) for instance in args.instance or []),
        *flag_args("allow_partial", args.allow_partial),
        *flag_args("retry_finalize_failed", args.retry_finalize_failed),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a named ProgramBench /goal batch config")
    parser.add_argument("action", choices=["watch", "status", "finalize", "retry"])
    parser.add_argument("config")
    parser.add_argument("--once", action="store_true", help="only applies to watch")
    parser.add_argument("--max-parallel", type=int, default=None, help="override config max_parallel for watch")
    parser.add_argument("--programbench-repo", default="", help="only applies to finalize")
    parser.add_argument("--allow-partial", action="store_true", help="only applies to finalize")
    parser.add_argument(
        "--retry-finalize-failed",
        action="store_true",
        help="disabled; kept only for CLI compatibility",
    )
    parser.add_argument("--failed", action="store_true", help="retry only session_failed_before_goal_done rows")
    parser.add_argument("--max-attempts", type=int, default=2, help="only applies to retry")
    parser.add_argument("--limit", type=int, default=0, help="only applies to finalize")
    parser.add_argument("--instance", action="append", help="only applies to finalize")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    validate_config(config, args.action, args.dry_run)
    cmd = command(config, args)
    if args.dry_run:
        print(shlex.join(cmd))
        return
    subprocess.run(cmd, cwd=REPO, check=True)


if __name__ == "__main__":
    main()
