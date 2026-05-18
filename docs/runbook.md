# Detailed Runbook

This is the long-form operator runbook for GoalBench. Start with the
root `README.md` if you only need the default path.

Small harness for running Codex GPT-5.5 `/goal` against ProgramBench tasks.

This is a Codex `/goal` scaffold, not the official mini-SWE-agent baseline
scaffold. It uses ProgramBench's Docker task images and evaluation code, but the
inference loop is Codex CLI goal mode. Report results as Codex `/goal` results,
not as mini-SWE-agent results.

The harness keeps the solving workspace separate from the ProgramBench evaluator
repo. It gives Codex a clean writable solution directory and produces the
`submission.tar.gz` layout that `programbench eval` expects. GoalBench currently
keeps three public tracks: `mini-swe-compatible-nointernet` for the already
published result, `paper-prompt-nointernet` for the ProgramBench paper prompt
with `/goal`, and `paper-prompt-goal-contract-nointernet` for the same paper
prompt plus an explicit Codex Goal contract. The paper-prompt tracks use the
mini-SWE-style task workspace scaffold.

ProgramBench is a free-form reimplementation benchmark. The agent should choose
the language, architecture, source layout, abstractions, and build script from
black-box observations of the executable plus documentation already present in
the target container. It should not receive method signatures, skeletons,
product requirements, hidden hints, or task-specific harness tuning.

If ProgramBench publishes the exact mini-SWE-agent baseline prompt, keep that
as a separate immutable template and do not mix it with older GoalBench rows.
Public GoalBench rows should stay labeled as Codex `/goal` scaffold results.

## Requirements

- Linux `amd64` host for real runs.
- `uv`.
- Docker.
- Codex CLI with `/goal` support.
- `tmux`.
- A separate ProgramBench checkout only for evaluation.

For a fresh clone of this repo, bootstrap the ProgramBench evaluator as a
sibling checkout:

```bash
scripts/bootstrap-programbench.sh
scripts/install-target-wrapper.sh
```

The bootstrap script clones or updates `../ProgramBench` and runs
`uv sync --project` there. The wrapper installer grants the current user
passwordless sudo for `/usr/local/bin/pb-target-exec` only, so Codex can probe
target containers without direct Docker socket access. `scripts/run-sweep.sh`
auto-detects the sibling ProgramBench checkout. If you keep ProgramBench
somewhere else, set `PROGRAMBENCH_REPO=/path/to/ProgramBench` or pass
`--programbench-repo /path/to/ProgramBench`.

The ProgramBench images are published for `linux/amd64`. Docker Desktop on Apple
Silicon can sometimes emulate them, but serious runs should happen on Linux
`amd64`.

## Fresh Linux VM Setup

Use a real Ubuntu `amd64` VM for public comparable results. Recommended minimum:
20 vCPU, 60GB RAM, and enough disk for Docker images/eval artifacts; 32 vCPU,
96-128GB RAM, and 500GB disk leaves more room.

On the VM:

```bash
git clone git@github.com:Muhtasham/goalbench.git
cd goalbench
scripts/bootstrap-linux-vm.sh
```

The bootstrap installs base packages, Docker, `uv`, `tmux`, Codex CLI when
missing, the sibling `../ProgramBench` checkout, and the narrow
`/usr/local/bin/pb-target-exec` wrapper. It also writes a Codex config with
`service_tier = "fast"` and `[features].fast_mode = true`, so GPT-5.5 runs use
Codex fast mode by default. Fast mode trades higher credit consumption for
lower latency; API-key logins use standard API pricing instead. The bootstrap
also trusts the VM run directories and sets managed defaults for YOLO-style
`approval_policy = "never"` and `sandbox_mode = "danger-full-access"`. If it
adds your user to the Docker group, log out and back in before running sweeps.

Then authenticate Codex on the VM:

```bash
codex login
docker run --rm hello-world
scripts/doctor.sh configs/linux-smoke-miniswecompat-xhigh.json
```

For Codex app remote connections, add the VM to your local `~/.ssh/config`,
confirm `ssh <alias>` works, enable `remote_connections = true` in local Codex
config if needed, then open this repo as a remote project.

Run the Linux smoke first:

