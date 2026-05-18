#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shlex
import tarfile
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_TOOLS = (
    "brew",
    "dtruss",
    "gdb",
    "hexdump",
    "lldb",
    "ltrace",
    "nm",
    "objdump",
    "otool",
    "perf",
    "readelf",
    "strings",
    "strace",
    "uv",
    "xxd",
)
SOURCE_LOOKUP_PATTERNS = (
    r"\bcargo\s+install\b",
    r"\bcargo\s+search\b",
    r"\bcargo\s+add\b",
    r"\bgo\s+get\b",
    r"\bgo\s+install\s+[\w./-]+@",
    r"\bpip3?\s+install\b",
    r"\bnpm\s+install\b",
    r"\byarn\s+add\b",
    r"\bpnpm\s+add\b",
    r"\bapt(-get)?\s+source\b",
    r"\bapt(-get)?\s+install\b",
    r"\bbrew\s+install\b",
    r"\bgh\s+repo\s+clone\b",
    r"\bgit\s+clone\b",
    r"\bgit\s+remote\b",
    r"\bgit\s+fetch\b",
    r"\bgit\s+pull\b",
    r"\bgit\s+checkout\b",
    r"\.cargo/registry/src",
    r"/go/pkg/mod",
    r"\$\(go env GOPATH\)/pkg/mod",
)
GIT_SOURCE_LOOKUP_PATTERNS = {
    r"\bgit\s+clone\b",
    r"\bgit\s+remote\b",
    r"\bgit\s+fetch\b",
    r"\bgit\s+pull\b",
    r"\bgit\s+checkout\b",
}
LOCALHOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}
HOST_PATH_MARKERS = (
    "/" + "Users" + "/",
    "/Documents/" + "ProgramBench",
)
BINARY_ANALYSIS_TOOLS = (
    "dtruss",
    "file",
    "gdb",
    "hexdump",
    "lldb",
    "ltrace",
    "nm",
    "objdump",
    "otool",
    "perf",
    "readelf",
    "strings",
    "strace",
    "xxd",
)
TARGET_EXECUTABLE_INSPECTION_TOOLS = (
    "cat",
    "cp",
    "head",
    "ls",
    "md5sum",
    "readlink",
    "sha256sum",
    "shasum",
    "stat",
    "tail",
    "wc",
)
PARENT_INSPECTION = re.compile(r"(^|[;&|]\s*)(cat|find|grep|head|ls|rg|sed|tail|wc)\s+[^;&|]*\.\.")
HARNESS_PARENT_PATH = re.compile(
    r"\.\./(?:package-submission|start-target|check-compliance|eval-submission|run\.json|GOAL_PROMPT|GOAL_OBJECTIVE)"
)
WRAPPER_PATTERNS = (
    r"\bdocker\s+exec\b",
    r"/ProgramBench(?:/|\b)",
    r"\bprogrambench\s+(?:eval|run|solve|benchmark)\b",
    r"\bprogrambench/",
    r"\bsubprocess\.[^(]+\([^)\n]*(?:/workspace/executable|['\"]executable['\"])",
    r"\bos\.(?:system|popen|execv|execve|spawnv|spawnve)\([^)\n]*/workspace/executable",
    r"\bCommand::new\([^)]*/workspace/executable",
)
SCRATCH_ARTIFACT_NAME = re.compile(r"(^|[_-])(probe|probes|compare|fuzz|fuzzer|fixture|fixtures)([_\.-]|$)")


@dataclass
class Finding:
    source: str
    message: str
    command: str = ""


def session_meta(path: Path) -> dict | None:
    return next(
        (event["payload"] for event in jsonl_events(path) if event.get("type") == "session_meta"),
        None,
    )


