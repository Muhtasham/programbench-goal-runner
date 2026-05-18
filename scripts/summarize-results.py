#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class TokenUsage:
    calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    started_at: str = ""
    ended_at: str = ""
    session_logs: list[str] = field(default_factory=list)
    call_usages: list[dict] = field(default_factory=list)

    def estimated_cost_usd(self, rates: dict | None) -> float | None:
        input_rate = os.environ.get("CODEX_INPUT_USD_PER_MTOK")
        cached_rate = os.environ.get("CODEX_CACHED_INPUT_USD_PER_MTOK")
        output_rate = os.environ.get("CODEX_OUTPUT_USD_PER_MTOK")
        if input_rate is None and rates:
            input_rate = str(rates["input_usd_per_mtok"])
        if cached_rate is None and rates:
            cached_rate = str(rates["cached_input_usd_per_mtok"])
        if output_rate is None and rates:
            output_rate = str(rates["output_usd_per_mtok"])
        if input_rate is None or cached_rate is None or output_rate is None:
            return None
        if rates and self.call_usages and rates.get("long_context_input_threshold_tokens"):
            threshold = int(rates["long_context_input_threshold_tokens"])
            long_context = [usage for usage in self.call_usages if usage["input_tokens"] > threshold]
            normal = [usage for usage in self.call_usages if usage["input_tokens"] <= threshold]
            return (
                self.usage_cost(normal, float(input_rate), float(cached_rate), float(output_rate))
                + self.usage_cost(
                    long_context,
                    float(input_rate) * float(rates["long_context_input_multiplier"]),
                    float(cached_rate) * float(rates.get("long_context_cached_input_multiplier", 1)),
                    float(output_rate) * float(rates["long_context_output_multiplier"]),
                )
            ) / 1_000_000
        return (
            self.usage_cost(
                [
                    {
                        "input_tokens": self.input_tokens,
                        "cached_input_tokens": self.cached_input_tokens,
                        "output_tokens": self.output_tokens,
                    }
                ],
                float(input_rate),
                float(cached_rate),
                float(output_rate),
            )
            / 1_000_000
        )

    @staticmethod
    def usage_cost(usages: list[dict], input_rate: float, cached_rate: float, output_rate: float) -> float:
        uncached_input = sum(max(0, usage["input_tokens"] - usage["cached_input_tokens"]) for usage in usages)
        return (
            uncached_input * float(input_rate)
            + sum(usage["cached_input_tokens"] for usage in usages) * float(cached_rate)
            + sum(usage["output_tokens"] for usage in usages) * float(output_rate)
        )

    def long_context_calls(self, rates: dict | None) -> int:
        if not rates or not self.call_usages or not rates.get("long_context_input_threshold_tokens"):
            return 0
        threshold = int(rates["long_context_input_threshold_tokens"])
        return sum(usage["input_tokens"] > threshold for usage in self.call_usages)

    @property
    def wall_clock_seconds(self) -> int:
        if not self.started_at or not self.ended_at:
            return 0
        return int((parse_timestamp(self.ended_at) - parse_timestamp(self.started_at)).total_seconds())


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_programbench(programbench_repo: Path) -> tuple:
    sys.path.insert(0, str(programbench_repo / "src"))
    from programbench.eval.eval import EvaluationResult  # ty: ignore[unresolved-import]
    from programbench.eval.eval_batch import InstanceEvalSummary  # ty: ignore[unresolved-import]
    from programbench.utils.load_data import (  # ty: ignore[unresolved-import]
        get_active_branches,
        get_ignored_tests,
        load_all_instances,
    )

    return (
        EvaluationResult,
        InstanceEvalSummary,
        get_active_branches,
        get_ignored_tests,
        {instance["instance_id"]: instance for instance in load_all_instances(include_tests=True)},
    )