```bash
scripts/start-sweep-tmux.sh configs/linux-smoke-miniswecompat-xhigh.json
uv run python scripts/run-config.py status configs/linux-smoke-miniswecompat-xhigh.json
tail -f local_state/logs/pb-goal-linux-smoke-miniswecompat-xhigh.log
```

For smoke-only debugging, set `ALLOW_PARTIAL=1` if you want the report to
rebuild before every target finishes. Do not use that for a public full run.

If you are using a smaller Hetzner shared `cpx62` VM, use one of the labeled
16 CPU / 30GB configs. They still require strict egress and must be run as a
dedicated non-root user with the OpenAI-only egress guard active:

```bash
scripts/doctor.sh configs/cpx62-paperprompt-xhigh.json
scripts/start-sweep-tmux.sh configs/cpx62-paperprompt-xhigh.json
```

Only start full sweeps after a Linux smoke produces `submission.tar.gz`,
ProgramBench `.eval.json`, `results.csv`, and a clean audit.

## Isolation Model

In no-internet-style modes, the target binary runs in a Docker container with
`--network none`, so probes against the original program cannot reach the
internet. The paper-prompt tracks use a mini-SWE-style task workspace: bundled
documentation is copied into the workspace, `./executable` is an execute-only
black-box target shim, and Codex probes it through normal CLI/stdin/stdout/stderr
and file interactions.

Codex itself runs on the host because it must reach OpenAI. The prompts forbid
internet use, package managers, upstream source lookup, decompilers, the
ProgramBench evaluator repository, and external replacement docs for images with
missing documentation. The launcher does not enable web search. Strict runs use
a VM/user-level egress policy that only permits Codex/OpenAI traffic for the
Codex task user.

Avoid giving the Codex user direct Docker socket access. Install the narrow
target wrapper and prepare runs with `--target-access wrapper` when you want
wrapper-only target probing:

```bash
scripts/install-target-wrapper.sh
uv run python programbench_goal_runner.py prepare jqlang__jq.b33a763 \
  --inference-mode paper-prompt-nointernet \
  --target-access wrapper
```

In wrapper mode the prompt tells Codex to probe targets through:

```bash
sudo -n /usr/local/bin/pb-target-exec <container> bash -lc '<command>'
```

That wrapper only performs `docker exec -u agent` against `pb-goal-*`
containers and refuses nested Docker commands. For a publishable Linux run,
grant the dedicated Codex user only that wrapper through sudoers instead of
adding it to the `docker` group.

The Codex launcher uses YOLO mode. Generated launch scripts prefer `--yolo`
when the installed Codex accepts it, then fall back to the public long flag
`--dangerously-bypass-approvals-and-sandbox`:

```bash
codex --enable goals --disable plugins --disable apps \
  -m gpt-5.5 -c model_reasoning_effort='xhigh' \
  --yolo --no-alt-screen
```

Each generated script also marks its exact solution directory trusted in the
local Codex config before launch, so unattended `tmux` runs do not stop at the
directory trust prompt.

The generated launcher starts Codex interactively in tmux, then sends a compact
`/goal <objective>` command followed by the rendered benchmark prompt from
`GOAL_PROMPT.md`. `CODEX_INITIAL_PROMPT.md` is still written for auditability
and begins with `/goal`, but the live launch avoids passing a large multi-line
Goal as a one-shot CLI argument.

Override `--model` and `--reasoning-effort` when preparing runs if you want a
separate high/xhigh sweep. These values are written into `run.json` and the
metrics CSV. Container and `tmux` session names include the run name, so high
and xhigh runs for the same instance can coexist. Default run names include the
inference mode plus non-default model or effort values.

The generated target container defaults to 20 CPUs and 60GB RAM. For local
smoke tests on smaller machines, pass
`--docker-cpus` and `--docker-memory` to `prepare` or `prepare-batch`; do not
report those local smoke runs as full-sized Linux results.

For reportable modes, the generated Codex launcher prepends a `guard-bin`
directory to `PATH`. It blocks common host-side internet,
source/package lookup, and binary-analysis commands, restricts `docker` to the
allowed `docker exec -u agent <container> ...` target-probing form, and points
common tool caches at an empty per-run directory. Local build commands such as
`go build` and `cargo build` are still allowed; source-acquisition commands such
as `go get`, `cargo install`, and `pip install` are blocked. Agent-created
black-box probes, fuzzers, generators, and comparison scripts are allowed when
they interact with the target only through normal runtime behavior. This catches
common mistakes. It also blocks common local file-inspection commands from
reading parent directories, the run root, home paths, or the evaluator checkout.
Packaging is exposed as a `package-submission` command in `guard-bin`, so Codex
does not need to invoke or inspect parent-directory helper scripts.
This is still not a replacement for a VM/container/user-level egress policy.

