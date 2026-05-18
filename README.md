# GoalBench

Codex `/goal` runner for ProgramBench tasks.

GoalBench runs Codex CLI goal mode against ProgramBench cleanroom task images,
packages each generated replacement as `submission.tar.gz`, evaluates with
ProgramBench, and publishes a static report. It is a Codex `/goal` scaffold
measurement, not the official ProgramBench mini-SWE-agent baseline.

## What This Measures

ProgramBench asks an agent to rebuild a CLI program from only a compiled
executable and bundled documentation. GoalBench keeps that shape, but swaps the
agent scaffold for Codex CLI `/goal`.

Current reportable tracks:

| Track | Config | Prompt | Compliance label |
| --- | --- | --- | --- |
| Verbatim paper prompt | `configs/cpx62-paperprompt-xhigh.json` | ProgramBench paper prompt with `/goal ` prepended and only harness context appended | Paper-prompt no internet |
| Mini-SWE-compatible | `configs/full-miniswecompat-xhigh.json` | Short mini-SWE-style prompt with `/goal ` prepended | Mini-SWE-compatible no internet |
| Stricter GoalBench | `configs/full-nointernet-xhigh.json` | GoalBench audit-heavy prompt with `/goal ` prepended | No internet |

All reportable no-internet tracks use strict host egress, wrapper-only target
access, and post-run audits.

## System Layout

```text
                         GitHub / Pages
                              ^
                              | publish only from coordinator
                              |
local laptop  --ssh-->  coordinator VM  -----------------------------+
                         |                                           |
                         | runs Codex /goal sessions                 |
                         | owns merged report                        |
                         |                                           |
                         +--> target Docker containers               |
                         |    one clean ProgramBench image per task  |
                         |                                           |
                         +--> ProgramBench evaluator                 |
                         |    package -> audit -> eval -> summarize  |
                         |
                         +--> optional eval/inference workers
                              same repo commit, separate shards
                              no publishing
```

Codex runs on the VM host, not inside the target container. Docker is used for
the reference target during inference and by ProgramBench during evaluation.

## Run Flow

```text
target_sets/*.txt
      |
      v
run-batch.py watch
      |
      +-- prepare per-task run root
      |     CODEX_INITIAL_PROMPT.md
      |     run.json
      |     start-target.sh
      |     start-codex-goal.sh
      |
      +-- start target container
      |
      +-- start tmux Codex session
      |     codex --enable goals ... "$(cat CODEX_INITIAL_PROMPT.md)"
      |
      +-- watch transcript
      |     running -> goal_done
      |     failed before goal_done -> fresh retry only
      |
      v
run-batch.py finalize
      |
      +-- hard gates
      |     prompt starts with /goal
      |     transcript shows /goal
      |     run.json has model/reasoning/mode/strict egress
      |
      +-- package-submission
      +-- audit-run.py
      +-- ProgramBench eval
      +-- summarize-results.py
      |
      v
static report
```

## How `/goal` Is Applied

Every benchmark prompt is rendered to `GOAL_PROMPT.md`. The actual prompt sent
to Codex is written to `CODEX_INITIAL_PROMPT.md` as:

```text
/goal <rendered ProgramBench/GoalBench prompt>
```

`start-codex-goal.sh` then loads that file and invokes Codex:

```bash
CODEX_INITIAL_PROMPT="$(cat CODEX_INITIAL_PROMPT.md)"
codex --enable goals --disable plugins --disable apps \
  -m gpt-5.5 \
  -c model_reasoning_effort=xhigh \
  -c trust_level=trusted \
  -C solution \
  --yolo --no-alt-screen \
  "$CODEX_INITIAL_PROMPT"
```

For the paper-prompt track, the bytes immediately after `/goal ` are the copied
ProgramBench paper prompt from Appendix 8.2, followed by a small harness context
block with instance id, target command, package command, and solution directory.

Before publishing, hard gates check both the prompt file and transcript:

```text
CODEX_INITIAL_PROMPT.md begins with /goal
tmux transcript contains /goal
run.json records model, reasoning effort, inference mode, and strict egress
```

## No-Internet Controls

Reportable no-internet runs use layered controls. The prompt alone is not the
security boundary.

```text
Codex process
  user: codex-runner
  |
  +-- UID-scoped iptables
  |     only loopback is allowed for codex-runner
  |
  +-- local OpenAI allowlist proxy
  |     Codex talks to http://127.0.0.1:18080
  |     proxy makes the OpenAI/Codex network calls
  |
  +-- guard-bin PATH
  |     blocks source lookup, package installs, binary analysis tools,
  |     broad host traversal, and direct docker access
  |
  +-- target wrapper
        sudo -n /usr/local/bin/pb-target-exec <container> <command>
        only allows docker exec into pb-goal-* target containers as agent
```

The coordinator/root account still needs normal network for setup, Docker pulls,
Git operations, and publishing. The restricted Codex task user does not.

Required host checks before a serious run:

```bash
scripts/doctor.sh configs/cpx62-paperprompt-xhigh.json
sudo scripts/linux-openai-egress-guard.sh status codex-runner
sudo -H -u codex-runner sudo -n /usr/local/bin/pb-target-exec __pb-wrapper-check true
```

The wrapper check should fail with `refusing non ProgramBench target container`,
not `command not found` and not `sudo: a password is required`.

