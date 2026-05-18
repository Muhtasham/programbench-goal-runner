#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def expected_score_row(eval_json: Path, programbench: tuple) -> dict:
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
    return {
        "score": summary.score,
        "resolved": summary.score == 1.0 and summary.n_tests > 0,
        "almost_resolved": summary.score >= 0.95 and summary.n_tests > 0,
        "n_resolved_tests": summary.n_resolved,
        "n_tests": summary.n_tests,
        "n_system_errors": summary.n_system_errors,
        "n_warnings": summary.n_warnings,
    }


def check_fixture(run_dir: Path, summarize_results, programbench: tuple) -> None:
    for eval_json in sorted(run_dir.glob("*/*.eval.json")):
        actual = summarize_results.score_eval(eval_json, programbench)
        expected = expected_score_row(eval_json, programbench)
        mismatches = {key: (actual[key], value) for key, value in expected.items() if actual[key] != value}
        if mismatches:
            raise SystemExit(f"metric mismatch for {eval_json}: {mismatches}")
        print(f"{eval_json.parent.name}: score={actual['score']:.4f} resolved={actual['resolved']}")


def check_aggregate(build_report) -> None:
    rows = [
        build_report.ResultRow(
            instance_id=str(index),
            run_name="synthetic",
            run_version="",
            model="gpt-5.5",
            reasoning_effort="xhigh",
            inference_mode="mini-swe-compatible-nointernet",
            paper_compliant=False,
            score=1.0 if index == 0 else 0.96 if index < 27 else 0.5,
            resolved=index == 0,
            almost_resolved=index < 27,
            evaluator_problem=False,
            error_code="",
            n_system_errors=0,
            n_warnings=0,
            n_resolved_tests=100,
            n_tests=100,
            calls=82 if index < 120 else 81,
            wall_clock_seconds=0,
            estimated_cost_usd=8.85,
            host_system="Linux",
            host_machine="x86_64",
            docker_cpus="20",
            docker_memory="60g",
            pricing_source="",
        )
        for index in range(200)
    ]
    summary = build_report.aggregate(rows)
    expected = {
        "resolved_rate": 0.005,
        "almost_resolved_rate": 0.135,
        "total_cost_usd": 1770.0,
        "total_calls": 16320,
        "average_cost_usd": 8.85,
        "average_calls": 81.6,
    }
    mismatches = {key: (summary[key], value) for key, value in expected.items() if summary[key] != value}
    if mismatches:
        raise SystemExit(f"aggregate mismatch: {mismatches}")
    print("aggregate: resolved=0.5% almost=13.5% total_cost=$1770 total_calls=16320")


def check_repeatability(build_report) -> None:
    rows = [
        build_report.ResultRow(
            instance_id="task",
            run_name=f"synthetic-{index}",
            run_version=f"v{index}",
            model="gpt-5.5",
            reasoning_effort="xhigh",
            inference_mode="mini-swe-compatible-nointernet",
            paper_compliant=False,
            score=score,
            resolved=score == 1.0,
            almost_resolved=score >= 0.95,
            evaluator_problem=False,
            error_code="",
            n_system_errors=0,
            n_warnings=0,
            n_resolved_tests=int(score * 100),
            n_tests=100,
            calls=80 + index,
            wall_clock_seconds=3600 * index,
            estimated_cost_usd=8 + index,
            host_system="Linux",
            host_machine="x86_64",
            docker_cpus="20",
            docker_memory="60g",
            pricing_source="",
        )
        for index, score in enumerate([0.8, 1.0], start=1)
    ]
    repeated = build_report.repeatability_groups(rows)
    if len(repeated) != 1:
        raise SystemExit(f"repeatability mismatch: {repeated}")
    expected = {
        "attempts": 2,
        "versions": ["v1", "v2"],
        "resolved_attempts": 1,
        "almost_attempts": 1,
    }
    mismatches = {key: (repeated[0][key], value) for key, value in expected.items() if repeated[0][key] != value}
    if round(repeated[0]["score_mean"], 6) != 0.9 or round(repeated[0]["score_delta"], 6) != 0.2:
        mismatches["score_stats"] = (repeated[0]["score_mean"], repeated[0]["score_delta"])
    if mismatches:
        raise SystemExit(f"repeatability mismatch: {mismatches}")
    print("repeatability: repeated version attempts are summarized")


def main() -> None:
    parser = argparse.ArgumentParser(description="Check ProgramBench metric parity")
    parser.add_argument("--programbench-repo", required=True)
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    programbench_repo = Path(args.programbench_repo).expanduser().resolve()
    summarize_results = load_module("summarize_results", repo / "scripts" / "summarize-results.py")
    build_report = load_module("build_report", repo / "scripts" / "build-report.py")
    programbench = summarize_results.load_programbench(programbench_repo)
    check_fixture(
        programbench_repo / "src" / "programbench" / "data" / "test_runs" / "correct",
        summarize_results,
        programbench,
    )
    check_fixture(
        programbench_repo / "src" / "programbench" / "data" / "test_runs" / "incorrect",
        summarize_results,
        programbench,
    )
    check_aggregate(build_report)
    check_repeatability(build_report)


if __name__ == "__main__":
    main()