## Inference Modes

Default mode is `paper-prompt-nointernet`: the ProgramBench paper prompt with
`/goal` and mini-SWE-style execution.

```bash
uv run python programbench_goal_runner.py prepare jqlang__jq.b33a763 \
  --inference-mode paper-prompt-nointernet
```

The Goal-contract variant uses the same paper prompt and mini-SWE-style
execution, but makes the outcome, verification surface, constraints, boundaries,
iteration policy, and blocked stop condition explicit:

```bash
uv run python programbench_goal_runner.py prepare jqlang__jq.b33a763 \
  --inference-mode paper-prompt-goal-contract-nointernet
```

The mini-SWE-compatible no-internet mode is retained for the already published
result and for comparison with the current site data.

All inference modes still produce
`submission.tar.gz` and can be evaluated with ProgramBench. Report them as
Codex `/goal` scaffold results, not as official mini-SWE-agent results.

## Reporting

Use ProgramBench's resolved, almost-resolved, average pass-rate, cost, and calls
shape so results are comparable to the leaderboard. Scores are computed through
ProgramBench's own `EvaluationResult` and `InstanceEvalSummary` logic after the
same active-branch and ignored-test filtering used by `programbench info`.
Resolved means the filtered behavioral test pass rate is exactly `1.0`; warning
and evaluator-problem fields are disclosed separately. Label our cost column as
estimated cost: Codex session logs expose token counts and call counts, but not
authoritative billed dollars. Label the scaffold explicitly, for example:
`GPT-5.5 xhigh / Codex goal`, and disclose wall-clock time, inference mode, and
host/network enforcement. Treat this as a scaffold comparison against
mini-SWE-agent, not an apples-to-apples model-only comparison.

Cost uses local Codex `token_count` events from both `~/.codex/sessions` and
`~/.codex/archived_sessions`. The estimate prices uncached input, cached input,
and output tokens. `reasoning_output_tokens` is kept in the audit file but is
not added again because it is a subset of output tokens in the local logs. When
OpenAI's model docs expose a long-context threshold, the summarizer applies
that multiplier per Codex call using `last_token_usage.input_tokens`.

## Strict Host Egress Guard

For a stronger run on Linux, run Codex as a dedicated non-root user and force
all model traffic through the local OpenAI allowlist proxy. This is stricter
than the DNS-to-IP allowlist because direct egress from the Codex UID is blocked
and the proxy only accepts HTTPS `CONNECT` requests to configured OpenAI/Codex
hostnames.

```bash
sudo useradd -m codex-runner
sudo nohup scripts/openai-connect-proxy.py \
  >/var/log/pb-openai-connect-proxy.log 2>&1 &
sudo scripts/linux-openai-egress-guard.sh proxy-apply codex-runner
sudo scripts/linux-openai-egress-guard.sh status codex-runner
```

Run the sweep from the coordinator account. The strict configs set
`"codex_user": "codex-runner"`, so Docker/eval orchestration can stay on the
coordinator while generated Codex `/goal` tmux sessions run as the firewalled
non-root user:

```bash
export PB_CODEX_PROXY_URL=http://127.0.0.1:18080
PUBLISH=1 scripts/start-sweep-tmux.sh configs/full-miniswecompat-xhigh.json
```

Strict egress is config-scoped, not global. The full no-internet-style configs
set `"strict_egress": true`. The runner currently supports strict egress only
for OpenAI/Codex model runs.

By default the proxy allows:

```text
api.openai.com auth.openai.com chatgpt.com ab.chatgpt.com persistent.oaistatic.com
```

Set `OPENAI_EGRESS_DOMAINS` before starting the proxy if the installed Codex
build needs an additional official OpenAI hostname. The generated Codex launcher
copies proxy environment variables into per-task `tmux` sessions so the setting
survives the sweep handoff.

There is also a simpler DNS-to-IP allowlist mode:

```bash
sudo scripts/linux-openai-egress-guard.sh apply codex-runner
```

