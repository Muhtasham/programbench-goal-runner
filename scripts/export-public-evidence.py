#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from collections import Counter
from pathlib import Path

LOCAL_ARTIFACTS = Path("local_state/run_artifacts")
DEFAULT_OUTPUT = Path("docs/evidence")
MAX_TEXT_CHARS = 2000
MAX_FAILED_TESTS = 80
MAX_PUBLIC_TEST_RESULTS = 120
MAX_LOG_STEPS = 120
MAX_PACKAGE_FILES = 250
SECRET_PATTERNS = (
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}\b"), "[redacted-stripe-key]"),
    (re.compile(r"\bwhsec_[A-Za-z0-9]{8,}\b"), "[redacted-stripe-webhook-secret]"),
)


def redact_secrets(value: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def redact_json(value):
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [redact_json(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_json(item) for key, item in value.items()}
    return value


def truncate(value: str, limit: int = MAX_TEXT_CHARS) -> str:
    value = redact_secrets(value)
    return value if len(value) <= limit else value[:limit] + f"\n[truncated {len(value) - limit} chars]"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def scrub_metrics(metrics: dict) -> dict:
    return {key: value for key, value in metrics.items() if key != "session_logs"}


def status_counts(eval_json: dict) -> dict[str, int]:
    return dict(Counter(result["status"] for result in eval_json.get("test_results", [])))


def failed_tests(eval_json: dict) -> list[dict]:
    return [
        {
            "name": result["name"],
            "branch": result.get("branch", ""),
            "status": result["status"],
            "message_redacted": "message" in result.get("extra", {}),
            "message_chars": len(str(result.get("extra", {}).get("message", ""))),
        }
        for result in eval_json.get("test_results", [])
        if result["status"] != "passed"
    ]


def eval_summary(eval_json: dict) -> dict:
    results = eval_json.get("test_results", [])
    failures = failed_tests(eval_json)
    return {
        "test_records": len(results),
        "status_counts": status_counts(eval_json),
        "failed_tests": failures[:MAX_FAILED_TESTS],
        "failed_tests_omitted": max(0, len(failures) - MAX_FAILED_TESTS),
        "error_code": eval_json.get("error_code"),
        "error_details_redacted": bool(eval_json.get("error_details")),
        "error_details_chars": len(str(eval_json.get("error_details") or "")),
        "test_branches": eval_json.get("test_branches", []),
        "test_branch_errors": redact_json(eval_json.get("test_branch_errors", {})),
        "executable_hash": eval_json.get("executable_hash"),
        "warnings": redact_json(eval_json.get("warnings", [])),
        "evaluator_log_steps": [
            {
                "step": entry.get("step", ""),
                "branch": entry.get("branch", ""),
                "returncode": entry.get("returncode"),
                "wall_time": entry.get("wall_time"),
                "exception_info_redacted": bool(entry.get("exception_info")),
                "exception_info_chars": len(str(entry.get("exception_info", ""))),
            }
            for entry in eval_json.get("log", [])[:MAX_LOG_STEPS]
        ],
        "evaluator_log_steps_omitted": max(0, len(eval_json.get("log", [])) - MAX_LOG_STEPS),
    }


def public_eval(eval_json: dict) -> dict:
    results = eval_json.get("test_results", [])
    non_passed = [result for result in results if result["status"] != "passed"]
    public_results = non_passed[:MAX_PUBLIC_TEST_RESULTS]
    return {
        "test_results": [
            {
                "name": result["name"],
                "branch": result.get("branch", ""),
                "status": result["status"],
                "extra": public_extra(result.get("extra", {})),
            }
            for result in public_results
        ],
        "test_records": len(results),
        "status_counts": status_counts(eval_json),
        "test_results_policy": "non-passed records only, capped for public artifact size",
        "test_results_omitted": max(0, len(non_passed) - len(public_results)),
        "error_code": eval_json.get("error_code"),
        "error_details_redacted": bool(eval_json.get("error_details")),
        "error_details_chars": len(str(eval_json.get("error_details") or "")),
        "log": [public_log_entry(entry) for entry in eval_json.get("log", [])[:MAX_LOG_STEPS]],
        "log_entries_omitted": max(0, len(eval_json.get("log", [])) - MAX_LOG_STEPS),
        "solution_branch": eval_json.get("solution_branch"),
        "test_branches": eval_json.get("test_branches", []),
        "test_branch_errors": redact_json(eval_json.get("test_branch_errors", {})),
        "executable_hash": eval_json.get("executable_hash"),
        "warnings": redact_json(eval_json.get("warnings", [])),
        "public_redactions": {
            "extra_text": "redacted",
            "log_output": "redacted",
            "long_extra_text": f"truncated to {MAX_TEXT_CHARS} chars",
        },
    }


def public_usage_audit(usage_audit: dict, instance_id: str) -> dict:
    return {
        "generated_at": usage_audit.get("generated_at", ""),
        "pricing_snapshot": usage_audit.get("pricing_snapshot", {}),
        "row": next((row for row in usage_audit.get("rows", []) if row.get("instance_id") == instance_id), {}),
        "totals": usage_audit.get("totals", {}),
        "warnings_for_instance": redact_json(
            [warning for warning in usage_audit.get("warnings", []) if warning.startswith(f"{instance_id}:")]
        ),
        "notes": redact_json(usage_audit.get("notes", [])),
    }


def public_extra(extra: dict) -> dict:
    allowed = {key: extra[key] for key in ("time",) if key in extra}
    if "message" in extra:
        allowed["message_redacted"] = True
        allowed["message_chars"] = len(str(extra["message"]))
    return allowed


def public_log_entry(entry: dict) -> dict:
    return {
        "step": entry.get("step", ""),
        "branch": entry.get("branch", ""),
        "command": entry.get("command", ""),
        "wall_time": entry.get("wall_time"),
        "returncode": entry.get("returncode"),
        "exception_info_redacted": bool(entry.get("exception_info")),
        "exception_info_chars": len(str(entry.get("exception_info", ""))),
        "output_redacted": "output" in entry,
        "output_original_chars": len(str(entry.get("output", ""))) if "output" in entry else 0,
    }


def public_manifest(manifest: dict, eval_summary_path: str, eval_json_path: str) -> dict:
    run_name = manifest.get("metrics", {}).get("run_name") or manifest["run"]["run_name"]
    paper_compliant = (
        manifest["run"]["inference_mode"] in {"paper", "paper-prompt-nointernet"}
        and manifest["run"].get("target_access") == "wrapper"
        and manifest["run"]["host_system"] == "Linux"
        and manifest["run"]["host_machine"] in {"x86_64", "AMD64"}
        and str(manifest["run"]["docker_cpus"]) == "20"
        and manifest["run"]["docker_memory"] == "60g"
    )
    return {
        "collected_at": manifest["collected_at"],
        "instance_id": manifest["run"]["instance_id"],
        "run_name": run_name,
        "run_version": manifest["run"].get("run_version", ""),
        "model": manifest["run"]["model"],
        "reasoning_effort": manifest["run"]["reasoning_effort"],
        "codex_version": manifest["run"].get("codex_version", ""),
        "inference_mode": manifest["run"]["inference_mode"],
        "paper_mode": manifest["run"]["inference_mode"] in {"paper", "paper-prompt-nointernet"},
        "paper_compliant": paper_compliant,
        "host_system": manifest["run"]["host_system"],
        "host_machine": manifest["run"]["host_machine"],
        "docker_cpus": manifest["run"]["docker_cpus"],
        "docker_memory": manifest["run"]["docker_memory"],
        "metrics": scrub_metrics(manifest.get("metrics", {})),
        "eval": {
            "test_records": manifest["eval"]["test_records"],
            "failed_tests": redact_json(manifest["eval"]["failed_tests"][:MAX_FAILED_TESTS]),
            "failed_tests_omitted": max(0, len(manifest["eval"]["failed_tests"]) - MAX_FAILED_TESTS),
            "error_code": redact_json(manifest["eval"]["error_code"]),
            "test_branch_errors": redact_json(manifest["eval"]["test_branch_errors"]),
            "warnings": redact_json(manifest["eval"]["warnings"]),
            "summary_path": eval_summary_path,
            "public_eval_path": eval_json_path,
        },
        "usage_audit": {
            "available_local_artifact": bool(manifest["copied_files"].get("usage_audit")),
            "public_path": "usage-audit.json" if manifest["copied_files"].get("usage_audit") else "",
        },
        "package": {
            "contents": manifest["package"]["contents"][:MAX_PACKAGE_FILES],
            "contents_omitted": max(0, len(manifest["package"]["contents"]) - MAX_PACKAGE_FILES),
            "submission_available_local_only": bool(manifest["copied_files"].get("submission")),
        },
        "agent_trace": {
            "codex_logs_available_local_only": bool(
                [path for path in manifest["copied_files"].get("codex_logs", []) if path]
            ),
            "raw_logs_published": False,
        },
    }


def export_one(manifest_path: Path, output_dir: Path) -> None:
    manifest = read_json(manifest_path)
    instance_id = manifest["run"]["instance_id"]
    run_name = manifest.get("metrics", {}).get("run_name") or manifest["run"]["run_name"]
    target_dir = output_dir / run_name / instance_id
    target_dir.mkdir(parents=True, exist_ok=True)

    eval_json_path = manifest_path.parent / f"{instance_id}.eval.json"
    summary_name = "eval-summary.json"
    public_eval_name = "eval.json"
    (target_dir / summary_name).write_text(
        json.dumps(eval_summary(read_json(eval_json_path)), indent=2, sort_keys=True) + "\n"
    )
    (target_dir / public_eval_name).write_text(
        json.dumps(public_eval(read_json(eval_json_path)), indent=2, sort_keys=True) + "\n"
    )
    usage_audit_path = manifest_path.parent / "usage-audit.json"
    if usage_audit_path.is_file():
        (target_dir / "usage-audit.json").write_text(
            json.dumps(public_usage_audit(read_json(usage_audit_path), instance_id), indent=2, sort_keys=True) + "\n"
        )
    (target_dir / "manifest.json").write_text(
        json.dumps(public_manifest(manifest, summary_name, public_eval_name), indent=2, sort_keys=True) + "\n"
    )
    print(target_dir)


def csv_rows(path: str) -> list[dict]:
    with Path(path).expanduser().open(newline="") as f:
        return list(csv.DictReader(f))


def result_pairs(paths: list[str]) -> set[tuple[str, str]]:
    return {(row["run_name"], row["instance_id"]) for path in paths for row in csv_rows(path)}


def manifest_key(manifest_path: Path) -> tuple[str, str]:
    manifest = read_json(manifest_path)
    return manifest.get("metrics", {}).get("run_name") or manifest["run"]["run_name"], manifest["run"]["instance_id"]


def selected_manifests(artifacts_dir: Path, wanted: set[tuple[str, str]]) -> list[Path]:
    if not wanted:
        return []
    return [
        manifest_path
        for manifest_path in sorted(artifacts_dir.glob("*/*/manifest.json"))
        if manifest_key(manifest_path) in wanted
    ]


def export(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    if args.clean_output and output_dir.exists():
        shutil.rmtree(output_dir)
    for manifest_path in selected_manifests(Path(args.artifacts_dir), result_pairs(args.results_csv)):
        export_one(manifest_path, output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export sanitized public evidence from local run artifacts")
    parser.add_argument("--artifacts-dir", default=str(LOCAL_ARTIFACTS))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--results-csv", action="append", default=[])
    parser.add_argument("--clean-output", action="store_true")
    export(parser.parse_args())


if __name__ == "__main__":
    main()