## Setup

Use Linux `amd64` for serious runs. ProgramBench task images are published for
`linux/amd64`; Mac/ARM is smoke-test only.

```bash
git clone git@github.com:Muhtasham/goalbench.git
cd goalbench
scripts/bootstrap-linux-vm.sh --codex-user codex-runner
codex login
scripts/doctor.sh configs/cpx62-paperprompt-xhigh.json
```

Bootstrap installs Docker, `uv`, `tmux`, Codex CLI if missing, a sibling
`../ProgramBench` checkout, Codex goal/fast defaults, and the target wrapper
used by no-internet runs.

## Single-VM Sweep

Start inference in a named tmux session so SSH disconnects do not stop the run:

```bash
RUN_VERSION="$(date -u +%Y%m%dT%H%M%SZ)"
tmux new-session -d -s "goalbench-watch-$RUN_VERSION" \
  "cd '$PWD' && uv run python scripts/run-config.py watch configs/cpx62-paperprompt-xhigh.json --run-version '$RUN_VERSION'"
```

Start a second tmux session to finalize completed rows sequentially:

```bash
tmux new-session -d -s "goalbench-finalize-$RUN_VERSION" \
  "cd '$PWD' && while true; do uv run python scripts/run-config.py finalize configs/cpx62-paperprompt-xhigh.json --run-version '$RUN_VERSION' --programbench-repo ../ProgramBench --allow-partial --limit 1; sleep 60; done"
```

`watch` can run up to `max_parallel` Codex sessions. `finalize --limit 1`
packages, audits, and evaluates one ready task per call, so a simple loop can
keep evaluation moving without running multiple ProgramBench evals on the same
VM.

## Multi-VM 200-Task Sweep

For a 5-way run, split the 200 task list into five 40-task shard files and use
one batch name per shard:

```text
goalbench-vm      shard 0/5  cpx62-paperprompt-xhigh-shard-0-of-5
goalbench-eval-1  shard 1/5  cpx62-paperprompt-xhigh-shard-1-of-5
goalbench-eval-2  shard 2/5  cpx62-paperprompt-xhigh-shard-2-of-5
goalbench-eval-3  shard 3/5  cpx62-paperprompt-xhigh-shard-3-of-5
goalbench-eval-4  shard 4/5  cpx62-paperprompt-xhigh-shard-4-of-5
```

Each VM runs two tmux supervisors:

```text
pb-paperprompt-xhigh-shard-N-<version>
  inference watcher, max 10 Codex /goal sessions

pb-paperprompt-xhigh-finalize-shard-N-<version>
  sequential package -> audit -> ProgramBench eval loop
```

Keep every VM on the same Git commit, Codex version, config, and `RUN_VERSION`.
Only the coordinator should merge artifacts and publish the static site.

## Retry Policy

GoalBench does not paste continuation prompts into failed Codex sessions.

Retryable:

- `session_failed_before_goal_done`: tmux/Codex ended before `/goal` completed
  and no valid submission was produced. Retry starts a fresh solution directory
  and records attempt metadata.

Not retryable:

- valid low-scoring submissions
- audit violations
- evaluator results
- normal no-submission exits unless explicitly reported as a fresh attempt

Command:

```bash
uv run python scripts/run-config.py retry \
  configs/cpx62-paperprompt-xhigh.json \
  --run-version "$RUN_VERSION" \
  --failed \
  --max-attempts 2
```

## Modes

| Mode | Meaning |
| --- | --- |
| `paper-prompt-nointernet` | Verbatim ProgramBench paper prompt with `/goal ` prepended, strict egress, wrapper-only target access. |
| `mini-swe-compatible-nointernet` | Shorter parity prompt with the same no-internet enforcement. |
| `no-internet` | Stricter GoalBench prompt that also asks for an explicit behavior audit. |
| `no-internet-local-tools` | Non-comparable ablation: internet/source/package lookup blocked, but local binary-analysis/tracing tools allowed. |

## Reporting

The static report mirrors ProgramBench's headline shape:

- resolved rate
- almost-resolved rate at `score >= 0.95`
- average behavioral pass rate
- estimated cost
- Codex calls
- wall-clock time
- per-task detail pages

Cost is estimated from local Codex token logs and a refreshed OpenAI pricing
snapshot. It is not authoritative billing.

Build locally:

```bash
uv run python scripts/build-report.py --output-dir docs
uv run python scripts/privacy-scan.py
```

Public output includes sanitized aggregate rows and public evidence summaries.
Raw Codex logs and submission tarballs stay local by default.

## Useful Commands

```bash
scripts/doctor.sh configs/cpx62-paperprompt-xhigh.json
uv run python scripts/run-config.py status configs/cpx62-paperprompt-xhigh.json
uv run python scripts/run-config.py finalize configs/cpx62-paperprompt-xhigh.json --programbench-repo ../ProgramBench --allow-partial --limit 1
uv run python scripts/validate-run-gates.py local_state/batches/<batch>/<version>/results.csv
scripts/backup-run-root.sh --batch-name <batch> --run-version <version>
uv run ruff check .
uv run ty check
uv run pre-commit run --all-files
```

## Links

- [Public report](https://muhtasham.github.io/goalbench/)
- [Detailed runbook](docs/runbook.md)
- [ProgramBench](https://programbench.com/)