Prefer `proxy-apply` for publishable runs. Do not apply either guard to `root`
on a live SSH VM; root-owned SSH/server processes may need normal egress. To
remove the guard:

```bash
sudo scripts/linux-openai-egress-guard.sh delete codex-runner
```

For strict compliance, do not give the Codex user broad Docker socket access.
Raw Docker access is effectively root-equivalent and can bypass network
controls. For a publishable run, expose only the narrow target-execution wrapper
and keep target observations to normal CLI/stdin/stdout/stderr/file behavior.

## Metrics

Use ProgramBench's primary metric when reporting results: fully resolved
instances. Almost-resolved and average pass rate are useful diagnostics, but
they should not be the headline score.

Metric formulas:

- `resolved_rate = count(score == 1.0) / evaluated_instances`
- `almost_resolved_rate = count(score >= 0.95) / evaluated_instances`
- `total_cost_usd = sum(estimated_cost_usd per instance)`
- `total_calls = sum(calls per instance)`
- `average_cost_usd = total_cost_usd / evaluated_instances`
- `average_calls = total_calls / evaluated_instances`

ProgramBench run-detail pages display total cost and total calls. ProgramBench's
extended leaderboard table displays average cost and average calls per instance.
The generated report follows the same split.

## Doctor

Before launching any expensive run, use the doctor script:

```bash
scripts/doctor.sh configs/linux-smoke-miniswecompat-xhigh.json
```

It checks the selected config, required commands, host architecture, Docker
daemon/resources against the config, ProgramBench checkout, wrapper access,
target set, and Codex version. `scripts/start-sweep-tmux.sh` runs the same
check before launching unless `SKIP_DOCTOR=1` is set.

Before a serious run, check metric parity against ProgramBench's scoring code
and bundled fixture runs:

```bash
uv run --project /path/to/ProgramBench python scripts/check-metric-parity.py \
  --programbench-repo /path/to/ProgramBench
```

`scripts/run-sweep.sh` runs this check automatically whenever a ProgramBench
checkout is supplied.

Local state lives under `local_state/`, which is ignored by git. Use it for
pricing snapshots, run manifests, copied Codex logs, eval JSON, result CSVs, and
trace bundles that should be shareable locally but not committed.

Refresh OpenAI pricing before summarizing cost:

```bash
uv run python scripts/refresh-openai-pricing.py
```

This writes `local_state/openai_pricing.json` from official OpenAI model docs.
OpenAI does not currently expose a supported structured pricing endpoint for
these model price cards, so this repository stores an official-doc snapshot
instead. The sweep script refreshes it before scoring. Offline rebuilds keep
using the cached snapshot and `usage-audit.json` will flag it if it is stale.
The summarizer reads that file by default; `CODEX_INPUT_USD_PER_MTOK`,
`CODEX_CACHED_INPUT_USD_PER_MTOK`, and `CODEX_OUTPUT_USD_PER_MTOK` still
override it when set.

Evaluation may need internet access to fetch ProgramBench test blobs from
Hugging Face. That is evaluator-side access, not inference-side access. For
repeatable runs, prefetch the blobs from the ProgramBench checkout before
evaluating:

```bash
uv run --project /path/to/ProgramBench programbench blob sync <instance_id>
```

## Quickstart

Prepare a `jq` run:

```bash
uv run python programbench_goal_runner.py prepare jqlang__jq.b33a763
```

Prepare a high-effort comparison run:

```bash
uv run python programbench_goal_runner.py prepare jqlang__jq.b33a763 \
  --reasoning-effort high
```

## Full 200-Task Runs

Do not start all 200 tasks from a laptop as the first serious run. Use the
config runner so the command, mode, resource settings, and batch name are
reproducible. Refresh the official 200-task target set from a ProgramBench
checkout:

```bash
uv run python scripts/write-target-set.py /path/to/ProgramBench \
  --output target_sets/all_tasks.txt
uv run python scripts/validate-target-set.py target_sets/all_tasks.txt \
  --programbench-repo /path/to/ProgramBench
```

The validator expects 200 real tasks by default: all ProgramBench task metadata
entries except the bundled `testorg__` fixture. `scripts/run-sweep.sh` runs this
validation automatically whenever the config uses `target_sets/all_tasks.txt`.