def score_eval(eval_json: Path, programbench: tuple) -> dict:
    EvaluationResult, InstanceEvalSummary, get_active_branches, get_ignored_tests, instances = programbench
    instance_id = eval_json.parent.name
    result = EvaluationResult.model_validate_json(eval_json.read_text())
    if instance_id in instances:
        active = get_active_branches(instances[instance_id])
        ignored_branches = {branch for branch in result.test_branches if branch not in set(active)}
        result = result.for_branches(active).without_ignored(get_ignored_tests(instances[instance_id]))
        if ignored_branches:
            result.warnings = [
                warning
                for warning in result.warnings
                if not any(f"branch {branch}" in warning for branch in ignored_branches)
            ]
    summary = InstanceEvalSummary.from_eval_result(instance_id, result)
    evaluator_problem = bool(
        summary.error_code or summary.test_branch_errors or summary.n_system_errors or summary.n_warnings
    )
    return {
        "instance_id": instance_id,
        "score": summary.score,
        "resolved": summary.score == 1.0 and summary.n_tests > 0,
        "almost_resolved": summary.score >= 0.95 and summary.n_tests > 0,
        "n_resolved_tests": summary.n_resolved,
        "n_tests": summary.n_tests,
        "evaluator_problem": evaluator_problem,
        "error_code": summary.error_code or "",
        "test_branch_errors": json.dumps(summary.test_branch_errors, sort_keys=True),
        "n_system_errors": summary.n_system_errors,
        "n_warnings": summary.n_warnings,
    }


def session_meta(path: Path) -> dict | None:
    with path.open(errors="replace") as f:
        for line in f:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "session_meta":
                return event["payload"]
    return None


def token_usage(path: Path) -> TokenUsage:
    usage = TokenUsage(calls=0, session_logs=[str(path)], call_usages=[])
    with path.open(errors="replace") as f:
        for line in f:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("timestamp"):
                usage.started_at = usage.started_at or event["timestamp"]
                usage.ended_at = event["timestamp"]
            payload = event.get("payload", {})
            if event.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not info:
                continue
            usage.calls += 1
            total = info["total_token_usage"]
            usage.input_tokens = total["input_tokens"]
            usage.cached_input_tokens = total["cached_input_tokens"]
            usage.output_tokens = total["output_tokens"]
            usage.reasoning_output_tokens = total["reasoning_output_tokens"]
            usage.total_tokens = total["total_tokens"]
            if info.get("last_token_usage"):
                usage.call_usages.append(info["last_token_usage"])
    return usage


def add_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    return TokenUsage(
        calls=left.calls + right.calls,
        input_tokens=left.input_tokens + right.input_tokens,
        cached_input_tokens=left.cached_input_tokens + right.cached_input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        reasoning_output_tokens=left.reasoning_output_tokens + right.reasoning_output_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
        started_at=min(filter(None, [left.started_at, right.started_at]), default=""),
        ended_at=max(filter(None, [left.ended_at, right.ended_at]), default=""),
        session_logs=[*left.session_logs, *right.session_logs],
        call_usages=[*left.call_usages, *right.call_usages],
    )


def find_codex_usage(instance_dir: Path, sessions_roots: list[Path]) -> TokenUsage:
    solution_dir = str(instance_dir / "solution")
    total = TokenUsage(session_logs=[])
    for path in [path for root in sessions_roots if root.is_dir() for path in root.glob("**/*.jsonl")]:
        meta = session_meta(path)
        if meta and meta.get("cwd") == solution_dir:
            total = add_usage(total, token_usage(path))
    return total


def format_cost(cost: float | None) -> str:
    return "" if cost is None else f"{cost:.4f}"


def load_pricing_snapshot(path: str) -> dict:
    pricing_path = Path(path).expanduser()
    if not pricing_path.is_file():
        return {}
    return json.loads(pricing_path.read_text())


def load_pricing(path: str) -> dict:
    return load_pricing_snapshot(path).get("models", {})


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def pricing_age_hours(snapshot: dict) -> float | None:
    if not snapshot.get("fetched_at"):
        return None
    return (datetime.now(timezone.utc) - parse_timestamp(snapshot["fetched_at"])).total_seconds() / 3600


