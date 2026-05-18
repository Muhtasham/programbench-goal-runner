#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import pwd
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path.home() / "pb-goal-runs"
MINI_SWE_COMPAT_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "programbench_goal_mini_swe_compatible.md"
PAPER_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "programbench_goal_paper_prompt.md"
GOAL_CONTRACT_PAPER_PROMPT_TEMPLATE = Path(__file__).parent / "prompts" / "programbench_goal_contract_paper_prompt.md"
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_REASONING_EFFORT = "xhigh"
DEFAULT_INFERENCE_MODE = "paper-prompt-nointernet"
MINI_SWE_COMPAT_MODE = "mini-swe-compatible-nointernet"
PAPER_PROMPT_MODE = "paper-prompt-nointernet"
GOAL_CONTRACT_PAPER_MODE = "paper-prompt-goal-contract-nointernet"
INFERENCE_MODES = (
    MINI_SWE_COMPAT_MODE,
    PAPER_PROMPT_MODE,
    GOAL_CONTRACT_PAPER_MODE,
)
NO_INTERNET_MODES = {
    MINI_SWE_COMPAT_MODE,
    PAPER_PROMPT_MODE,
    GOAL_CONTRACT_PAPER_MODE,
}
MODE_RUN_SEGMENTS = {
    MINI_SWE_COMPAT_MODE: "miniswecompat",
    PAPER_PROMPT_MODE: "paperprompt",
    GOAL_CONTRACT_PAPER_MODE: "goalcontract",
}
BLOCKED_ALWAYS_TOOLS = (
    "brew",
    "dtruss",
    "file",
    "gdb",
    "gh",
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
SOURCE_ACQUISITION_GUARDS = {
    "apt": r"(^| )install( |$)|(^| )source( |$)",
    "apt-get": r"(^| )install( |$)|(^| )source( |$)",
    "cargo": r"(^| )(install|search|add|fetch|update|publish|login|owner|yank)( |$)",
    "git": r"(^| )(clone|fetch|pull|remote)( |$)",
    "go": r"(^| )(get|install)( |$)",
    "npm": r"(^| )(install|i|add|publish|login|view|info|search|pack)( |$)",
    "pip": r"(^| )install( |$)|(^| )download( |$)",
    "pip3": r"(^| )install( |$)|(^| )download( |$)",
    "pnpm": r"(^| )(install|i|add|publish|login|view|info|search|pack)( |$)",
    "yarn": r"(^| )(install|add|publish|login|info|search|pack)( |$)",
}
HOST_INSPECTION_GUARDS = (
    "cat",
    "find",
    "grep",
    "head",
    "ls",
    "rg",
    "sed",
    "tail",
    "wc",
)
URL_FETCH_GUARDS = (
    "curl",
    "wget",
)
HOST_INSPECTION_PATTERNS = (
    "/" + "Users" + "/",
    "/home/",
    "/Documents/" + "ProgramBench",
    "ProgramBench",
    "pb-goal-runs",
)
PARENT_TRAVERSAL_PATTERNS = (
    " .. ",
    "../",
    "/..",
)
TOOL_CACHE_ENV = (
    "CARGO_HOME",
    "CARGO_NET_OFFLINE",
    "GOMODCACHE",
    "GONOSUMDB",
    "GOPATH",
    "GOPROXY",
    "GOSUMDB",
    "NPM_CONFIG_CACHE",
    "NPM_CONFIG_OFFLINE",
    "PIP_CACHE_DIR",
    "PIP_NO_INDEX",
)
LOCAL_TOOLS_OFFLINE_ENV = (
    "CARGO_NET_OFFLINE",
    "GOPROXY",
    "GOSUMDB",
    "NPM_CONFIG_OFFLINE",
    "PIP_NO_INDEX",
)


def image_name(instance_id: str) -> str:
    return f"programbench/{instance_id.replace('__', '_1776_')}"


def slug(instance_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", instance_id).strip("-")


def model_slug(model: str) -> str:
    return slug(model.replace(".", ""))


def run_name(
    instance_id: str,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    inference_mode: str = DEFAULT_INFERENCE_MODE,
) -> str:
    name = instance_id.split("__", 1)[1].split(".", 1)[0] if "__" in instance_id else slug(instance_id)
    prefix = "gpt55" if model == DEFAULT_MODEL else model_slug(model)
    effort = "" if model == DEFAULT_MODEL and reasoning_effort == DEFAULT_REASONING_EFFORT else f"-{reasoning_effort}"
    return f"{prefix}-goal{effort}-{MODE_RUN_SEGMENTS[inference_mode]}-{name}"


def render_prompt(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def chown_tree(path: Path, user: str) -> None:
    if not user:
        return
    user_info = pwd.getpwnam(user)
    for item in (path, *path.rglob("*")):
        os.chown(item, user_info.pw_uid, user_info.pw_gid)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command_output(command: list[str]) -> str:
    try:
        return (
            subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False).stdout
        ).strip()
    except FileNotFoundError:
        return ""


def write_guard_bin(
    guard_dir: Path,
    container_name: str,
    target_access: str,
    target_wrapper_command: str,
    allow_local_tools: bool,
) -> None:
    guard_dir.mkdir(parents=True, exist_ok=True)
    if target_access == "direct-docker":
        write_executable(
            guard_dir / "docker",
            f"""#!/usr/bin/env bash
set -euo pipefail
if [ "${{1:-}}" = "inspect" ] && [ "${{2:-}}" = {shlex.quote(container_name)} ]; then
  exec {shlex.quote(shutil.which("docker") or "docker")} "$@"
fi
if [ {str(allow_local_tools).lower()} = "true" ] && [ "${{1:-}}" = "cp" ]; then
  case "${{2:-}}" in
    {container_name}:/*)
      exec {shlex.quote(shutil.which("docker") or "docker")} "$@"
      ;;
  esac
fi
if [ {str(allow_local_tools).lower()} = "true" ] \\
  && [ "${{1:-}}" = "exec" ] \\
  && [ "${{2:-}}" = {shlex.quote(container_name)} ]; then
  exec {shlex.quote(shutil.which("docker") or "docker")} "$@"
fi
index=2
if [ "${{1:-}}" = "exec" ] && [ "${{2:-}}" = "-i" ]; then
  index=3
fi
if [ "$index" -eq 2 ]; then
  user_flag="${{2:-}}"
  user_name="${{3:-}}"
  target_container="${{4:-}}"
else
  user_flag="${{3:-}}"
  user_name="${{4:-}}"
  target_container="${{5:-}}"
fi
if [ "${{1:-}}" = "exec" ] \\
  && [ "$user_flag" = "-u" ] \\
  && [ "$user_name" = "agent" ] \\
  && [ "$target_container" = {shlex.quote(container_name)} ]; then
  exec {shlex.quote(shutil.which("docker") or "docker")} "$@"
fi
echo "blocked docker command. Use: docker exec [-i] -u agent {container_name} bash -lc '<command>'" >&2
exit 126
""",
        )
    else:
        write_executable(
            guard_dir / "docker",
            """#!/usr/bin/env bash
echo "blocked docker command. This run uses the configured target exec wrapper, not raw Docker." >&2
exit 126
""",
        )
    write_sudo_guard(guard_dir, container_name, target_access, target_wrapper_command)
    blocked_reason = (
        "host internet/source tooling" if allow_local_tools else "host internet/source/binary-analysis tooling"
    )
    for tool in BLOCKED_ALWAYS_TOOLS:
        if allow_local_tools and tool in {
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
        }:
            continue
        write_executable(
            guard_dir / tool,
            f"""#!/usr/bin/env bash
echo "blocked {tool}: ProgramBench no-internet runs forbid {blocked_reason}" >&2
exit 126
""",
        )
    for tool, pattern in SOURCE_ACQUISITION_GUARDS.items():
        real = shutil.which(tool)
        exec_line = (
            f'exec {shlex.quote(real)} "$@"' if real else f'echo "{tool} is not available on this host" >&2\nexit 127'
        )
        write_executable(
            guard_dir / tool,
            f"""#!/usr/bin/env bash
set -euo pipefail
args=" $* "
blocked_re={shlex.quote(pattern)}
if [[ "$args" =~ $blocked_re ]]; then
  echo "blocked {tool}: ProgramBench no-internet runs allow local builds, not source/package acquisition" >&2
  exit 126
fi
{exec_line}
""",
        )
    for tool in URL_FETCH_GUARDS:
        real = shutil.which(tool)
        exec_line = (
            f'exec {shlex.quote(real)} "$@"' if real else f'echo "{tool} is not available on this host" >&2\nexit 127'
        )
        write_executable(
            guard_dir / tool,
            f"""#!/usr/bin/env bash
set -euo pipefail
for arg in "$@"; do
  case "$arg" in
    http://localhost*|https://localhost*|http://127.0.0.1*|https://127.0.0.1*|http://0.0.0.0*|https://0.0.0.0*|http://[::1]*|https://[::1]*)
      ;;
    http://*|https://*)
      echo "blocked {tool}: ProgramBench no-internet runs allow loopback fetches, not external URL fetches" >&2
      exit 126
      ;;
  esac
done
{exec_line}
""",
        )
    for tool in HOST_INSPECTION_GUARDS:
        real = shutil.which(tool)
        exec_line = (
            f'exec {shlex.quote(real)} "$@"' if real else f'echo "{tool} is not available on this host" >&2\nexit 127'
        )
        checks = "\n".join(
            [
                f'if [[ "$args" == *{shlex.quote(pattern)}* ]]; then '
                f'echo "blocked {tool}: ProgramBench no-internet runs forbid host/evaluator path inspection" >&2; '
                "exit 126; fi"
                for pattern in (*HOST_INSPECTION_PATTERNS, *PARENT_TRAVERSAL_PATTERNS)
            ]
        )
        write_executable(
            guard_dir / tool,
            f"""#!/usr/bin/env bash
set -euo pipefail
args=" $* "
{checks}
{exec_line}
""",
        )


def write_sudo_guard(guard_dir: Path, container_name: str, target_access: str, target_wrapper_command: str) -> None:
    real_sudo = shutil.which("sudo") or "sudo"
    wrapper_parts = shlex.split(target_wrapper_command)
    if target_access == "wrapper" and wrapper_parts[:1] == ["sudo"]:
        allowed = wrapper_parts[1:] + [container_name]
        checks = "\n".join(
            f'[[ "${{{index}:-}}" == {shlex.quote(value)} ]] || allowed=0'
            for index, value in enumerate(allowed, start=1)
        )
        write_executable(
            guard_dir / "sudo",
            f"""#!/usr/bin/env bash
set -euo pipefail
allowed=1
[[ "$#" -ge {len(allowed)} ]] || allowed=0
{checks}
if [[ "$allowed" == 1 ]]; then
  exec {shlex.quote(real_sudo)} "$@"
fi
echo "blocked sudo command. Use only: {shlex.quote(target_wrapper_command)} {container_name} <command> [args...]" >&2
exit 126
""",
        )
        return
    write_executable(
        guard_dir / "sudo",
        """#!/usr/bin/env bash
echo "blocked sudo command in ProgramBench no-internet run" >&2
exit 126
""",
    )


def tool_cache_exports(cache_dir: Path) -> str:
    values = {
        "CARGO_HOME": cache_dir / "cargo",
        "CARGO_NET_OFFLINE": "true",
        "GOMODCACHE": cache_dir / "go" / "pkg" / "mod",
        "GONOSUMDB": "*",
        "GOPATH": cache_dir / "go",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "NPM_CONFIG_CACHE": cache_dir / "npm",
        "NPM_CONFIG_OFFLINE": "true",
        "PIP_CACHE_DIR": cache_dir / "pip",
        "PIP_NO_INDEX": "1",
    }
    return " ".join(f"{key}={shlex.quote(str(value))}" for key, value in values.items())


def local_tools_offline_exports() -> str:
    values = {
        "CARGO_NET_OFFLINE": "true",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "NPM_CONFIG_OFFLINE": "true",
        "PIP_NO_INDEX": "1",
    }
    return " ".join(f"{key}={shlex.quote(value)}" for key, value in values.items())


def write_target_shim_source(path: Path, container_name: str, target_wrapper_command: str) -> None:
    wrapper = [*shlex.split(target_wrapper_command), container_name]
    prefix = ", ".join(json.dumps(value) for value in wrapper)
    command = json.dumps('cd /workspace/solution && exec /workspace/executable "$@"')
    path.write_text(
        f"""#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv) {{
    char *prefix[] = {{{prefix}, "bash", "-lc", {command}, "pb-target"}};
    int prefix_len = sizeof(prefix) / sizeof(prefix[0]);
    char **args = calloc(prefix_len + argc, sizeof(char *));
    if (!args) return 127;
    for (int i = 0; i < prefix_len; i++) args[i] = prefix[i];
    for (int i = 1; i < argc; i++) args[prefix_len + i - 1] = argv[i];
    execvp(args[0], args);
    return 127;
}}
"""
    )


def proxy_exports(enabled: bool) -> str:
    if not enabled:
        return ""
    proxy_url = os.environ.get("PB_CODEX_PROXY_URL", "http://127.0.0.1:18080")
    no_proxy = os.environ.get("PB_CODEX_NO_PROXY", "localhost,127.0.0.1")
    return " ".join(
        f"{key}={shlex.quote(value)}"
        for key, value in {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "ALL_PROXY": proxy_url,
            "NO_PROXY": no_proxy,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "all_proxy": proxy_url,
            "no_proxy": no_proxy,
        }.items()
    )


def prepare(args: argparse.Namespace) -> None:
    codex_user = args.codex_user.strip()
    if args.inference_mode in NO_INTERNET_MODES and not args.strict_egress:
        raise SystemExit(f"{args.inference_mode} mode requires --strict-egress")
    if args.strict_egress and not args.model.startswith("gpt-"):
        raise SystemExit("strict OpenAI egress is only supported for OpenAI/Codex model runs")
    if args.strict_egress and platform.system() != "Linux":
        raise SystemExit("strict egress is only implemented for Linux hosts")
    if args.strict_egress and os.geteuid() == 0 and not codex_user:
        raise SystemExit("strict egress under root requires --codex-user so only the Codex UID is firewalled")
    if args.strict_egress and codex_user == "root":
        raise SystemExit("strict egress must run Codex as a dedicated non-root user")
    if codex_user:
        pwd.getpwnam(codex_user)
    root = Path(args.run_root).expanduser().resolve()
    prepared_run_name = args.run_name or run_name(
        args.instance_id,
        args.model,
        args.reasoning_effort,
        args.inference_mode,
    )
    instance_dir = root / prepared_run_name / args.instance_id
    solution_dir = instance_dir / "solution"
    guard_dir = instance_dir / "guard-bin"
    helper_dir = instance_dir / "helper-bin"
    cache_dir = instance_dir / "tool-caches"
    mini_swe_compat_mode = args.inference_mode == MINI_SWE_COMPAT_MODE
    paper_prompt_mode = args.inference_mode == PAPER_PROMPT_MODE
    goal_contract_paper_mode = args.inference_mode == GOAL_CONTRACT_PAPER_MODE
    mini_swe_style_execution = paper_prompt_mode or goal_contract_paper_mode
    local_tools_mode = False
    strict_no_internet_mode = (
        mini_swe_compat_mode
        or paper_prompt_mode
        or goal_contract_paper_mode
    )
    tool_env = list(TOOL_CACHE_ENV)
    container_name = f"pb-goal-{slug(prepared_run_name)}-{slug(args.instance_id)}"
    session_name = f"pb-goal-{slug(prepared_run_name)}-{slug(args.instance_id)}"
    image = image_name(args.instance_id)
    target_command = (
        "./executable <args>"
        if mini_swe_style_execution
        else (
            f"docker exec -u agent {container_name} bash -lc '<command>'"
            if args.target_access == "direct-docker"
            else f"{args.target_wrapper_command} {container_name} bash -lc '<command>'"
        )
    )
    if goal_contract_paper_mode:
        objective = (
            f"Complete ProgramBench instance {args.instance_id} by producing the strongest defensible original "
            "replacement CLI, verified by bundled documentation, allowed normal user-interface target executions, "
            "local replacement executions, compile.sh, package-submission, and workspace probe notes, while "
            "preserving no-internet, no source/package lookup, no target binary reading or analysis, no tracing, "
            "no wrapping, no evaluator/test access, and workspace-only boundaries. Between iterations, compare "
            "target-vs-local observable behavior across args, flags, subcommands, help/version output, config/env "
            "behavior, stdin, stdout, stderr, exit codes, file inputs/outputs, filesystem effects, and TUI behavior "
            "where applicable; fix the highest-value missing behavior class and re-check it. If blocked or no valid "
            "path remains, finish with the best implementation plus a short note recording evidence, blockers, "
            "unresolved gaps, and what would unlock progress."
        )
    elif paper_prompt_mode:
        objective = (
            f"Complete ProgramBench instance {args.instance_id} by reimplementing the target CLI from bundled docs "
            "and normal user-interface observations only, until compile.sh builds ./executable and "
            "package-submission succeeds."
        )
    elif mini_swe_compat_mode:
        objective = (
            f"Complete ProgramBench instance {args.instance_id} by reimplementing the target CLI from the provided "
            "binary behavior and documentation, without internet access, until compile.sh builds ./executable and "
            "package-submission succeeds. Do not inspect parent directories or files outside the solution directory."
        )
    else:
        objective = (
            f"Complete ProgramBench instance {args.instance_id} in the target container by reimplementing the "
            "target CLI from black-box behavior only, without stopping until solution/compile.sh builds ./executable, "
            "package-submission succeeds, and .goal/BEHAVIOR_AUDIT.md documents adversarial target-vs-local probe "
            "coverage, comparisons, mismatches fixed, known gaps, and stopping rationale. Treat hidden tests as "
            "adversarial edge-case tests. Keep iterating with generated/fuzzed probes until no documented or "
            "discovered behavior class remains unexplored. Do not mark the goal complete just because packaging works "
            "or representative probes pass. "
            "Do not inspect parent directories or files outside the solution directory."
        )

    solution_dir.mkdir(parents=True, exist_ok=True)
    helper_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    write_guard_bin(guard_dir, container_name, args.target_access, args.target_wrapper_command, local_tools_mode)
    if mini_swe_compat_mode or paper_prompt_mode or goal_contract_paper_mode:
        agent_rules = (
            "Do not use internet, package registries, public source, external docs, ProgramBench tests, or "
            "the ProgramBench evaluator repository. Do not inspect files outside this solution directory or run "
            "commands against parent/sibling directories. Probe the target executable only through normal "
            f"CLI/stdin/stdout/stderr/filesystem behavior using {target_command}. "
            "Do not read, decompile, disassemble, trace, or wrap the target binary. Implement a complete replacement "
            "codebase here. compile.sh must produce ./executable. Run package-submission before finishing.\n"
        )
    else:
        raise SystemExit(f"unsupported inference mode: {args.inference_mode}")
    (solution_dir / "AGENT_RULES.md").write_text(agent_rules)
    prompt_template = (
        args.prompt_template
        or {
            MINI_SWE_COMPAT_MODE: MINI_SWE_COMPAT_PROMPT_TEMPLATE,
            PAPER_PROMPT_MODE: PAPER_PROMPT_TEMPLATE,
            GOAL_CONTRACT_PAPER_MODE: GOAL_CONTRACT_PAPER_PROMPT_TEMPLATE,
        }[args.inference_mode]
    )
    prompt_template_path = Path(prompt_template).expanduser()
    (instance_dir / "GOAL_PROMPT.md").write_text(
        render_prompt(
            prompt_template_path.read_text(),
            {
                "instance_id": args.instance_id,
                "run_name": prepared_run_name,
                "run_version": args.run_version,
                "image": image,
                "container_name": container_name,
                "solution_dir": ".",
                "target_command": target_command,
                "package_command": "package-submission",
            },
        )
    )
    (instance_dir / "GOAL_OBJECTIVE.txt").write_text(objective + "\n")
    (instance_dir / "CODEX_INITIAL_PROMPT.md").write_text(
        "/goal " + (instance_dir / "GOAL_PROMPT.md").read_text()
        if paper_prompt_mode or goal_contract_paper_mode
        else "/goal " + objective + "\n\n" + (instance_dir / "GOAL_PROMPT.md").read_text()
    )
    (instance_dir / "run.json").write_text(
        json.dumps(
            {
                "instance_id": args.instance_id,
                "run_name": prepared_run_name,
                "run_version": args.run_version,
                "image": image,
                "container_name": container_name,
                "session_name": session_name,
                "solution_dir": str(solution_dir),
                "guard_bin_dir": str(guard_dir),
                "tool_cache_dir": str(cache_dir),
                "tool_cache_env": tool_env,
                "target_access": args.target_access,
                "strict_egress": args.strict_egress,
                "codex_user": codex_user,
                "target_wrapper_command": args.target_wrapper_command,
                "target_command": target_command,
                "mini_swe_style_execution": mini_swe_style_execution,
                "prompt_template": str(prompt_template_path),
                "prompt_template_sha256": file_sha256(prompt_template_path),
                "prompt_rendered_sha256": file_sha256(instance_dir / "GOAL_PROMPT.md"),
                "docker_cpus": args.docker_cpus,
                "docker_memory": args.docker_memory,
                "inference_mode": args.inference_mode,
                "paper_mode": paper_prompt_mode,
                "paper_compliant": (
                    paper_prompt_mode
                    and args.target_access == "wrapper"
                    and platform.system() == "Linux"
                    and platform.machine() in {"x86_64", "AMD64"}
                    and str(args.docker_cpus) == "20"
                    and args.docker_memory == "60g"
                ),
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
                "codex_version": command_output(["codex", "--version"]),
                "codex_launch_mode": "interactive_goal_then_prompt",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "host_machine": platform.machine(),
                "host_system": platform.system(),
            },
            indent=2,
        )
        + "\n"
    )
    if mini_swe_style_execution:
        write_target_shim_source(instance_dir / "target-shim.c", container_name, args.target_wrapper_command)

    network_check = (
        f'test "$(docker inspect {shlex.quote(container_name)} --format \'{{{{.HostConfig.NetworkMode}}}}\')" = "none"'
    )
    network_arg = "--network none"
    write_executable(
        instance_dir / "start-target.sh",
        f"""#!/usr/bin/env bash
set -euo pipefail
docker rm -f {shlex.quote(container_name)} >/dev/null 2>&1 || true
docker pull --platform linux/amd64 {shlex.quote(image)}:task_cleanroom
docker run -d --platform linux/amd64 \\
  --name {shlex.quote(container_name)} \\
  {network_arg} \\
  --cpus {shlex.quote(str(args.docker_cpus))} \\
  --memory {shlex.quote(args.docker_memory)} \\
  -v {shlex.quote(str(solution_dir))}:/workspace/solution \\
  {shlex.quote(image)}:task_cleanroom \\
  sleep infinity
docker exec -u agent {shlex.quote(container_name)} bash -lc 'pwd; find /workspace -maxdepth 2 -type f | sort | head -80'
if [ {str(mini_swe_style_execution).lower()} = true ]; then
  docker exec -u agent {shlex.quote(container_name)} bash -lc \\
    'cd /workspace && tar --exclude=./executable --exclude=./solution -cf - .' \\
    | tar -C {shlex.quote(str(solution_dir))} -xf -
  cc {shlex.quote(str(instance_dir / "target-shim.c"))} -o {shlex.quote(str(solution_dir / "executable"))}
  chmod 111 {shlex.quote(str(solution_dir / "executable"))}
  if [ -e /workspace ] && [ ! -L /workspace ]; then
    echo "host /workspace exists and is not a symlink" >&2
    exit 1
  fi
  ln -sfn {shlex.quote(str(solution_dir))} /workspace
fi
""",
    )
    write_executable(
        instance_dir / "check-compliance.sh",
        f"""#!/usr/bin/env bash
set -euo pipefail
docker inspect {shlex.quote(container_name)} \\
  --format 'network={{{{.HostConfig.NetworkMode}}}} image={{{{.Config.Image}}}} status={{{{.State.Status}}}}'
{network_check}
docker exec -u agent {shlex.quote(container_name)} bash -lc '
  set -e
  id
  stat -c "%A %a %U %G %n" /workspace/executable
  test -x /workspace/executable
  if head -c 4 /workspace/executable >/tmp/pb-readtest 2>/dev/null; then
    echo "FAIL executable is readable"
    exit 1
  fi
  if strings /workspace/executable >/tmp/pb-stringtest 2>/dev/null; then
    echo "FAIL strings can read executable"
    exit 1
  fi
  if objdump -h /workspace/executable >/tmp/pb-objdumptest 2>/dev/null; then
    echo "FAIL objdump can read executable"
    exit 1
  fi
  echo "ok: agent can execute but cannot read/decompile executable"
'
(
  cd {shlex.quote(str(solution_dir))}
  export PATH={shlex.quote(str(guard_dir))}:$PATH
  command -v package-submission >/dev/null
  if rg --files -uu .. >/tmp/pb-parent-guard.out 2>/tmp/pb-parent-guard.err; then
    echo "FAIL guard allowed parent-directory inspection" >&2
    exit 1
  fi
  grep -q "blocked rg" /tmp/pb-parent-guard.err
  echo "ok: guard blocks parent-directory inspection and exposes package-submission"
)
""",
    )
    base_codex_env = (
        f"PATH={shlex.quote(str(guard_dir))}:$PATH GIT_CEILING_DIRECTORIES={shlex.quote(str(instance_dir))} "
        f"{tool_cache_exports(cache_dir)}"
        if strict_no_internet_mode
        else (
            f"PATH={shlex.quote(str(guard_dir))}:$PATH GIT_CEILING_DIRECTORIES={shlex.quote(str(instance_dir))} "
            f"{local_tools_offline_exports()}"
        )
    )
    codex_env = " ".join(value for value in (base_codex_env, proxy_exports(args.strict_egress)) if value)
    codex_command = f"sudo -H -u {shlex.quote(codex_user)} codex" if codex_user else "codex"
    tmux_command = f"sudo -H -u {shlex.quote(codex_user)} tmux" if codex_user else "tmux"
    transcript_log = shlex.quote(str(instance_dir / "tmux-transcript.log"))
    codex_config_setup = (
        f"""
CODEX_USER={shlex.quote(codex_user)}
CODEX_USER_HOME="$(getent passwd "$CODEX_USER" | cut -d: -f6)"
CODEX_CONFIG="$CODEX_USER_HOME/.codex/config.toml"
mkdir -p "$(dirname "$CODEX_CONFIG")"
touch "$CODEX_CONFIG"
if [ "$(id -u)" -eq 0 ]; then
  chown -R "$CODEX_USER:$CODEX_USER" "$(dirname "$CODEX_CONFIG")"
fi
"""
        if codex_user
        else 'CODEX_CONFIG="${CODEX_HOME:-$HOME/.codex}/config.toml"\nmkdir -p "$(dirname "$CODEX_CONFIG")"\n'
    )
    write_executable(
        instance_dir / "start-codex-goal.sh",
        f"""#!/usr/bin/env bash
set -euo pipefail
CODEX_BYPASS_FLAG="--yolo"
if ! {codex_command} --yolo --version >/dev/null 2>&1; then
  CODEX_BYPASS_FLAG="--dangerously-bypass-approvals-and-sandbox"
fi
{codex_config_setup}
TRUST_KEY="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1]))' {shlex.quote(str(solution_dir))})"
if ! grep -Fqx "[projects.$TRUST_KEY]" "$CODEX_CONFIG" 2>/dev/null; then
  {{
    printf '\\n[projects.%s]\\n' "$TRUST_KEY"
    printf 'trust_level = "trusted"\\n'
  }} >> "$CODEX_CONFIG"
fi
if [ -n "{codex_user}" ] && [ "$(id -u)" -eq 0 ]; then
  chown "$CODEX_USER:$CODEX_USER" "$CODEX_CONFIG"
fi
{tmux_command} kill-session -t {shlex.quote(session_name)} >/dev/null 2>&1 || true
{tmux_command} new-session -d -s {shlex.quote(session_name)} -c {shlex.quote(str(solution_dir))} \\
  "{codex_env} codex --enable goals --disable plugins --disable apps -m {shlex.quote(args.model)} \\
  -c model_reasoning_effort={shlex.quote(args.reasoning_effort)} \\
  -c trust_level=trusted \\
  -C {shlex.quote(str(solution_dir))} $CODEX_BYPASS_FLAG --no-alt-screen"
{tmux_command} pipe-pane -o -t {shlex.quote(session_name)} 'cat >> {transcript_log}'
sleep 4
GOAL_OBJECTIVE="$(cat {shlex.quote(str(instance_dir / "GOAL_OBJECTIVE.txt"))})"
{tmux_command} send-keys -t {shlex.quote(session_name)} "/goal $GOAL_OBJECTIVE" Enter
sleep 2
{tmux_command} load-buffer {shlex.quote(str(instance_dir / "GOAL_PROMPT.md"))}
{tmux_command} paste-buffer -t {shlex.quote(session_name)}
sleep 2
{tmux_command} send-keys -t {shlex.quote(session_name)} Enter
echo "Attached session: {tmux_command} attach -t {session_name}"
""",
    )
    write_executable(
        instance_dir / "package-submission.sh",
        f"""#!/usr/bin/env bash
set -euo pipefail
test -f {shlex.quote(str(solution_dir / "compile.sh"))} || {{
  echo "missing solution/compile.sh" >&2
  exit 1
}}
COPYFILE_DISABLE=1 tar -C {shlex.quote(str(solution_dir))} \\
  --exclude './AGENT_RULES.md' \\
  --exclude './.goal' \\
  --exclude './probes' \\
  --exclude './.DS_Store' \\
  --exclude './._*' \\
  -czf {shlex.quote(str(instance_dir / "submission.tar.gz"))} .
/bin/ls -lh {shlex.quote(str(instance_dir / "submission.tar.gz"))}
""",
    )
    write_executable(
        guard_dir / "package-submission",
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *".."* ]]; then
  echo "blocked package-submission: parent traversal is not allowed" >&2
  exit 126
fi
exec {shlex.quote(str(instance_dir / "package-submission.sh"))} "$@"
""",
    )
    write_executable(
        helper_dir / "package-submission",
        f"""#!/usr/bin/env bash
set -euo pipefail
exec {shlex.quote(str(instance_dir / "package-submission.sh"))} "$@"
""",
    )
    chown_tree(instance_dir, codex_user)
    write_executable(
        instance_dir / "eval-submission.sh",
        f"""#!/usr/bin/env bash
set -euo pipefail
programbench_repo="${{1:?usage: $0 /path/to/ProgramBench}}"
cd "$programbench_repo"
uv run programbench eval {shlex.quote(str(instance_dir.parent))} \\
  --filter {shlex.quote(args.instance_id)} \\
  --workers 1 \\
  --branch-workers 1 \\
  --docker-cpus {shlex.quote(str(args.docker_cpus))}
uv run programbench info {shlex.quote(str(instance_dir.parent))}
""",
    )

    print(instance_dir)
    if platform.machine() not in {"x86_64", "AMD64"}:
        print("warning: this host is not amd64; use a Linux amd64 host for real runs")


def prepare_batch(args: argparse.Namespace) -> None:
    for line in Path(args.target_file).expanduser().read_text().splitlines():
        instance_id = line.split("#", 1)[0].strip()
        if instance_id:
            prepare(
                argparse.Namespace(
                    instance_id=instance_id,
                    run_root=args.run_root,
                    run_name="",
                    run_version=args.run_version,
                    prompt_template=args.prompt_template,
                    target_access=args.target_access,
                    target_wrapper_command=args.target_wrapper_command,
                    docker_cpus=args.docker_cpus,
                    docker_memory=args.docker_memory,
                    inference_mode=args.inference_mode,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                    strict_egress=args.strict_egress,
                    codex_user=args.codex_user,
                )
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare Codex /goal ProgramBench runs")
    subparsers = parser.add_subparsers(required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("instance_id")
    prepare_parser.add_argument("--run-root", default=str(DEFAULT_ROOT))
    prepare_parser.add_argument("--run-name", default="")
    prepare_parser.add_argument("--run-version", default="")
    prepare_parser.add_argument("--docker-cpus", type=int, default=20)
    prepare_parser.add_argument("--docker-memory", default="60g")
    prepare_parser.add_argument(
        "--inference-mode",
        choices=INFERENCE_MODES,
        default=DEFAULT_INFERENCE_MODE,
    )
    prepare_parser.add_argument(
        "--target-access",
        choices=["direct-docker", "wrapper"],
        default="direct-docker",
        help="Use guarded raw Docker for local smoke runs, or a narrow external target exec wrapper for strict runs.",
    )
    prepare_parser.add_argument("--target-wrapper-command", default="sudo -n /usr/local/bin/pb-target-exec")
    prepare_parser.add_argument("--model", default=DEFAULT_MODEL)
    prepare_parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    prepare_parser.add_argument("--strict-egress", action="store_true")
    prepare_parser.add_argument(
        "--codex-user",
        default="",
        help="Run Codex/tmux as this non-root user while the coordinator keeps Docker/eval access.",
    )
    prepare_parser.add_argument(
        "--prompt-template",
        default="",
        help="Prompt template to render. Use this to pass an official ProgramBench prompt unchanged when available.",
    )
    prepare_parser.set_defaults(func=prepare)
    batch_parser = subparsers.add_parser("prepare-batch")
    batch_parser.add_argument("target_file")
    batch_parser.add_argument("--run-root", default=str(DEFAULT_ROOT))
    batch_parser.add_argument("--run-version", default="")
    batch_parser.add_argument("--docker-cpus", type=int, default=20)
    batch_parser.add_argument("--docker-memory", default="60g")
    batch_parser.add_argument(
        "--inference-mode",
        choices=INFERENCE_MODES,
        default=DEFAULT_INFERENCE_MODE,
    )
    batch_parser.add_argument(
        "--target-access",
        choices=["direct-docker", "wrapper"],
        default="direct-docker",
        help="Use guarded raw Docker for local smoke runs, or a narrow external target exec wrapper for strict runs.",
    )
    batch_parser.add_argument("--target-wrapper-command", default="sudo -n /usr/local/bin/pb-target-exec")
    batch_parser.add_argument("--model", default=DEFAULT_MODEL)
    batch_parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    batch_parser.add_argument("--strict-egress", action="store_true")
    batch_parser.add_argument(
        "--codex-user",
        default="",
        help="Run Codex/tmux as this non-root user while the coordinator keeps Docker/eval access.",
    )
    batch_parser.add_argument(
        "--prompt-template",
        default="",
        help="Prompt template to render. Use this to pass an official ProgramBench prompt unchanged when available.",
    )
    batch_parser.set_defaults(func=prepare_batch)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