For the closest public comparison against the ProgramBench leaderboard, use the
mini-SWE-compatible no-internet Codex `/goal` scaffold: same task set,
autonomous Codex goal loop, no external lookup, and a minimal task prompt. It is
not the official mini-SWE-agent baseline, but it avoids the biggest confound
from normal Codex internet access:

```bash
scripts/run-sweep.sh --config configs/full-miniswecompat-xhigh.json --dry-run
scripts/run-sweep.sh --config configs/full-miniswecompat-xhigh.json
```

On the smaller Hetzner `cpx62` runner, use the `cpx62-*` xhigh configs. They
disclose 16 CPU / 30g instead of 20 CPU / 60g, and they still require strict
egress for every no-internet-style mode:

```bash
scripts/run-sweep.sh --config configs/cpx62-miniswecompat-xhigh.json --dry-run
scripts/run-sweep.sh --config configs/cpx62-miniswecompat-xhigh.json --incremental-finalize --publish
```

By default, `run-sweep.sh` uses `PROGRAMBENCH_REPO` if set, otherwise it
auto-detects a sibling `../ProgramBench` checkout. Run
`scripts/bootstrap-programbench.sh` first if that checkout does not exist yet.

The script refreshes `target_sets/all_tasks.txt`, runs the configured batch,
refreshes the OpenAI pricing snapshot before scoring, finalizes completed
instances, exports sanitized evidence, rebuilds `docs/`, refreshes public
ProgramBench leaderboard rows and per-task baseline context, checks report size,
and runs the privacy scan. By default this updates the local `docs/` site only.
Add `--publish` to commit and push `docs/` to GitHub Pages after the site
rebuild:

```bash
scripts/run-sweep.sh --publish
scripts/run-sweep.sh --skip-watch --publish
```

For long runs, prefer incremental finalize/reporting so each completed task is
packaged, evaluated, summarized, and optionally published without waiting for
the whole batch:

```bash
scripts/run-sweep.sh --incremental-finalize --publish
INCREMENTAL_FINALIZE=1 PUBLISH=1 scripts/start-sweep-tmux.sh configs/full-miniswecompat-xhigh.json
```

### Retry and scoring policy

Do not continue failed Codex sessions by pasting a follow-up into the same tmux
session. That changes the `/goal` condition being measured.

Only one retry class is allowed: `session_failed_before_goal_done`. Use it when
the Codex tmux session exits before `/goal` completion and before a submission is
produced. A retry starts from a fresh solution directory, keeps the prior attempt
in `attempt_history`, and is capped by `--max-attempts`:

```bash
uv run python scripts/run-config.py retry configs/cpx62-miniswecompat-xhigh.json \
  --failed \
  --max-attempts 2
```

Do not retry low scores, valid evaluations, or audit violations. If Codex
reaches `/goal` completion, produces `submission.tar.gz`, passes audit, and gets
an eval result, that result is scored even when it is bad. If Codex exits
normally without a submission, report it as no-submission/zero or disclose it
separately in the denominator.

Before scoring/publishing a current run, `scripts/run-sweep.sh` runs
`scripts/validate-run-gates.py`. Each published row must have:

- `CODEX_INITIAL_PROMPT.md` beginning with `/goal`
- a tmux transcript showing `/goal`
- `run.json` with model, reasoning effort, inference mode, and strict egress
- `submission.tar.gz`
- a passing audit marker
- a ProgramBench eval JSON

The public site intentionally publishes sanitized manifests, eval summaries,
usage audits, and score CSV/JSON. It does not publish submission tarballs by
default; those stay in the VM run root as private backups and can be archived
separately if needed.

The tmux helper uses the same defaults without publishing:

```bash
scripts/start-sweep-tmux.sh configs/nointernet-xhigh.json
PUBLISH=1 scripts/start-sweep-tmux.sh configs/nointernet-xhigh.json
```

Before starting a new tmux sweep, the helper keeps the runner checkout in sync:
it refuses to launch from a dirty worktree, then runs `git fetch origin main`
and `git merge --ff-only origin/main`. This means VM launches use the latest
pushed `main` by default without silently mixing local edits into a benchmark
run. Set `SYNC_REPO=0` only for an intentional local/debug run:

```bash
SYNC_REPO=0 scripts/start-sweep-tmux.sh configs/linux-smoke-miniswecompat-xhigh.json
```