def pricing_snapshot_summary(snapshot: dict, max_age_hours: int) -> dict:
    age = pricing_age_hours(snapshot)
    return {
        "fetched_at": snapshot.get("fetched_at", ""),
        "source": snapshot.get("source", ""),
        "source_type": snapshot.get("source_type", ""),
        "pricing_api_endpoint": snapshot.get("pricing_api_endpoint"),
        "age_hours": round(age, 2) if age is not None else None,
        "max_age_hours": max_age_hours,
        "models": {
            model: {
                "source_url": pricing.get("source_url", ""),
                "input_usd_per_mtok": pricing.get("input_usd_per_mtok"),
                "cached_input_usd_per_mtok": pricing.get("cached_input_usd_per_mtok"),
                "output_usd_per_mtok": pricing.get("output_usd_per_mtok"),
                "long_context_input_threshold_tokens": pricing.get("long_context_input_threshold_tokens"),
                "long_context_input_multiplier": pricing.get("long_context_input_multiplier"),
                "long_context_cached_input_multiplier": pricing.get("long_context_cached_input_multiplier"),
                "long_context_output_multiplier": pricing.get("long_context_output_multiplier"),
            }
            for model, pricing in snapshot.get("models", {}).items()
        },
        "notes": snapshot.get("notes", []),
    }


def usage_audit(rows: list[dict], pricing_file: Path, pricing_snapshot: dict, max_pricing_age_hours: int) -> dict:
    age = pricing_age_hours(pricing_snapshot)
    warnings = [
        warning
        for row in rows
        for warning in (
            f"{row['instance_id']}: no Codex session logs matched solution cwd" if not row["session_logs"] else "",
            f"{row['instance_id']}: no estimated cost; missing pricing for {row['model']}"
            if not row["estimated_cost_usd"]
            else "",
            f"{row['instance_id']}: pricing source missing for {row['model']}" if not row["pricing_source"] else "",
        )
        if warning
    ]
    if not pricing_snapshot:
        warnings.append("pricing snapshot missing; costs require env overrides or remain blank")
    if age is not None and age > max_pricing_age_hours:
        warnings.append(f"pricing snapshot is stale: {age:.1f}h old exceeds {max_pricing_age_hours}h")
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "pricing_file": str(pricing_file),
        "pricing_file_sha256": file_sha256(pricing_file),
        "pricing_snapshot": pricing_snapshot_summary(pricing_snapshot, max_pricing_age_hours),
        "rows": [
            {
                "instance_id": row["instance_id"],
                "run_name": row["run_name"],
                "run_version": row.get("run_version", ""),
                "model": row["model"],
                "reasoning_effort": row["reasoning_effort"],
                "calls": row["calls"],
                "input_tokens": row["input_tokens"],
                "cached_input_tokens": row["cached_input_tokens"],
                "output_tokens": row["output_tokens"],
                "reasoning_output_tokens": row["reasoning_output_tokens"],
                "total_tokens": row["total_tokens"],
                "long_context_calls": row["long_context_calls"],
                "estimated_cost_usd": row["estimated_cost_usd"],
                "pricing_source": row["pricing_source"],
                "session_logs": row["session_logs"].split(";") if row["session_logs"] else [],
            }
            for row in rows
        ],
        "totals": {
            "calls": sum(row["calls"] for row in rows),
            "input_tokens": sum(row["input_tokens"] for row in rows),
            "cached_input_tokens": sum(row["cached_input_tokens"] for row in rows),
            "output_tokens": sum(row["output_tokens"] for row in rows),
            "reasoning_output_tokens": sum(row["reasoning_output_tokens"] for row in rows),
            "total_tokens": sum(row["total_tokens"] for row in rows),
            "long_context_calls": sum(row["long_context_calls"] for row in rows),
            "estimated_cost_usd": sum(float(row["estimated_cost_usd"] or 0) for row in rows),
        },
        "warnings": warnings,
        "notes": [
            "Costs are estimates from local Codex token_count logs, not authoritative billed dollars.",
            "Pricing comes from an official-doc snapshot because there is no supported structured pricing endpoint.",
            "reasoning_output_tokens is preserved for audit only; it is not added separately to output_tokens.",
            "long_context_calls counts Codex calls whose last_token_usage.input_tokens exceeded the pricing threshold.",
        ],
    }


def legacy_compliance_marker(_run: dict) -> bool:
    return False