def jsonl_events(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def exec_calls(path: Path) -> list[tuple[int, dict, str]]:
    events = [(n, event, event.get("payload", {})) for n, event in enumerate(jsonl_events(path), start=1)]
    calls = [
        (n, payload["call_id"], json.loads(payload["arguments"]))
        for n, event, payload in events
        if event.get("type") == "response_item"
        and payload.get("type") == "function_call"
        and payload.get("name") == "exec_command"
    ]
    outputs = {
        payload["call_id"]: payload.get("output", "")
        for _, event, payload in events
        if event.get("type") == "response_item" and payload.get("type") == "function_call_output"
    }
    return [(line, call, outputs.get(call_id, "")) for line, call_id, call in calls]


def was_blocked(output: str) -> bool:
    return "blocked " in output


def was_rejected(output: str) -> bool:
    return "Failed to create unified exec process" in output


def uses_tool(command: str, tool: str) -> bool:
    for segment in command_segments(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            path = r"(?:/(?:bin|usr/bin|usr/local/bin|opt/homebrew/bin)/)?"
            if re.search(rf"^\s*{path}{re.escape(tool)}([\s()]|$)", segment):
                return True
            continue
        index = command_token_index(tokens)
        if index is not None and Path(tokens[index]).name == tool:
            return True
    return False


def uses_binary_analysis_on_target(command: str, tool: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tool_path = rf"(?:/(?:bin|usr/bin|usr/local/bin|opt/homebrew/bin)/)?{re.escape(tool)}"
        return any(
            bool(re.search(rf"^\s*{tool_path}([\s;&|()]|$)[^;&|]*?/workspace/executable", segment))
            for segment in re.split(r"(?:;|\n|&&|\|\|)", command)
        )
    for token in tokens:
        if (
            token != command
            and ("\n" in token or ";" in token or "/workspace/executable" in token)
            and any(
                uses_binary_analysis_on_target(segment, tool)
                for segment in re.split(r"(?:;|\n|&&|\|\|)", token)
                if segment.strip()
            )
        ):
            return True
    index = command_token_index(tokens)
    if (
        index is not None
        and Path(tokens[index]).name == tool
        and any("/workspace/executable" in later for later in tokens[index + 1 :])
    ):
        return True
    return False


def uses_target_executable_inspection(command: str, tool: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tool_path = rf"(?:/(?:bin|usr/bin|usr/local/bin|opt/homebrew/bin)/)?{re.escape(tool)}"
        return any(
            bool(re.search(rf"^\s*{tool_path}([\s;&|()]|$)[^;&|]*?/workspace/executable", segment))
            for segment in re.split(r"(?:;|\n|&&|\|\|)", command)
        )
    for token in tokens:
        if (
            token != command
            and ("\n" in token or ";" in token or "/workspace/executable" in token)
            and any(
                uses_target_executable_inspection(segment, tool)
                for segment in re.split(r"(?:;|\n|&&|\|\|)", token)
                if segment.strip()
            )
        ):
            return True
    index = command_token_index(tokens)
    return bool(
        index is not None
        and Path(tokens[index]).name == tool
        and any("/workspace/executable" in later for later in tokens[index + 1 :])
    )


def command_token_index(tokens: list[str]) -> int | None:
    index = 0
    while index < len(tokens) and "=" in tokens[index] and not tokens[index].startswith(("/", "./")):
        index += 1
    if index >= len(tokens):
        return None
    if Path(tokens[index]).name == "sudo":
        index += 1
        while index < len(tokens) and tokens[index].startswith("-"):
            index += 1
    if index >= len(tokens):
        return None
    if Path(tokens[index]).name == "env":
        index += 1
        while index < len(tokens) and "=" in tokens[index] and not tokens[index].startswith(("/", "./")):
            index += 1
    return index if index < len(tokens) else None


def uses_allowed_docker(command: str, container_name: str) -> bool:
    return bool(re.search(rf"\bdocker\s+exec\s+(?:-i\s+)?-u\s+agent\s+{re.escape(container_name)}\b", command))


def uses_allowed_local_tools_docker(command: str, container_name: str) -> bool:
    escaped = re.escape(container_name)
    return bool(
        re.search(rf"\bdocker\s+exec\s+(?:-i\s+)?(?:-u\s+agent\s+)?{escaped}\b", command)
        or re.search(rf"\bdocker\s+exec\s+-u\s+agent\s+(?:-i\s+)?{escaped}\b", command)
        or re.search(rf"\bdocker\s+cp\s+{escaped}:/", command)
        or re.search(rf"\bdocker\s+inspect\s+{escaped}\b", command)
    )


def command_segments(command: str) -> list[str]:
    return [segment.strip() for segment in re.split(r"(?:;|\n|&&|\|\||\|)", command) if segment.strip()]


def is_parent_path_arg(arg: str) -> bool:
    if arg in {"../submission.tar.gz", "../submission.tgz"}:
        return False
    return arg == ".." or arg.startswith("../") or "/../" in arg or arg.endswith("/..")


def segment_inspects_parent_directory(segment: str) -> bool:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return bool(PARENT_INSPECTION.search(segment))
    if not tokens:
        return False
    tool = Path(tokens[0]).name
    if tool not in {"cat", "find", "grep", "head", "ls", "rg", "sed", "tail", "wc"}:
        return False
    skip_next = False
    skip_output_redirect = False
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if skip_output_redirect:
            skip_output_redirect = False
            continue
        if tool == "rg" and token in {"-g", "--glob"}:
            skip_next = True
            continue
        if tool == "rg" and (token.startswith("--glob=") or token.startswith("-g")):
            continue
        if token in {">", ">>", "1>", "1>>", "2>", "2>>", "&>", "&>>"}:
            skip_output_redirect = True
            continue
        if re.match(r"^(?:\d|&)?>>?", token):
            continue
        if token.startswith("-"):
            continue
        if is_parent_path_arg(token):
            return True
    return False


def inspects_parent_directory(command: str) -> bool:
    return any(segment_inspects_parent_directory(segment) for segment in command_segments(command))


def source_lookup_patterns(command: str) -> list[str]:
    patterns = []
    for pattern in SOURCE_LOOKUP_PATTERNS:
        if not re.search(pattern, command):
            continue
        if pattern == r"\bgit\s+remote\b" and re.search(r"\bgit\s+remote\s+add\b", command):
            continue
        if pattern in GIT_SOURCE_LOOKUP_PATTERNS and git_command_uses_only_local_fixture(command):
            continue
        if pattern == r"\bgit\s+clone\b" and not git_clone_looks_like_source_lookup(command):
            continue
        patterns.append(pattern)
    return patterns


def git_command_uses_only_local_fixture(command: str) -> bool:
    if not re.search(r"\bgit\s+init\b", command):
        return False
    return not re.search(r"(?:https?|ssh|git)://|[\w.-]+@[\w.-]+:|github\.com|gitlab\.com|bitbucket\.org", command)


def git_clone_looks_like_source_lookup(command: str) -> bool:
    if 'grep -E "path:|git clone"' in command or "grep -E 'path:|git clone'" in command:
        return False
    for segment in command_segments(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            if re.search(r"\bgit\s+clone\s+(?:https?://|ssh://|git@)", segment):
                return True
            continue
        for index, token in enumerate(tokens[:-1]):
            if Path(token).name != "git" or tokens[index + 1] != "clone":
                continue
            source = git_clone_source_arg(tokens[index + 2 :])
            if source and clone_source_is_remote_or_host_path(source):
                return True
    return False


def git_clone_source_arg(tokens: list[str]) -> str:
    options_with_values = {
        "-b",
        "-c",
        "-j",
        "-o",
        "--branch",
        "--config",
        "--depth",
        "--jobs",
        "--origin",
        "--reference",
        "--reference-if-able",
        "--separate-git-dir",
        "--template",
    }
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token in options_with_values:
            skip_next = True
            continue
        if token.startswith("--") and "=" in token:
            continue
        if token.startswith("-"):
            continue
        return token
    return ""


def clone_source_is_remote_or_host_path(source: str) -> bool:
    if re.match(r"^(?:https?|ssh|git)://", source):
        return True
    if re.match(r"^[\w.-]+@[\w.-]+:", source):
        return True
    if any(host in source for host in ("github.com", "gitlab.com", "bitbucket.org")):
        return True
    return source.startswith(("..", "/", "~"))


def fetches_external_url(command: str) -> str:
    return next(
        (
            match.group(0)
            for segment in command_segments(command)
            for match in re.finditer(r"^\s*(?:curl|wget)\b[^;&|]*\bhttps?://([^/\s'\"]+)", segment)
            if match.group(1).rsplit(":", 1)[0] not in LOCALHOSTS
        ),
        "",
    )


def is_inside(path: str | Path, root: Path) -> bool:
    candidate = Path(path).expanduser()
    return candidate == root or root in candidate.parents


def resolve_exec_workdir(workdir: str, session_cwd: Path | None, solution_dir: Path) -> Path:
    candidate = Path(workdir).expanduser()
    if not candidate.is_absolute():
        candidate = (session_cwd or solution_dir) / candidate
    return candidate.resolve()


def find_session_logs(instance_dir: Path, sessions_roots: list[Path]) -> list[Path]:
    solution_dir = str(instance_dir / "solution")
    return [
        path
        for sessions_root in sessions_roots
        if sessions_root.is_dir()
        for path in sorted(sessions_root.glob("**/*.jsonl"))
        if (session_meta(path) or {}).get("cwd") == solution_dir
    ]


def text_files(root: Path) -> list[Path]:
    return [path for path in sorted(root.rglob("*")) if is_small_text_file(path)]


def is_small_text_file(path: Path) -> bool:
    if not (
        path.is_file()
        and ".goal" not in path.parts
        and "probes" not in path.parts
        and not any(SCRATCH_ARTIFACT_NAME.search(part.lower()) for part in path.parts)
        and path.name != "AGENT_RULES.md"
        and path.stat().st_size < 2_000_000
    ):
        return False
    try:
        path.read_text(errors="strict")
    except UnicodeDecodeError:
        return False
    return True


def audit_solution_files(solution_dir: Path) -> list[Finding]:
    return [
        Finding(str(path), f"solution file contains wrapper/evaluator pattern: {pattern}")
        for path in text_files(solution_dir)
        for pattern in WRAPPER_PATTERNS
        if re.search(pattern, path.read_text(errors="replace"))
    ]


def audit_behavior_audit_file(solution_dir: Path) -> list[Finding]:
    path = solution_dir / ".goal" / "BEHAVIOR_AUDIT.md"
    if not path.is_file():
        return [Finding(str(path), "missing behavior audit required by goal prompt")]
    if not path.read_text(errors="replace").strip():
        return [Finding(str(path), "behavior audit is empty")]
    return []


def audit_submission_archive(instance_dir: Path) -> list[Finding]:
    archive = instance_dir / "submission.tar.gz"
    if not archive.exists():
        return []
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    return [
        *(
            []
            if any(name.strip("./") == "compile.sh" for name in names)
            else [Finding(str(archive), "submission archive does not include compile.sh")]
        ),
        *(
            [Finding(str(archive), "submission archive includes harness-only AGENT_RULES.md")]
            if any(name.strip("./") == "AGENT_RULES.md" for name in names)
            else []
        ),
        *[
            Finding(str(archive), f"submission archive contains unsafe path: {name}")
            for name in names
            if name.startswith("/") or ".." in Path(name).parts
        ],
    ]


def audit_command(
    line_source: str,
    call: dict,
    output: str,
    solution_dir: Path,
    session_cwd: Path | None,
    container_name: str,
    allow_local_tools: bool,
) -> list[Finding]:
    findings = []
    if was_blocked(output) or was_rejected(output):
        return findings
    command = call.get("cmd", "")
    if not command:
        return findings
    workdir = call.get("workdir", "")
    if workdir and not is_inside(resolve_exec_workdir(workdir, session_cwd, solution_dir), solution_dir):
        findings.append(Finding(line_source, f"exec workdir escapes solution dir: {workdir}", command))
    if any(marker in command for marker in HOST_PATH_MARKERS):
        findings.append(Finding(line_source, "command contains private host or evaluator path", command))
    if inspects_parent_directory(command):
        findings.append(Finding(line_source, "command inspects parent directories from solution workspace", command))
    elif HARNESS_PARENT_PATH.search(command):
        findings.append(
            Finding(line_source, "command uses parent-directory traversal from solution workspace", command)
        )
    if uses_tool(command, "docker") and not (
        uses_allowed_local_tools_docker(command, container_name)
        if allow_local_tools
        else uses_allowed_docker(command, container_name)
    ):
        findings.append(Finding(line_source, "docker command does not use the allowed target exec form", command))
    if "/workspace/executable" in command and not allow_local_tools:
        findings.extend(
            Finding(line_source, f"binary analysis tool used on target executable: {tool}", command)
            for tool in BINARY_ANALYSIS_TOOLS
            if uses_binary_analysis_on_target(command, tool)
        )
        findings.extend(
            Finding(line_source, f"target executable inspection command: {tool}", command)
            for tool in TARGET_EXECUTABLE_INSPECTION_TOOLS
            if uses_target_executable_inspection(command, tool)
        )
    findings.extend(
        Finding(line_source, f"source/package lookup pattern: {pattern}", command)
        for pattern in source_lookup_patterns(command)
    )
    if fetch := fetches_external_url(command):
        findings.append(Finding(line_source, f"external URL fetch: {fetch}", command))
    findings.extend(
        Finding(line_source, f"forbidden no-internet host/tool command: {tool}", command)
        for tool in FORBIDDEN_TOOLS
        if tool not in BINARY_ANALYSIS_TOOLS and uses_tool(command, tool) and tool != "docker"
    )
    return findings


def audit(args: argparse.Namespace) -> None:
    instance_dir = Path(args.instance_dir).expanduser().resolve()
    run = json.loads((instance_dir / "run.json").read_text())
    solution_dir = instance_dir / "solution"
    findings = []

    if not (solution_dir / "compile.sh").is_file():
        findings.append(Finding(str(solution_dir / "compile.sh"), "missing ProgramBench compile.sh"))
    if (instance_dir / "submission.tar.gz").is_file() and not (solution_dir / "compile.sh").is_file():
        findings.append(
            Finding(str(instance_dir / "submission.tar.gz"), "submission exists but cannot compile without compile.sh")
        )
    if run["inference_mode"] == "no-internet":
        findings.extend(audit_behavior_audit_file(solution_dir))
    findings.extend(audit_solution_files(solution_dir))
    findings.extend(audit_submission_archive(instance_dir))

    logs = find_session_logs(instance_dir, [Path(path).expanduser() for path in args.codex_sessions])
    if not logs:
        findings.append(Finding(str(instance_dir), "no Codex JSONL session logs found for solution cwd"))
    blocked_attempts = 0
    log_cwds = {log: Path((session_meta(log) or {}).get("cwd", "")).expanduser().resolve() for log in logs}
    calls = [(log, line, call, output) for log in logs for line, call, output in exec_calls(log)]
    blocked_attempts = sum(was_blocked(output) for _, _, _, output in calls)
    rejected_attempts = sum(was_rejected(output) for _, _, _, output in calls)
    findings.extend(
        finding
        for log, line, call, output in calls
        for finding in audit_command(
            f"{log}:{line}",
            call,
            output,
            solution_dir,
            log_cwds.get(log),
            run["container_name"],
            run.get("inference_mode") == "no-internet-local-tools",
        )
    )

    if findings:
        print("\n".join(failure_text(finding) for finding in findings))
        raise SystemExit(1)
    print(f"OK audit passed for {instance_dir}")
    print(f"session_logs={';'.join(str(path) for path in logs)}")
    print(f"blocked_attempts={blocked_attempts}")
    print(f"rejected_exec_attempts={rejected_attempts}")


def failure_text(finding: Finding) -> str:
    return (
        f"FAIL {finding.source}: {finding.message}\n  cmd: {finding.command}"
        if finding.command
        else f"FAIL {finding.source}: {finding.message}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit a Codex /goal ProgramBench run for no-internet gaps")
    parser.add_argument("instance_dir")
    parser.add_argument("--codex-sessions", nargs="+", default=[str(Path.home() / ".codex" / "sessions")])
    audit(parser.parse_args())


if __name__ == "__main__":
    main()