Use `--offline-report` only when you intentionally want cached pricing and
cached ProgramBench baseline rows for a reproducible or offline rebuild.

The equivalent lower-level commands are:

```bash
uv run python scripts/run-config.py watch configs/full-miniswecompat-xhigh.json --dry-run
uv run python scripts/run-config.py watch configs/full-miniswecompat-xhigh.json
```

Run the matching high-effort sweep as a separate batch:

```bash
uv run python scripts/run-config.py watch configs/full-miniswecompat-high.json
```

For harness smoke testing, use a small target set and override parallelism
instead of the full 200-task config:

```bash
uv run python scripts/run-config.py watch configs/linux-smoke-miniswecompat-xhigh.json --dry-run
```

Treat smoke runs as harness validation only; publishable all-task runs should
still happen on Linux amd64.

Check status:

```bash
uv run python scripts/run-config.py status configs/full-miniswecompat-xhigh.json
```

Finalize completed instances, run ProgramBench evaluation, summarize metrics,
and collect local evidence:

```bash
uv run python scripts/run-config.py finalize configs/full-miniswecompat-xhigh.json \
  --programbench-repo /path/to/ProgramBench
```

That run is labeled `mini-swe-compatible-nointernet`: it blocks external lookup
and target binary analysis, uses the shorter parity prompt, and still discloses
that the scaffold is Codex `/goal`, not mini-SWE-agent.

The committed full-run mini-SWE-compatible configs use `gpt-5.5`, 20 CPUs, 60GB
RAM, and `max_parallel=10`. The paper-prompt `cpx62-*` configs use xhigh,
16 CPUs, 30GB RAM, and `max_parallel=1` for sequential inference/eval launch
validation. Lower parallelism on smaller VMs with
`scripts/run-sweep.sh --max-parallel N` or `MAX_PARALLEL=N
scripts/start-sweep-tmux.sh ...`. Do not mix reasoning modes in one batch.

The `cpx62-*` configs are sized for the current Hetzner shared runner. They
still use strict egress for no-internet-style modes.

Prepare with an official prompt template when one is available:

```bash
uv run python programbench_goal_runner.py prepare jqlang__jq.b33a763 \
  --prompt-template /path/to/official-programbench-prompt.md
```

The generated `run.json` records the prompt template path, template SHA-256,
and rendered prompt SHA-256. If ProgramBench publishes an official baseline
prompt, pin that file and report its hash with the run.

Prepare the near-miss first batch:

```bash
uv run python programbench_goal_runner.py prepare-batch target_sets/first_batch_near_miss.txt \
  --inference-mode paper-prompt-nointernet
```

Prepare the same batch with wrapper-mode target access:

```bash
uv run python programbench_goal_runner.py prepare-batch target_sets/first_batch_near_miss.txt \
  --inference-mode paper-prompt-nointernet \
  --target-access wrapper
```

For real sweeps, prefer the resumable batch manager so the laptop does not start
too many Codex `/goal` sessions at once:

```bash
uv run python scripts/run-batch.py watch target_sets/first_batch_near_miss.txt \
  --batch-name first-near-miss-xhigh \
  --max-parallel 1 \
  --inference-mode paper-prompt-nointernet \
  --reasoning-effort xhigh
```

Use `--max-parallel 1` on a laptop or small smoke VM. Use higher concurrency
only when both Codex usage limits and host capacity are comfortable. Use
separate batch names for `high`, `xhigh`, `mini-swe-compatible-nointernet`,
`paper-prompt-nointernet`, and `paper-prompt-goal-contract-nointernet` runs. The manager stores resumable state under
`local_state/batches/`, starts new work only when active sessions are below the
concurrency cap, and pauses new launches when a running pane shows rate-limit
text.

Check progress:

```bash
uv run python scripts/run-batch.py status --batch-name first-near-miss-xhigh
```

Back up a completed or in-progress version on the VM before teardown:

```bash
scripts/backup-run-root.sh \
  --batch-name first-near-miss-xhigh \
  --run-version 20260515-nointernet-xhigh-a
```

The backup is a private `tar.gz` under `local_state/backups/` by default. It
contains the batch state plus the matching `~/pb-goal-runs/` artifact tree and
is ignored by git.

After sessions reach `goal_done`, package, audit, evaluate, summarize, and
collect local artifacts:

```bash
uv run python scripts/run-batch.py finalize \
  --batch-name first-near-miss-xhigh \
  --programbench-repo /path/to/ProgramBench
```

Start the target container:

```bash
~/pb-goal-runs/gpt55-goal-open-jq/jqlang__jq.b33a763/start-target.sh
```

Check the compliance-critical container properties:

```bash
~/pb-goal-runs/gpt55-goal-open-jq/jqlang__jq.b33a763/check-compliance.sh
```

Before running expensive inference, do a full evaluator preflight with a known
bad stub on one small real task. The expected result is a clean evaluation with a
low score, not a solved task. This verifies Docker image access, blob access,
`submission.tar.gz` layout, eval JSON output, and the metrics summarizer.
The normal doctor check covers Linux `amd64`, Docker CPU/RAM capacity, wrapper
access, OpenAI egress guard status, target set, and Codex version.

Launch Codex in `tmux` and inject `/goal`:

```bash
~/pb-goal-runs/gpt55-goal-open-jq/jqlang__jq.b33a763/start-codex-goal.sh
```

Attach to the session:

```bash
tmux attach -t pb-goal-gpt55-goal-open-jq-jqlang-jq-b33a763
```

Package the submission:

```bash
~/pb-goal-runs/gpt55-goal-open-jq/jqlang__jq.b33a763/package-submission.sh
```

Inside a Codex task session, use `package-submission` from `guard-bin` instead
of parent-directory paths such as `../package-submission.sh`.

Audit the Codex JSONL trace and package shape before evaluating or reporting:

```bash
uv run python scripts/audit-run.py ~/pb-goal-runs/gpt55-goal-open-jq/jqlang__jq.b33a763
```

Evaluate from a ProgramBench checkout:

```bash
~/pb-goal-runs/gpt55-goal-open-jq/jqlang__jq.b33a763/eval-submission.sh /path/to/ProgramBench
```

Summarize leaderboard-style metrics after evaluation:

```bash
uv run --project /path/to/ProgramBench \
  python /path/to/goalbench/scripts/summarize-results.py ~/pb-goal-runs/gpt55-goal-open-jq \
  --programbench-repo /path/to/ProgramBench \
  --output results.csv
```

Run the summarizer in the ProgramBench `uv` environment because it imports
ProgramBench's scoring code. The runner itself stays separate from the evaluator
repo.