def result_row(eval_json: Path, programbench: tuple, codex_sessions: list[Path], pricing: dict) -> dict:
    instance_dir = eval_json.parent
    run = json.loads((instance_dir / "run.json").read_text()) if (instance_dir / "run.json").is_file() else {}
    usage = find_codex_usage(instance_dir, codex_sessions)
    model = run.get("model", "")
    rates = pricing.get(model)
    return {
        **score_eval(eval_json, programbench),
        "run_name": run.get("run_name", ""),
        "run_version": run.get("run_version", ""),
        "model": model,
        "reasoning_effort": run.get("reasoning_effort", ""),
        "codex_version": run.get("codex_version", ""),
        "inference_mode": run.get("inference_mode", ""),
        "paper_compliant": legacy_compliance_marker(run),
        "host_system": run.get("host_system", ""),
        "host_machine": run.get("host_machine", ""),
        "docker_cpus": run.get("docker_cpus", ""),
        "docker_memory": run.get("docker_memory", ""),
        "created_at": run.get("created_at", ""),
        "calls": usage.calls,
        "wall_clock_seconds": usage.wall_clock_seconds,
        "session_started_at": usage.started_at,
        "session_ended_at": usage.ended_at,
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
        "total_tokens": usage.total_tokens,
        "long_context_calls": usage.long_context_calls(rates),
        "estimated_cost_usd": format_cost(usage.estimated_cost_usd(rates)),
        "pricing_source": pricing.get(model, {}).get("source_url", ""),
        "session_logs": ";".join(usage.session_logs),
    }


def summarize(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir).expanduser()
    programbench_repo = Path(args.programbench_repo).expanduser()
    programbench = load_programbench(programbench_repo)
    pricing_file = Path(args.pricing_file).expanduser()
    pricing_snapshot = load_pricing_snapshot(args.pricing_file)
    pricing = pricing_snapshot.get("models", {})
    codex_sessions = [Path(path).expanduser() for path in args.codex_sessions]
    rows = [
        result_row(eval_json, programbench, codex_sessions, pricing)
        for eval_json in sorted(run_dir.glob("**/*.eval.json"))
    ]

    if args.output:
        output_path = Path(args.output).expanduser()
        with output_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else [])
            writer.writeheader()
            writer.writerows(rows)
        usage_audit_path = (
            Path(args.usage_audit_output).expanduser()
            if args.usage_audit_output
            else output_path.with_name("usage-audit.json")
        )
        usage_audit_path.parent.mkdir(parents=True, exist_ok=True)
        usage_audit_path.write_text(
            json.dumps(
                usage_audit(rows, pricing_file, pricing_snapshot, args.max_pricing_age_hours),
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        print(f"usage_audit,{usage_audit_path}")

    total = len(rows)
    resolved = sum(row["resolved"] for row in rows)
    almost = sum(row["almost_resolved"] for row in rows)
    avg_pass = sum(row["score"] for row in rows) / total if total else 0
    print(f"instances,{total}")
    print(f"resolved_rate,{resolved / total if total else 0:.4f}")
    print(f"almost_resolved_rate,{almost / total if total else 0:.4f}")
    print(f"average_pass_rate,{avg_pass:.4f}")
    print(f"calls,{sum(row['calls'] for row in rows)}")
    print(f"wall_clock_hours,{sum(row['wall_clock_seconds'] for row in rows) / 3600:.2f}")
    costs = [float(row["estimated_cost_usd"]) for row in rows if row["estimated_cost_usd"]]
    print(f"estimated_cost_usd,{sum(costs):.4f}" if costs else "estimated_cost_usd,")


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize ProgramBench /goal results")
    parser.add_argument("run_dir")
    parser.add_argument("--programbench-repo", required=True)
    parser.add_argument(
        "--codex-sessions",
        nargs="+",
        default=[
            str(Path.home() / ".codex" / "sessions"),
            str(Path.home() / ".codex" / "archived_sessions"),
        ],
    )
    parser.add_argument("--pricing-file", default="local_state/openai_pricing.json")
    parser.add_argument("--max-pricing-age-hours", type=int, default=24)
    parser.add_argument("--output", default="")
    parser.add_argument("--usage-audit-output", default="")
    summarize(parser.parse_args())


if __name__ == "__main__":
    main()