The summary reports fully resolved rate, almost-resolved rate (`score >= 0.95`,
matching ProgramBench's displayed leaderboard wording), average pass rate, Codex calls,
wall-clock hours, token usage, and estimated cost. The CSV includes model,
reasoning effort, inference mode, host/resource disclosures, and the exact Codex
JSONL `session_logs` used for each instance, so usage numbers can be audited
directly. Codex CLI session logs expose token counts and call counts, but not
authoritative dollars.

When an output CSV is written, the summarizer also writes `usage-audit.json`
next to it. That file records matched Codex session logs, token totals, pricing
source, pricing snapshot hash, cost estimates, and warnings for missing logs or
pricing. It also records `long_context_calls` when a model's official pricing
docs define a long-context multiplier. The audit includes the pricing snapshot
metadata and a freshness warning when the snapshot is older than 24 hours.

Set these environment variables to estimate cost from current pricing:

```bash
export CODEX_INPUT_USD_PER_MTOK=...
export CODEX_CACHED_INPUT_USD_PER_MTOK=...
export CODEX_OUTPUT_USD_PER_MTOK=...
```

Collect local evidence for a run after evaluation:

```bash
uv run python scripts/collect-run-artifacts.py ~/pb-goal-runs/gpt55-goal-open-jq/jqlang__jq.b33a763
```

The collector writes an ignored bundle under `local_state/run_artifacts/` with a
manifest, `run.json`, eval JSON, `results.csv`, `submission.tar.gz`, package
listing, and copied Codex JSONL logs. This is the local trace bundle to inspect
or selectively share when discussing results. Text artifacts are copied with
local home, run-root, ProgramBench-repo, and Codex-session paths redacted; the
submitted tarball is preserved byte-for-byte.

To audit a row's usage numbers, inspect the `session_logs` path from the CSV:

```bash
python3 - <<'PY' /path/to/codex-session.jsonl
import json, sys
calls = 0
last = None
for line in open(sys.argv[1], errors="replace"):
    event = json.loads(line)
    payload = event.get("payload", {})
    if event.get("type") == "event_msg" and payload.get("type") == "token_count" and payload.get("info"):
        calls += 1
        last = {
            "total_token_usage": payload["info"]["total_token_usage"],
            "last_token_usage": payload["info"].get("last_token_usage"),
        }
print({"calls": calls, **last})
PY
```

## GitHub Pages Report

The public report is generated into `docs/` and deployed by GitHub Actions. Build
it from one or more summarized result CSVs:

```bash
uv run python scripts/build-report.py \
  local_state/nointernet-sample-results.csv \
  --output-dir docs
```

`build-report.py` fetches the latest ProgramBench public baseline rows by
default before rendering. Use `--no-refresh-baselines` only for offline rebuilds.

The report keeps `mini-swe-compatible-nointernet`, `paper-prompt-nointernet`, and
`paper-prompt-goal-contract-nointernet` tracks separate, includes ProgramBench-style
resolved/almost/average-pass/estimated-cost/calls metrics, and commits only
sanitized aggregate rows. Local Codex session-log paths stay in `local_state/`
and are not published.
The summary page also plots ProgramBench-style behavioral pass-rate
distributions (histogram and cumulative), plus per-task pass rate against
estimated cost, Codex calls, and wall-clock latency. Calls are the public
compute proxy; raw token logs remain local unless explicitly exported.

Each sweep gets a `run_version`. `scripts/run-sweep.sh` generates a UTC version
when `RUN_VERSION` is not set, and you can pin one manually:

```bash
RUN_VERSION=20260514-miniswecompat-xhigh-a \
  PUBLISH=1 scripts/start-sweep-tmux.sh configs/full-miniswecompat-xhigh.json
```

The version is written into batch state, `run.json`, `results.csv`,
`results.json`, and report tables. Re-running the same config/task with a new
version creates a separate result group instead of overwriting or silently
merging scores. When the report sees more than one version for the same task,
model, mode, and compliance bucket, it adds a repeatability section with mean,
standard deviation, and best-worst delta for score, estimated cost, calls, and
wall-clock time. `uv run python scripts/run-config.py status <config>` uses the
latest version for that batch unless `RUN_VERSION` is set.

For each evaluated instance, the report also writes `docs/task/<instance_id>/`
with a ProgramBench-style task detail page: scored behavioral tests, best score,
results by model/mode, cost, calls, wall time, evidence links, and a link to the
official ProgramBench task page.

ProgramBench's public usage guide documents the per-instance `.eval.json` files
that `programbench eval` writes, including `test_results` and evaluator `log`
metadata. To publish similar evidence without exposing raw Codex traces or local
paths, export sanitized evidence first:

```bash
uv run python scripts/export-public-evidence.py
uv run python scripts/build-report.py \
  local_state/nointernet-sample-results.csv \
  --output-dir docs
```

This writes `docs/evidence/<run>/<instance>/manifest.json`,
`eval-summary.json`, and a size-safe public `eval.json`. The public eval keeps
test statuses and failure messages but redacts evaluator `log.output` payloads
and truncates long captured test text. If the local artifact contains
`usage-audit.json`, that is exported too. Raw Codex JSONL traces and
`submission.tar.gz` files remain local under `local_state/run_artifacts/`
unless explicitly reviewed and published.

Refresh the ProgramBench baseline rows before rebuilding the public report:

```bash
uv run python scripts/refresh-programbench-baselines.py
```

The report publishes `docs/data/results.json`, `docs/data/results.csv`, the
refreshed `programbench-baselines.json`, per-run detail pages under `docs/run/`,
and public evidence under `docs/evidence/`.

Before pushing a large report, check the static artifact size budget:

```bash
uv run python scripts/check-docs-size.py
```

The summary page does not load public eval files automatically; it links to
them. That keeps the main page usable for all 200 tasks while still preserving
click-through evidence per instance.

## Pilot Order

1. Near-miss conversion set in `target_sets/first_batch_near_miss.txt`.
2. Full xhigh almost-resolved set in `target_sets/gpt55_xhigh_almost_resolved.txt`.
3. Iconic follow-ups in `target_sets/iconic_followups.txt`.
4. A random control slice for generality.
