#!/usr/bin/env python3
# ruff: noqa: E501
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import shutil
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from html import unescape
from itertools import groupby
from pathlib import Path
from statistics import mean, median, stdev
from urllib.request import Request, urlopen

AGENT_NAME = "Codex /goal"
SITE_NAME = "GoalBench"
SITE_URL = "https://muhtasham.github.io/goalbench/"
SITE_DESCRIPTION = "GoalBench measures Codex /goal on ProgramBench tasks: rebuild CLI behavior from binaries and documentation, then score with ProgramBench behavioral tests."
SOCIAL_IMAGE = "assets/goalbench-social-preview.png"
PROGRAMBENCH_TASKS = 200
PROGRAMBENCH_EXTENDED = "https://programbench.com/extended/"
PROGRAMBENCH_HOME = "https://programbench.com/"
GOALBENCH_GITHUB = "https://github.com/Muhtasham/goalbench"
RUNBOOK_PAGE = "runbook.html"
ROW_RE = re.compile(r"<tr class=\"clickable-row\".*?</tr>", re.S)
CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
BRAND_SLASH_PATHS = """
  <rect width="64" height="64" rx="14" fill="#10201d"/>
  <path d="M40.5 10.5h9.75L24.25 53.5H14.5L40.5 10.5Z" fill="#effff9"/>
  <path d="M48.25 10.5h4.25L26.5 53.5h-4.25L48.25 10.5Z" fill="#14b8a6"/>
  <path d="M37.5 43.5h13v8.25h-13z" fill="#f6c453"/>
""".strip()
TAG_RE = re.compile(r"<[^>]+>")
RUN_VERSION_RE = re.compile(r"\d{8}T\d{6}Z")
PROMPTS = {
    "mini-swe-compatible-nointernet": {
        "slug": "mini-swe-compatible-nointernet",
        "title": "Mini-SWE-Compatible No Internet",
        "path": Path("prompts/programbench_goal_mini_swe_compatible.md"),
        "summary": "Short Codex /goal prompt for the closest mini-SWE-agent scaffold parity attempt.",
    },
    "paper-prompt-nointernet": {
        "slug": "paper-prompt-nointernet",
        "title": "ProgramBench Paper Prompt + /goal",
        "path": Path("prompts/programbench_goal_paper_prompt.md"),
        "summary": "Verbatim ProgramBench paper system prompt from arXiv v1, prefixed with /goal and followed by a minimal harness-context block.",
    },
    "paper-prompt-goal-contract-nointernet": {
        "slug": "paper-prompt-goal-contract-nointernet",
        "title": "Paper Prompt + Goal Contract",
        "path": Path("prompts/programbench_goal_contract_paper_prompt.md"),
        "summary": "ProgramBench paper prompt with a stronger Codex Goal contract that names outcome, verification surface, constraints, boundaries, iteration policy, and blocked stop condition.",
    },
    "no-internet": {
        "slug": "no-internet",
        "title": "No Internet",
        "path": Path("prompts/programbench_goal_no_internet.md"),
        "summary": "Stricter GoalBench prompt with explicit behavior-audit requirements.",
    },
    "no-internet-local-tools": {
        "slug": "no-internet-local-tools",
        "title": "No Internet + Local Tools",
        "path": Path("prompts/programbench_goal_local_tools.md"),
        "summary": "Non-comparable ablation prompt that keeps external lookup blocked while allowing local binary-analysis tools.",
    },
}


@dataclass
class ResultRow:
    instance_id: str
    run_name: str
    run_version: str
    model: str
    reasoning_effort: str
    inference_mode: str
    paper_compliant: bool
    score: float
    resolved: bool
    almost_resolved: bool
    evaluator_problem: bool
    error_code: str
    n_system_errors: int
    n_warnings: int
    n_resolved_tests: int
    n_tests: int
    calls: int
    wall_clock_seconds: int
    estimated_cost_usd: float
    host_system: str
    host_machine: str
    docker_cpus: str
    docker_memory: str
    pricing_source: str


BASELINES = [
    {
        "model": "GPT 5.5 (xhigh)",
        "agent": "mini-SWE-agent",
        "resolved_rate": 0.005,
        "almost_resolved_rate": 0.135,
        "average_cost_usd": 8.85,
        "average_calls": 82,
        "source": "https://programbench.com/extended/",
    },
    {
        "model": "GPT 5.5 (high)",
        "agent": "mini-SWE-agent",
        "resolved_rate": 0.005,
        "almost_resolved_rate": 0.05,
        "average_cost_usd": 3.65,
        "average_calls": 41,
        "source": "https://programbench.com/extended/",
    },
]


def slug_text(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in value.lower()).strip("-")


def fetch(url: str) -> str:
    with urlopen(Request(url, headers={"User-Agent": "goalbench/0.1"}), timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def clean_html(value: str) -> str:
    return " ".join(unescape(TAG_RE.sub(" ", value)).split())


def parse_percent(value: str) -> float:
    return float(value.rstrip("%")) / 100


def parse_money(value: str) -> float:
    return float(value.lstrip("$"))


def parse_baseline_rows(page: str) -> list[dict]:
    rows = []
    for row in ROW_RE.findall(page):
        cells = [clean_html(cell) for cell in CELL_RE.findall(row)]
        if len(cells) < 8:
            continue
        rows.append(
            {
                "model": cells[2],
                "agent": cells[3],
                "resolved_rate": parse_percent(cells[4]),
                "almost_resolved_rate": parse_percent(cells[5]),
                "average_cost_usd": parse_money(cells[6]),
                "average_calls": int(float(cells[7])),
                "source": PROGRAMBENCH_EXTENDED,
            }
        )
    if len(rows) < 2:
        raise ValueError("could not parse ProgramBench baseline rows")
    return rows


def refresh_baselines(output_dir: Path) -> None:
    path = output_dir / "data" / "programbench-baselines.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "source": PROGRAMBENCH_EXTENDED,
                "baselines": parse_baseline_rows(fetch(PROGRAMBENCH_EXTENDED)),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def load_baselines(output_dir: Path) -> list[dict]:
    path = output_dir / "data" / "programbench-baselines.json"
    return json.loads(path.read_text())["baselines"] if path.is_file() else BASELINES


def write_html(path: Path, content: str) -> None:
    path.write_text("\n".join(line.rstrip() for line in content.splitlines()) + "\n")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def prompt_record(mode: str) -> dict:
    prompt = PROMPTS.get(mode)
    if not prompt:
        return {"mode": mode, "slug": "", "title": "", "path": "", "summary": "", "sha256": "", "text": ""}
    source_path = Path(prompt["path"])
    text = source_path.read_text()
    return {
        "mode": mode,
        "slug": prompt["slug"],
        "title": prompt["title"],
        "path": f"prompt/{prompt['slug']}/",
        "source_path": str(source_path),
        "summary": prompt["summary"],
        "sha256": sha256_text(text),
        "text": text,
    }


def prompt_summary_record(mode: str) -> dict:
    prompt = prompt_record(mode)
    return {key: value for key, value in prompt.items() if key != "text"}


def prompt_records() -> list[dict]:
    return [prompt_record(mode) for mode in PROMPTS]


def prompt_link(mode: str, prefix: str = "") -> str:
    prompt = prompt_record(mode)
    if not prompt["path"]:
        return '<span class="muted">not recorded</span>'
    return f'<a href="{cell(prefix + prompt["path"])}">{cell(prompt["title"])}</a>'


def brand_slash_svg(class_name: str = "brand-mark") -> str:
    class_attr = f' class="{class_name}"' if class_name else ""
    return f'<svg{class_attr} xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" aria-hidden="true">{BRAND_SLASH_PATHS}</svg>'


def codex_logo_img(class_name: str = "codex-mark") -> str:
    return f'<img class="{class_name}" src="assets/codex-logo.png" alt="" aria-hidden="true">'


def absolute_site_url(path: str = "") -> str:
    return SITE_URL + path.lstrip("/")


def social_meta(title: str, description: str = SITE_DESCRIPTION, path: str = "") -> str:
    url = absolute_site_url(path)
    image = absolute_site_url(SOCIAL_IMAGE)
    return f"""
  <link rel="canonical" href="{cell(url)}">
  <meta name="description" content="{cell(description)}">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="{SITE_NAME}">
  <meta property="og:title" content="{cell(title)}">
  <meta property="og:description" content="{cell(description)}">
  <meta property="og:url" content="{cell(url)}">
  <meta property="og:image" content="{cell(image)}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="630">
  <meta property="og:image:alt" content="GoalBench preview card">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{cell(title)}">
  <meta name="twitter:description" content="{cell(description)}">
  <meta name="twitter:image" content="{cell(image)}">""".rstrip()


def write_support_files(output_dir: Path) -> None:
    (output_dir / "favicon.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">{BRAND_SLASH_PATHS}</svg>\n',
    )
    (output_dir / "assets").mkdir(exist_ok=True)
    shutil.copyfile(Path("assets/codex-logo.png"), output_dir / "assets" / "codex-logo.png")
    shutil.copyfile(Path("assets/goalbench-social-preview.png"), output_dir / SOCIAL_IMAGE)


def inline_markdown(text: str) -> str:
    escaped = cell(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)", lambda match: f'<a href="{cell(match.group(2))}">{match.group(1)}</a>', escaped
    )


def render_markdown_blocks(markdown: str) -> str:
    blocks = []
    paragraph = []
    list_items = []
    table_rows = []
    in_code = False
    code_lines = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{inline_markdown(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            blocks.append("<ul>" + "".join(f"<li>{inline_markdown(item)}</li>" for item in list_items) + "</ul>")
            list_items.clear()

    def flush_table() -> None:
        if table_rows:
            head, *body = table_rows
            blocks.append(
                '<div class="doc-table"><table><thead><tr>'
                + "".join(f"<th>{inline_markdown(cell_text)}</th>" for cell_text in head)
                + "</tr></thead><tbody>"
                + "".join(
                    "<tr>" + "".join(f"<td>{inline_markdown(cell_text)}</td>" for cell_text in row) + "</tr>"
                    for row in body
                )
                + "</tbody></table></div>"
            )
            table_rows.clear()

    def flush_code() -> None:
        if code_lines:
            blocks.append(f"<pre><code>{cell(chr(10).join(code_lines))}</code></pre>")
            code_lines.clear()

    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        if line.startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                flush_paragraph()
                flush_list()
                flush_table()
                in_code = True
            index += 1
            continue
        if in_code:
            code_lines.append(line)
            index += 1
            continue
        if not line.strip():
            flush_paragraph()
            flush_list()
            flush_table()
            index += 1
            continue
        if line.startswith("#"):
            flush_paragraph()
            flush_list()
            flush_table()
            level = min(len(line) - len(line.lstrip("#")), 3)
            blocks.append(f"<h{level}>{inline_markdown(line.lstrip('#').strip())}</h{level}>")
            index += 1
            continue
        if (
            line.startswith("|")
            and index + 1 < len(lines)
            and set(lines[index + 1].replace("|", "").strip()) <= {"-", ":", " "}
        ):
            flush_paragraph()
            flush_list()
            table_rows.append([part.strip() for part in line.strip("|").split("|")])
            index += 2
            while index < len(lines) and lines[index].startswith("|"):
                table_rows.append([part.strip() for part in lines[index].strip("|").split("|")])
                index += 1
            continue
        if line.startswith("- "):
            flush_paragraph()
            flush_table()
            list_items.append(line[2:].strip())
            index += 1
            continue
        numbered = re.match(r"\d+\.\s+(.*)", line)
        if numbered:
            flush_paragraph()
            flush_table()
            list_items.append(numbered.group(1).strip())
            index += 1
            continue
        paragraph.append(line.strip())
        index += 1
    flush_paragraph()
    flush_list()
    flush_table()
    flush_code()
    return "\n".join(blocks)


def render_doc_page(markdown_path: Path, title: str, page_path: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{cell(title)} · {SITE_NAME}</title>
  {social_meta(f"{title} · {SITE_NAME}", path=page_path)}
  <link rel="icon" href="favicon.svg" type="image/svg+xml">
  <style>
    :root {{ --ink: #182026; --muted: #5b6b78; --line: #d9e0e6; --soft: #f4f8f6; --accent: #0f766e; --accent-strong: #0b5f59; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: var(--ink); background: linear-gradient(180deg, #fbfcfb 0%, #f6f9f7 300px, #ffffff 301px); font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    a {{ color: #075985; text-decoration-thickness: 1px; text-underline-offset: 2px; }}
    header, main {{ max-width: 980px; margin: 0 auto; padding: 22px 28px; }}
    .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; }}
    .nav-brand {{ display: inline-flex; align-items: center; gap: 10px; color: var(--ink); font-weight: 850; text-decoration: none; }}
    .brand-mark {{ width: 30px; height: 30px; display: block; flex: 0 0 auto; }}
    .nav-links {{ display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }}
    .nav-links a {{ color: #40515c; text-decoration: none; border-radius: 6px; padding: 7px 9px; font-size: 14px; }}
    .nav-links a:hover {{ color: #075985; background: #eef6f3; }}
    .doc-hero {{ padding: 34px 0 28px; border-bottom: 1px solid var(--line); }}
    .doc-eyebrow {{ color: var(--accent-strong); font-size: 13px; font-weight: 850; margin: 0 0 10px; text-transform: uppercase; letter-spacing: 0.08em; }}
    h1 {{ margin: 0; font-size: clamp(38px, 7vw, 72px); line-height: 0.98; letter-spacing: 0; }}
    main {{ padding-top: 34px; }}
    main h1 {{ display: none; }}
    h2 {{ margin: 34px 0 10px; font-size: 24px; }}
    h3 {{ margin: 24px 0 8px; font-size: 18px; }}
    p, li {{ color: var(--muted); line-height: 1.58; }}
    ul {{ padding-left: 22px; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.92em; }}
    pre {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; padding: 14px; background: #10201d; color: #effff9; }}
    pre code {{ color: inherit; }}
    .doc-table {{ border: 1px solid var(--line); border-radius: 8px; overflow-x: auto; background: #ffffff; margin: 16px 0; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 720px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px 12px; text-align: left; vertical-align: top; font-size: 14px; }}
    th {{ background: var(--soft); color: #33424d; font-weight: 800; }}
    tr:last-child td {{ border-bottom: 0; }}
    @media (max-width: 760px) {{ header, main {{ padding-left: 16px; padding-right: 16px; }} .topbar {{ align-items: flex-start; flex-direction: column; }} .nav-links {{ justify-content: flex-start; }} }}
  </style>
</head>
<body>
  <header>
    <nav class="topbar" aria-label="Primary">
      <a class="nav-brand" href="./">{brand_slash_svg()}<span>{SITE_NAME}</span></a>
      <div class="nav-links">
        <a href="./">Leaderboard</a>
        <a href="extended/">Extended</a>
        <a href="task-details.html">Tasks</a>
        <a href="{RUNBOOK_PAGE}">Runbook</a>
        <a href="{GOALBENCH_GITHUB}">GitHub</a>
        <a href="{PROGRAMBENCH_HOME}">ProgramBench</a>
      </div>
    </nav>
    <section class="doc-hero">
      <p class="doc-eyebrow">GoalBench documentation</p>
      <h1>{cell(title)}</h1>
    </section>
  </header>
  <main>
    {render_markdown_blocks(markdown_path.read_text())}
  </main>
</body>
</html>
"""


def render_prompt_page(prompt: dict) -> str:
    prompt_title = f"{prompt['title']} Prompt · {SITE_NAME}"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{cell(str(prompt["title"]))} Prompt · {SITE_NAME}</title>
  {social_meta(prompt_title, path=str(prompt["path"]))}
  <link rel="icon" href="../../favicon.svg" type="image/svg+xml">
  <style>
    :root {{ --ink: #182026; --muted: #5b6b78; --line: #d9e0e6; --soft: #f4f8f6; --accent: #0f766e; --accent-strong: #0b5f59; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: var(--ink); background: linear-gradient(180deg, #fbfcfb 0%, #f6f9f7 300px, #ffffff 301px); font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    a {{ color: #075985; text-decoration-thickness: 1px; text-underline-offset: 2px; }}
    header, main {{ max-width: 980px; margin: 0 auto; padding: 22px 28px; }}
    .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; }}
    .nav-brand {{ display: inline-flex; align-items: center; gap: 10px; color: var(--ink); font-weight: 850; text-decoration: none; }}
    .brand-mark {{ width: 30px; height: 30px; display: block; flex: 0 0 auto; }}
    .nav-links {{ display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }}
    .nav-links a {{ color: #40515c; text-decoration: none; border-radius: 6px; padding: 7px 9px; font-size: 14px; }}
    .nav-links a:hover {{ color: #075985; background: #eef6f3; }}
    .prompt-hero {{ padding: 34px 0 28px; border-bottom: 1px solid var(--line); }}
    .prompt-eyebrow {{ color: var(--accent-strong); font-size: 13px; font-weight: 850; margin: 0 0 10px; text-transform: uppercase; letter-spacing: 0.08em; }}
    h1 {{ margin: 0; font-size: clamp(34px, 6vw, 64px); line-height: 0.98; letter-spacing: 0; }}
    p {{ color: var(--muted); line-height: 1.55; }}
    .meta-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 20px 0; }}
    .meta-grid div {{ border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #ffffff; min-width: 0; }}
    .meta-grid span {{ display: block; color: var(--muted); font-size: 12px; font-weight: 850; text-transform: uppercase; }}
    .meta-grid strong, .meta-grid code {{ display: block; margin-top: 7px; overflow-wrap: anywhere; }}
    pre {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; padding: 16px; background: #10201d; color: #effff9; line-height: 1.55; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }}
    pre code {{ color: inherit; font-size: 13px; }}
    @media (max-width: 760px) {{ header, main {{ padding-left: 16px; padding-right: 16px; }} .topbar {{ align-items: flex-start; flex-direction: column; }} .nav-links {{ justify-content: flex-start; }} .meta-grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <header>
    <nav class="topbar" aria-label="Primary">
      <a class="nav-brand" href="../../">{brand_slash_svg()}<span>{SITE_NAME}</span></a>
      <div class="nav-links">
        <a href="../../">Leaderboard</a>
        <a href="../../extended/">Extended</a>
        <a href="../../task-details.html">Tasks</a>
        <a href="../../runbook.html">Runbook</a>
        <a href="{GOALBENCH_GITHUB}">GitHub</a>
        <a href="{PROGRAMBENCH_HOME}">ProgramBench</a>
      </div>
    </nav>
    <section class="prompt-hero">
      <p class="prompt-eyebrow">Codex /goal prompt artifact</p>
      <h1>{cell(str(prompt["title"]))}</h1>
      <p>{cell(str(prompt["summary"]))}</p>
    </section>
  </header>
  <main>
    <div class="meta-grid">
      <div><span>Mode</span><strong>{cell(str(prompt["mode"]))}</strong></div>
      <div><span>Source</span><code>{cell(str(prompt["source_path"]))}</code></div>
      <div><span>SHA-256</span><code>{cell(str(prompt["sha256"]))}</code></div>
      <div><span>Machine-readable</span><a href="../../data/prompts.json">data/prompts.json</a></div>
    </div>
    <pre><code>{cell(str(prompt["text"]))}</code></pre>
  </main>
</body>
</html>
"""


def load_task_baselines(output_dir: Path) -> dict:
    path = output_dir / "data" / "programbench-task-baselines.json"
    return json.loads(path.read_text())["tasks"] if path.is_file() else {}


def as_bool(value: str) -> bool:
    return value.lower() == "true"


def as_int(value: str) -> int:
    return int(float(value)) if value else 0


def as_float(value: str) -> float:
    return float(value) if value else 0.0


def inferred_run_version(row: dict) -> str:
    if row.get("run_version", ""):
        return row["run_version"]
    match = RUN_VERSION_RE.search(row["run_name"])
    return match.group(0) if match else ""


def read_results(path: Path) -> list[ResultRow]:
    with path.open(newline="") as f:
        return [
            ResultRow(
                instance_id=row["instance_id"],
                run_name=row["run_name"],
                run_version=inferred_run_version(row),
                model=row["model"],
                reasoning_effort=row["reasoning_effort"],
                inference_mode=row["inference_mode"],
                paper_compliant=as_bool(row.get("paper_compliant", "")),
                score=as_float(row["score"]),
                resolved=as_bool(row["resolved"]),
                almost_resolved=as_bool(row["almost_resolved"]),
                evaluator_problem=as_bool(row.get("evaluator_problem", "")),
                error_code=row.get("error_code", ""),
                n_system_errors=as_int(row.get("n_system_errors", "")),
                n_warnings=as_int(row.get("n_warnings", "")),
                n_resolved_tests=as_int(row["n_resolved_tests"]),
                n_tests=as_int(row["n_tests"]),
                calls=as_int(row["calls"]),
                wall_clock_seconds=as_int(row["wall_clock_seconds"]),
                estimated_cost_usd=as_float(row["estimated_cost_usd"]),
                host_system=row["host_system"],
                host_machine=row["host_machine"],
                docker_cpus=row["docker_cpus"],
                docker_memory=row["docker_memory"],
                pricing_source=row.get("pricing_source", ""),
            )
            for row in csv.DictReader(f)
        ]


def read_target_ids(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


def aggregate(rows: list[ResultRow]) -> dict:
    return {
        "instances": len(rows),
        "resolved": sum(row.resolved for row in rows),
        "almost_resolved": sum(row.almost_resolved for row in rows),
        "resolved_rate": sum(row.resolved for row in rows) / len(rows) if rows else 0,
        "almost_resolved_rate": sum(row.almost_resolved for row in rows) / len(rows) if rows else 0,
        "average_pass_rate": sum(row.score for row in rows) / len(rows) if rows else 0,
        "total_calls": sum(row.calls for row in rows),
        "average_calls": sum(row.calls for row in rows) / len(rows) if rows else 0,
        "total_cost_usd": sum(row.estimated_cost_usd for row in rows),
        "average_cost_usd": sum(row.estimated_cost_usd for row in rows) / len(rows) if rows else 0,
        "total_wall_clock_hours": sum(row.wall_clock_seconds for row in rows) / 3600,
    }


def percentile(values: list[int], q: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * q)]


def duration_bucket(rows: list[ResultRow], lower: int, upper: int | None) -> dict:
    count = sum(lower <= row.wall_clock_seconds and (upper is None or row.wall_clock_seconds < upper) for row in rows)
    return {
        "lower_seconds": lower,
        "upper_seconds": upper,
        "count": count,
        "rate": count / len(rows) if rows else 0,
    }


def duration_summary(rows: list[ResultRow]) -> dict:
    seconds = [row.wall_clock_seconds for row in rows]
    longest = max(rows, key=lambda row: row.wall_clock_seconds) if rows else None
    return {
        "instances": len(rows),
        "average_seconds": mean(seconds) if seconds else 0,
        "median_seconds": median(seconds) if seconds else 0,
        "p75_seconds": percentile(seconds, 0.75),
        "p90_seconds": percentile(seconds, 0.90),
        "p95_seconds": percentile(seconds, 0.95),
        "max_seconds": max(seconds) if seconds else 0,
        "over_6h": sum(row.wall_clock_seconds > 6 * 60 * 60 for row in rows),
        "buckets": [
            {"label": "<15 min", **duration_bucket(rows, 0, 15 * 60)},
            {"label": "15-30 min", **duration_bucket(rows, 15 * 60, 30 * 60)},
            {"label": ">30 min", **duration_bucket(rows, 30 * 60, None)},
        ],
        "longest_task": row_to_dict(longest) if longest else {},
    }


def stddev(values: list[float]) -> float:
    return stdev(values) if len(values) > 1 else 0


def model_display(row: ResultRow) -> str:
    name = row.model.upper().replace("-", " ", 1).replace("-", ".")
    return f"{name} ({row.reasoning_effort})" if row.reasoning_effort else name


def mode_label(row: ResultRow) -> str:
    if row.inference_mode == "paper":
        return "Legacy internal"
    return {
        "no-internet": "No internet",
        "mini-swe-compatible-nointernet": "Mini-SWE-compatible no internet",
        "paper-prompt-nointernet": "Paper prompt no internet",
        "paper-prompt-goal-contract-nointernet": "Paper prompt + Goal contract no internet",
        "no-internet-local-tools": "No internet + local tools",
    }.get(row.inference_mode, row.inference_mode or "Unknown")


def is_programbench_comparable(row: ResultRow) -> bool:
    return (
        row.inference_mode == "paper"
        and row.paper_compliant
        and row.host_system == "Linux"
        and row.host_machine in {"x86_64", "AMD64"}
        and row.docker_cpus == "20"
        and row.docker_memory == "60g"
    )


def host_profile(row: ResultRow) -> str:
    if row.host_system != "Linux" or row.host_machine not in {"x86_64", "AMD64"}:
        return f"{row.host_system}/{row.host_machine} host"
    if row.docker_cpus == "20" and row.docker_memory == "60g":
        return "20 CPU / 60g host"
    return f"smaller VM: {row.docker_cpus} CPU / {row.docker_memory}"


def compliance_label(row: ResultRow) -> str:
    if row.inference_mode == "no-internet":
        return "Codex no-internet ablation"
    if row.inference_mode == "mini-swe-compatible-nointernet":
        return "Codex /goal mini-SWE-compatible no-internet"
    if row.inference_mode == "paper-prompt-nointernet":
        return "Codex /goal paper-prompt no-internet"
    if row.inference_mode == "paper-prompt-goal-contract-nointernet":
        return "Codex /goal contract no-internet"
    if row.inference_mode == "no-internet-local-tools":
        return "Non-compliant: local/binary tools allowed"
    if is_programbench_comparable(row):
        return "Legacy internal"
    return "Local smoke: host/resources differ"


def group_key(row: ResultRow) -> tuple[str, str, str, str, str]:
    return (model_display(row), AGENT_NAME, mode_label(row), compliance_label(row), row.run_version)


def result_groups(rows: list[ResultRow]) -> list[dict]:
    grouped = ((key, list(group_rows)) for key, group_rows in groupby(sorted(rows, key=group_key), key=group_key))
    groups = [
        {
            "slug": run_slug(key),
            "model": key[0],
            "agent": key[1],
            "mode": key[2],
            "compliance": key[3],
            "host_profile": ", ".join(sorted({host_profile(row) for row in group_rows})),
            "run_version": key[4],
            "prompt": prompt_summary_record(group_rows[0].inference_mode),
            "duration": duration_summary(group_rows),
            "score_distribution": distribution_bins(group_rows),
            **aggregate(group_rows),
        }
        for key, group_rows in grouped
    ]
    return sorted(
        groups,
        key=lambda item: (item["resolved_rate"], item["almost_resolved_rate"], item["average_pass_rate"]),
        reverse=True,
    )


def run_slug(key: tuple[str, str, str, str, str]) -> str:
    model, _agent, mode, compliance, version = key
    parts = [model, mode]
    if version:
        parts.append(version)
    if compliance == "Local smoke: host/resources differ":
        parts.append(compliance)
    return slug_text("-".join(parts))


def repeatability_groups(rows: list[ResultRow]) -> list[dict]:
    grouped = groupby(
        sorted(rows, key=lambda row: (row.instance_id, model_display(row), mode_label(row), compliance_label(row))),
        key=lambda row: (row.instance_id, model_display(row), mode_label(row), compliance_label(row)),
    )
    repeated = []
    for key, group_rows in grouped:
        attempts = list(group_rows)
        versions = sorted({version_label(row.run_version) for row in attempts})
        if len(versions) < 2:
            continue
        scores = [row.score for row in attempts]
        costs = [row.estimated_cost_usd for row in attempts]
        calls = [float(row.calls) for row in attempts]
        walls = [row.wall_clock_seconds / 3600 for row in attempts]
        repeated.append(
            {
                "instance_id": key[0],
                "model": key[1],
                "mode": key[2],
                "compliance": key[3],
                "attempts": len(attempts),
                "versions": versions,
                "score_mean": mean(scores),
                "score_stdev": stddev(scores),
                "score_delta": max(scores) - min(scores),
                "score_min": min(scores),
                "score_max": max(scores),
                "cost_mean": mean(costs),
                "cost_stdev": stddev(costs),
                "calls_mean": mean(calls),
                "calls_stdev": stddev(calls),
                "wall_mean_hours": mean(walls),
                "wall_stdev_hours": stddev(walls),
                "resolved_attempts": sum(row.resolved for row in attempts),
                "almost_attempts": sum(row.almost_resolved for row in attempts),
            }
        )
    return sorted(repeated, key=lambda item: (item["score_delta"], item["score_stdev"]), reverse=True)


def repeatability_summary(repeated: list[dict]) -> dict:
    return {
        "repeated_cells": len(repeated),
        "attempts": sum(item["attempts"] for item in repeated),
        "average_score_stdev": mean([item["score_stdev"] for item in repeated]) if repeated else 0,
        "max_score_delta": max([item["score_delta"] for item in repeated], default=0),
        "average_cost_stdev": mean([item["cost_stdev"] for item in repeated]) if repeated else 0,
        "average_calls_stdev": mean([item["calls_stdev"] for item in repeated]) if repeated else 0,
    }


def row_to_dict(row: ResultRow) -> dict:
    evidence_path = f"evidence/{row.run_name}/{row.instance_id}/manifest.json"
    return {
        "instance_id": row.instance_id,
        "run_name": row.run_name,
        "run_version": row.run_version,
        "evidence_path": evidence_path if Path("docs", evidence_path).is_file() else "",
        "task_path": f"task/{row.instance_id}/",
        "model": row.model,
        "model_display": model_display(row),
        "agent": AGENT_NAME,
        "reasoning_effort": row.reasoning_effort,
        "inference_mode": row.inference_mode,
        "mode": mode_label(row),
        "prompt": prompt_summary_record(row.inference_mode),
        "compliance": compliance_label(row),
        "host_profile": host_profile(row),
        "programbench_comparable": is_programbench_comparable(row),
        "paper_compliant": row.paper_compliant,
        "score": row.score,
        "resolved": row.resolved,
        "almost_resolved": row.almost_resolved,
        "evaluator_problem": row.evaluator_problem,
        "error_code": row.error_code,
        "n_system_errors": row.n_system_errors,
        "n_warnings": row.n_warnings,
        "n_resolved_tests": row.n_resolved_tests,
        "n_tests": row.n_tests,
        "calls": row.calls,
        "wall_clock_seconds": row.wall_clock_seconds,
        "estimated_cost_usd": row.estimated_cost_usd,
        "host_system": row.host_system,
        "host_machine": row.host_machine,
        "docker_cpus": row.docker_cpus,
        "docker_memory": row.docker_memory,
        "pricing_source": row.pricing_source,
    }


def task_groups(rows: list[ResultRow], target_ids: list[str], official_tasks: dict) -> list[dict]:
    row_groups = {
        instance_id: list(group_rows)
        for instance_id, group_rows in groupby(
            sorted(rows, key=lambda row: row.instance_id), key=lambda row: row.instance_id
        )
    }
    instance_ids = target_ids or sorted(row_groups)
    return [
        {
            "instance_id": instance_id,
            "task_path": f"task/{instance_id}/",
            "official_task_url": f"https://programbench.com/task/{instance_id}/",
            "result_count": len(task_rows),
            "scored_tests": max((row.n_tests for row in task_rows), default=None),
            "best_score": max((row.score for row in task_rows), default=None),
            "best_model": model_display(max(task_rows, key=lambda row: row.score)) if task_rows else "",
            "official_generated_tests": official_tasks.get(instance_id, {}).get("generated_tests"),
            "official_best_score": official_tasks.get(instance_id, {}).get("best_score"),
            "official_result_count": len(official_tasks.get(instance_id, {}).get("results", [])),
        }
        for instance_id in instance_ids
        for task_rows in [row_groups.get(instance_id, [])]
    ]


def percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def money(value: float) -> str:
    return f"${value:.2f}"


def display_timestamp(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M UTC")


def evaluated_instances_label(count: int) -> str:
    return f"{count} evaluated instances" if count else "Awaiting fresh evaluation results"


def whole_money(value: float) -> str:
    return f"${value:,.0f}" if value >= 100 else money(value)


def integer(value: float | int) -> str:
    return f"{value:,.0f}"


def minutes(value: float) -> str:
    return f"{value / 60:.1f} min"


def hours(value: float) -> str:
    return f"{Decimal(str(value / 3600)).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)}h"


def short_minutes(value: float) -> str:
    return f"{value / 60:.1f}m"


def cell(value: str) -> str:
    return html.escape(value)


def plot_points(rows: list[ResultRow], x_value, x_label: str, x_format) -> str:
    width = 360
    height = 220
    left = 42
    right = 18
    top = 18
    bottom = 34
    plot_width = width - left - right
    plot_height = height - top - bottom
    values = [x_value(row) for row in rows]
    max_x = max(values) if values else 1
    max_x = max_x or 1
    return f"""
      <svg class="plot" viewBox="0 0 {width} {height}" role="img" aria-label="Pass rate by {cell(x_label)}">
        <line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" />
        <line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" />
        <text x="{left - 8}" y="{top + 4}" text-anchor="end">100%</text>
        <text x="{left - 8}" y="{top + plot_height + 4}" text-anchor="end">0%</text>
        <text x="{left}" y="{height - 8}">0</text>
        <text x="{left + plot_width}" y="{height - 8}" text-anchor="end">{cell(x_format(max_x))}</text>
        <text x="{width / 2}" y="{height - 8}" text-anchor="middle">{cell(x_label)}</text>
        <text x="12" y="{height / 2}" transform="rotate(-90 12 {height / 2})" text-anchor="middle">pass rate</text>
        {"".join(plot_circle(row, x_value, x_label, x_format, max_x, left, top, plot_width, plot_height) for row in rows)}
      </svg>
    """


def plot_circle(row: ResultRow, x_value, x_label: str, x_format, max_x, left, top, plot_width, plot_height) -> str:
    x = left + (x_value(row) / max_x) * plot_width
    y = top + (1 - row.score) * plot_height
    title = (
        f"{row.instance_id}: {percent(row.score)} pass, {x_label.lower()} {x_format(x_value(row))}, {mode_label(row)}"
    )
    color = "#047857" if row.resolved else "#d97706" if row.almost_resolved else "#be123c"
    return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5.5" fill="{color}"><title>{cell(title)}</title></circle>'


def distribution_bins(rows: list[ResultRow]) -> list[dict]:
    counts = [0] * 10
    for row in rows:
        counts[min(int(row.score * 10), 9)] += 1
    return [
        {
            "lower": index / 10,
            "upper": (index + 1) / 10,
            "label": f"{index * 10}-{(index + 1) * 10}%",
            "count": count,
            "cumulative_at_least": sum(counts[index:]),
            "cumulative_rate_at_least": sum(counts[index:]) / len(rows) if rows else 0,
        }
        for index, count in enumerate(counts)
    ]


def distribution_svg(rows: list[ResultRow], cumulative: bool) -> str:
    width = 520
    height = 240
    left = 42
    right = 14
    top = 18
    bottom = 46
    plot_width = width - left - right
    plot_height = height - top - bottom
    bins = distribution_bins(rows)
    values = [item["cumulative_at_least"] if cumulative else item["count"] for item in bins]
    max_y = max(values) if values else 1
    max_y = max_y or 1
    bar_width = plot_width / len(bins)
    bars = []
    for index, item in enumerate(bins):
        value = values[index]
        bar_height = (value / max_y) * plot_height
        x = left + index * bar_width + 2
        y = top + plot_height - bar_height
        title = (
            f"{item['label']} pass: {value} task(s)"
            if not cumulative
            else f">= {item['lower']:.0%} pass: {value} task(s), {percent(item['cumulative_rate_at_least'])}"
        )
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width - 4:.1f}" height="{bar_height:.1f}" fill="#0f766e"><title>{cell(title)}</title></rect>'
        )
    return f"""
      <svg class="plot" viewBox="0 0 {width} {height}" role="img" aria-label="Behavioral test pass rate {"cumulative distribution" if cumulative else "histogram"}">
        <line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" />
        <line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" />
        <text x="{left - 8}" y="{top + 4}" text-anchor="end">{max_y}</text>
        <text x="{left - 8}" y="{top + plot_height + 4}" text-anchor="end">0</text>
        <text x="{left}" y="{height - 12}">0%</text>
        <text x="{left + plot_width}" y="{height - 12}" text-anchor="end">100%</text>
        <text x="{width / 2}" y="{height - 12}" text-anchor="middle">behavioral test pass rate</text>
        <text x="12" y="{height / 2}" transform="rotate(-90 12 {height / 2})" text-anchor="middle">tasks</text>
        {"".join(bars)}
      </svg>
    """


def official_distribution_svg(rows: list[dict], cumulative: bool) -> str:
    result_rows = [
        ResultRow(
            instance_id=row["instance_id"],
            run_name="official",
            run_version="",
            model="official",
            reasoning_effort="",
            inference_mode="official",
            paper_compliant=True,
            score=float(row["score"] or 0),
            resolved=row["score"] == 1.0,
            almost_resolved=row["score"] is not None and row["score"] >= 0.95,
            evaluator_problem=False,
            error_code="",
            n_system_errors=0,
            n_warnings=0,
            n_resolved_tests=0,
            n_tests=0,
            calls=int(row["calls"]),
            wall_clock_seconds=0,
            estimated_cost_usd=float(row["cost_usd"]),
            host_system="",
            host_machine="",
            docker_cpus="",
            docker_memory="",
            pricing_source="",
        )
        for row in rows
    ]
    return distribution_svg(result_rows, cumulative)


def render_score_distribution(rows: list[ResultRow]) -> str:
    if not rows:
        return ""
    return f"""
    <h2>Behavioral Test Pass Rate Distribution</h2>
    <p>These plots mirror ProgramBench's run-detail views: a score histogram and a cumulative count by behavioral test pass-rate bucket.</p>
    <div class="plot-grid">
      <section class="plot-card">
        <h3>Histogram</h3>
        {distribution_svg(rows, cumulative=False)}
      </section>
      <section class="plot-card">
        <h3>Cumulative</h3>
        {distribution_svg(rows, cumulative=True)}
      </section>
    </div>
    """


def render_efficiency_plots(rows: list[ResultRow]) -> str:
    if not rows:
        return ""
    return f"""
    <h2>Efficiency Plots</h2>
    <p>Each point is one evaluated task. Compute is shown as Codex calls because public results omit raw token logs; cost is estimated from local token logs and a pricing snapshot.</p>
    <div class="plot-grid">
      <section class="plot-card">
        <h3>Pass vs. Est. Cost</h3>
        {plot_points(rows, lambda row: row.estimated_cost_usd, "Est. cost (USD)", money)}
      </section>
      <section class="plot-card">
        <h3>Pass vs. Calls</h3>
        {plot_points(rows, lambda row: row.calls, "Codex calls", lambda value: f"{value:.0f}")}
      </section>
      <section class="plot-card">
        <h3>Pass vs. Latency</h3>
        {plot_points(rows, lambda row: row.wall_clock_seconds / 3600, "Wall-clock hours", lambda value: f"{value:.2f}h")}
      </section>
    </div>
    """


def pending_plot(title: str, x_label: str) -> str:
    return f"""
      <section class="plot-card pending-plot">
        <h3>{cell(title)}</h3>
        <svg class="plot" viewBox="0 0 360 220" role="img" aria-label="{cell(title)} pending results">
          <line x1="42" y1="18" x2="42" y2="186" />
          <line x1="42" y1="186" x2="342" y2="186" />
          <text x="34" y="22" text-anchor="end">100%</text>
          <text x="34" y="190" text-anchor="end">0%</text>
          <text x="192" y="212" text-anchor="middle">{cell(x_label)}</text>
          <text x="12" y="110" transform="rotate(-90 12 110)" text-anchor="middle">pass rate</text>
          <text x="192" y="106" text-anchor="middle">waiting for results</text>
        </svg>
      </section>
    """


def render_pending_charts() -> str:
    return f"""
    <h2>Score by Model × Task</h2>
    <p>The matrix below mirrors ProgramBench's all-models-by-all-tasks view for this scaffold. Cells are pending until Codex results are published, then link to task pages with per-task detail.</p>
    <div class="score-matrix pending-matrix">
      {"".join('<a class="heat-cell pending" title="pending"></a>' for _ in range(PROGRAMBENCH_TASKS))}
    </div>
    <h2>Behavioral Test Pass Rate Distribution</h2>
    <p>The distribution plots are wired into the report. They stay empty until the first evaluated task lands, then render ProgramBench-style cumulative and histogram views.</p>
    <div class="plot-grid">
      {pending_plot("Cumulative", "behavioral test pass rate")}
      {pending_plot("Histogram", "behavioral test pass rate")}
    </div>
    <h2>Model Comparison</h2>
    <p>Model comparison plots appear once there are at least two Codex result groups. Each dot will be one task instance, matching ProgramBench's comparison framing.</p>
    <div class="plot-grid">
      {pending_plot("Model Comparison", "comparison score")}
    </div>
    <h2>Efficiency Plots</h2>
    <p>The chart slots are wired into the report. They stay empty until the first evaluated task lands, then render ProgramBench-style pass-rate distribution plus pass rate by cost, calls, and latency.</p>
    <div class="plot-grid">
      {pending_plot("Pass vs. Est. Cost", "estimated cost")}
      {pending_plot("Pass vs. Calls", "Codex calls")}
      {pending_plot("Pass vs. Latency", "wall-clock hours")}
    </div>
    """


def render_summary_cards(label: str, summary: dict) -> str:
    return f"""
      <section class="summary-card">
        <div class="summary-title">{cell(label)}</div>
        <div class="summary-meta">{cell(summary_meta(summary))}</div>
        <div class="metric-grid">
          <div><strong>{summary["instances"]}</strong><span>instances</span></div>
          <div><strong>{percent(summary["resolved_rate"])}</strong><span>resolved</span></div>
          <div><strong>{percent(summary["almost_resolved_rate"])}</strong><span>almost</span></div>
          <div><strong>{percent(summary["average_pass_rate"])}</strong><span>avg pass</span></div>
          <div><strong>{whole_money(summary["total_cost_usd"])}</strong><span>total est. cost</span></div>
          <div><strong>{integer(summary["total_calls"])}</strong><span>total calls</span></div>
          <div><strong>{money(summary["average_cost_usd"])}</strong><span>est. cost / task</span></div>
          <div><strong>{summary["average_calls"]:.1f}</strong><span>calls / task</span></div>
          <div><strong>{short_minutes(summary["duration"]["average_seconds"])}</strong><span>avg /goal session</span></div>
          <div><strong>{short_minutes(summary["duration"]["max_seconds"])}</strong><span>max /goal session</span></div>
        </div>
      </section>
    """


def summary_title(group: dict) -> str:
    version = str(group.get("run_version", ""))
    return f"{group['model']} · {group['mode']}" + (f" · {version}" if version else "")


def summary_meta(summary: dict) -> str:
    version = str(summary.get("run_version", ""))
    return " · ".join(
        [
            str(summary["compliance"]),
            str(summary["host_profile"]),
            f"version {version}" if version else "version not recorded",
        ]
    )


def result_count(summary: dict, key: str) -> str:
    return f"{percent(summary[key + '_rate'])} ({summary[key]}/{summary['instances']})"


def run_metric_cards(group: dict) -> str:
    return f"""
    <div class="run-kpis" aria-label="Run metrics">
      <div class="run-kpi primary"><span>Resolved</span><strong>{group["resolved"]} / {group["instances"]}</strong><em>{percent(group["resolved_rate"])}</em></div>
      <div class="run-kpi"><span>Almost resolved</span><strong>{group["almost_resolved"]} / {group["instances"]}</strong><em>{percent(group["almost_resolved_rate"])}</em></div>
      <div class="run-kpi"><span>Average pass rate</span><strong>{percent(group["average_pass_rate"])}</strong><em>behavioral tests</em></div>
      <div class="run-kpi"><span>Total est. cost</span><strong>{whole_money(group["total_cost_usd"])}</strong><em>{money(group["average_cost_usd"])} / task</em></div>
      <div class="run-kpi"><span>Total calls</span><strong>{integer(group["total_calls"])}</strong><em>{group["average_calls"]:.1f} / task</em></div>
      <div class="run-kpi"><span>Avg /goal session</span><strong>{short_minutes(group["duration"]["average_seconds"])}</strong><em>{hours(group["duration"]["average_seconds"])}</em></div>
      <div class="run-kpi"><span>Max /goal session</span><strong>{short_minutes(group["duration"]["max_seconds"])}</strong><em>{hours(group["duration"]["max_seconds"])}</em></div>
      <div class="run-kpi"><span>Total wall time</span><strong>{group["total_wall_clock_hours"]:.2f}h</strong><em>sum across {group["instances"]} task{"s" if group["instances"] != 1 else ""}</em></div>
    </div>
    """


def render_duration_summary(summary: dict, prefix: str = "") -> str:
    if not summary["instances"]:
        return ""
    longest = summary["longest_task"]
    buckets = "\n".join(
        f"""
        <div class="duration-bucket">
          <span>{cell(bucket["label"])}</span>
          <strong>{bucket["count"]} / {summary["instances"]}</strong>
          <em>{percent(bucket["rate"])}</em>
        </div>
        """
        for bucket in summary["buckets"]
    )
    return f"""
    <section class="duration-panel" aria-label="Run duration distribution">
      <div>
        <h2>Goal Session Duration</h2>
        <p class="muted">Wall-clock time is measured per Codex <code>/goal</code> session from launch to packaged submission. ProgramBench's public mini-SWE-agent runs use a much larger timeout, so this block makes latency differences explicit.</p>
      </div>
      <div class="duration-stats">
        <div><span>Average</span><strong>{hours(summary["average_seconds"])}</strong><em>{minutes(summary["average_seconds"])}</em></div>
        <div><span>Median</span><strong>{hours(summary["median_seconds"])}</strong><em>{minutes(summary["median_seconds"])}</em></div>
        <div><span>P75</span><strong>{hours(summary["p75_seconds"])}</strong><em>{minutes(summary["p75_seconds"])}</em></div>
        <div><span>P90</span><strong>{hours(summary["p90_seconds"])}</strong><em>{minutes(summary["p90_seconds"])}</em></div>
        <div><span>P95</span><strong>{hours(summary["p95_seconds"])}</strong><em>{minutes(summary["p95_seconds"])}</em></div>
        <div><span>Max</span><strong>{hours(summary["max_seconds"])}</strong><em>{minutes(summary["max_seconds"])}</em></div>
        <div><span>Over 6h</span><strong>{summary["over_6h"]} / {summary["instances"]}</strong><em>{percent(summary["over_6h"] / summary["instances"])}</em></div>
      </div>
      <div class="duration-buckets">{buckets}</div>
      <p class="duration-longest">Longest task: <a href="{cell(prefix + str(longest["task_path"]))}"><code>{cell(str(longest["instance_id"]))}</code></a> at <strong>{minutes(float(longest["wall_clock_seconds"]))}</strong>.</p>
    </section>
    """


def version_label(version: str) -> str:
    return version or "not recorded"


def render_leaderboard(groups: list[dict], prefix: str = "") -> str:
    return "\n".join(
        f"""
            <tr>
              <td>{index}</td>
              <td><a href="{prefix}run/{cell(str(group["slug"]))}/">{cell(str(group["model"]))}</a></td>
              <td><code>{cell(version_label(str(group.get("run_version", ""))))}</code></td>
              <td>{cell(str(group["agent"]))}</td>
              <td>{result_count(group, "resolved")}</td>
              <td>{result_count(group, "almost_resolved")}</td>
              <td>{money(group["average_cost_usd"])}</td>
              <td>{group["average_calls"]:.1f}</td>
              <td>{short_minutes(group["duration"]["average_seconds"])}</td>
              <td>{short_minutes(group["duration"]["max_seconds"])}</td>
            </tr>
            """
        for index, group in enumerate(groups, start=1)
    )


def render_disclosures(groups: list[dict]) -> str:
    return "\n".join(
        f"""
            <tr>
              <td>{index}</td>
              <td>{cell(str(group["model"]))}</td>
              <td><code>{cell(version_label(str(group.get("run_version", ""))))}</code></td>
              <td>{cell(str(group["mode"]))}</td>
              <td>{cell(str(group["compliance"]))}</td>
              <td>{cell(str(group["host_profile"]))}</td>
              <td>{group["instances"]}/{PROGRAMBENCH_TASKS}</td>
              <td>{percent(group["average_pass_rate"])}</td>
              <td>{prompt_link(str(group["prompt"]["mode"]))}</td>
              <td>{short_minutes(group["duration"]["average_seconds"])}</td>
              <td>{short_minutes(group["duration"]["max_seconds"])}</td>
              <td>{group["total_wall_clock_hours"]:.2f}h</td>
            </tr>
            """
        for index, group in enumerate(groups, start=1)
    )


def render_instances(rows: list[ResultRow], prefix: str = "") -> str:
    table_rows = []
    for index, row in enumerate(
        sorted(rows, key=lambda item: (item.resolved, item.almost_resolved, item.score), reverse=True), start=1
    ):
        status = "resolved" if row.resolved else "almost" if row.almost_resolved else "open"
        table_rows.append(
            f"""
            <tr>
              <td>{index}</td>
              <td><a href="{task_page_link(row, prefix)}"><code>{cell(row.instance_id)}</code></a></td>
              <td><code>{cell(version_label(row.run_version))}</code></td>
              <td>{cell(mode_label(row))}</td>
              <td>{cell(model_display(row))}</td>
              <td>{cell(compliance_label(row))}</td>
              <td><span class="status {status}">{status}</span></td>
              <td>{eval_status(row)}</td>
              <td>{percent(row.score)}</td>
              <td>{row.n_resolved_tests}/{row.n_tests}</td>
              <td>{money(row.estimated_cost_usd)}</td>
              <td>{row.calls}</td>
              <td>{row.wall_clock_seconds / 3600:.2f}h</td>
              <td>{cell(row.host_system)}/{cell(row.host_machine)}</td>
              <td>{cell(row.docker_cpus)} CPU / {cell(row.docker_memory)}</td>
              <td>{evidence_links(row, prefix)}</td>
            </tr>
            """
        )
    return "\n".join(table_rows)


def eval_status(row: ResultRow) -> str:
    if row.error_code or row.n_system_errors:
        return f'<span class="status open" title="{cell(row.error_code or "system errors")}">error</span>'
    if row.n_warnings:
        return f'<span class="status almost" title="{row.n_warnings} evaluator warning(s)">warn {row.n_warnings}</span>'
    return '<span class="status resolved">ok</span>'


def evidence_links(row: ResultRow, prefix: str = "") -> str:
    base = f"evidence/{row.run_name}/{row.instance_id}"
    links = [
        (f"{base}/manifest.json", "manifest"),
        (f"{base}/eval.json", "eval json"),
        (f"{base}/eval-summary.json", "eval summary"),
        (f"{base}/usage-audit.json", "usage audit"),
    ]
    rendered = " · ".join(
        f'<a href="{cell(prefix + path)}">{label}</a>' for path, label in links if Path("docs", path).is_file()
    )
    return rendered or '<span class="muted">not exported</span>'


def task_page_link(row: ResultRow, prefix: str = "") -> str:
    return f"{prefix}task/{row.instance_id}/"


def official_run_url(model: str) -> str:
    return {
        "GPT 5.5 (xhigh)": "https://programbench.com/run/gpt-5-5-xhigh/",
        "GPT 5.5 (high)": "https://programbench.com/run/gpt-5-5-high/",
    }.get(model, "")


def render_baselines(baselines: list[dict]) -> str:
    return "\n".join(
        f"""
        <tr>
          <td>{index}</td>
          <td>{baseline_model_link(row)}</td>
          <td>{cell(str(row["agent"]))}</td>
          <td>{percent(float(row["resolved_rate"]))}</td>
          <td>{percent(float(row["almost_resolved_rate"]))}</td>
          <td>{money(float(row["average_cost_usd"]))}</td>
          <td>{row["average_calls"]}</td>
        </tr>
        """
        for index, row in enumerate(baselines, start=1)
    )


def baseline_model_link(row: dict) -> str:
    url = official_run_url(str(row["model"]))
    if url:
        return f'<a href="{cell(url)}">{cell(str(row["model"]))}</a>'
    return cell(str(row["model"]))


def render_csv(rows: list[ResultRow]) -> str:
    output = []
    names = [field.name for field in fields(ResultRow)]
    prompt_names = ["prompt", "prompt_path", "prompt_sha256"]
    output.append(",".join(names + prompt_names))
    for row in rows:
        values = []
        prompt = prompt_record(row.inference_mode)
        for value in [getattr(row, name) for name in names] + [prompt["title"], prompt["path"], prompt["sha256"]]:
            text = str(value)
            values.append('"' + text.replace('"', '""') + '"' if "," in text or "\n" in text else text)
        output.append(",".join(values))
    return "\n".join(output) + "\n"


def render_task_index(tasks: list[dict], prefix: str = "") -> str:
    if not tasks:
        return ""
    sorted_tasks = sorted(
        tasks,
        key=lambda item: (item["best_score"] is None, -(item["best_score"] or 0), item["instance_id"]),
    )
    rows = "\n".join(
        f"""
        <tr>
          <td>{index}</td>
          <td><a href="{cell(prefix + str(task["task_path"]))}"><code>{cell(str(task["instance_id"]))}</code></a></td>
          <td>{task["official_generated_tests"] if task["official_generated_tests"] is not None else "pending"}</td>
          <td>{percent(float(task["official_best_score"])) if task["official_best_score"] is not None else "pending"}</td>
          <td>{percent(float(task["best_score"])) if task["best_score"] is not None else "pending"}</td>
          <td>{cell(str(task["best_model"] or "pending"))}</td>
          <td>{task["result_count"]}</td>
          <td>{task["official_result_count"]}</td>
          <td><a href="{cell(str(task["official_task_url"]))}">official</a></td>
        </tr>
        """
        for index, task in enumerate(sorted_tasks, start=1)
    )
    return f"""
    <h2>Task Details</h2>
    <p>Per-task pages show official ProgramBench context, GoalBench rows by model/mode, failed-test evidence, and baseline links. See <a href="{prefix}task-details.html">how to read task pages</a>.</p>
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Task</th><th>Generated tests</th><th>Official best</th><th>Codex best</th><th>Codex model</th><th>Codex rows</th><th>Official rows</th><th>ProgramBench</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """


def official_task_result_rows(task: dict) -> str:
    return "\n".join(
        f"""
        <tr>
          <td>{index}</td>
          <td>{cell(str(row["model"]))}</td>
          <td>{cell(str(row["provider"]))}</td>
          <td>{percent(float(row["score"])) if row["score"] is not None else "n/a"}</td>
          <td>{money(float(row["cost_usd"]))}</td>
          <td>{integer(int(row["calls"]))}</td>
        </tr>
        """
        for index, row in enumerate(task.get("results", []), start=1)
    )


def heat_color(score: float) -> str:
    if score >= 1:
        return "#047857"
    if score >= 0.95:
        return "#d97706"
    if score >= 0.5:
        return "#0f766e"
    if score > 0:
        return "#94a3b8"
    return "#e5e7eb"


def run_version_chip(group: dict) -> str:
    version = str(group.get("run_version", ""))
    return f"version {cell(version)}" if version else "version not recorded"


def run_chips(group: dict) -> str:
    values = [
        cell(str(group["mode"])),
        cell(str(group["host_profile"])),
        cell(run_version_chip(group)),
    ]
    compliance = str(group["compliance"])
    if compliance not in {
        "Codex /goal mini-SWE-compatible no-internet",
        "Codex no-internet ablation",
    }:
        values.insert(1, cell(compliance))
    return "".join(f'<span class="run-chip">{value}</span>' for value in values)


def render_prompt_panel(group: dict) -> str:
    prompt = group.get("prompt", {})
    if not prompt.get("path"):
        return ""
    return f"""
    <section class="prompt-panel">
      <div>
        <h2>Prompt & Config</h2>
        <p class="muted">{cell(str(prompt["summary"]))} The exact prompt is published as a stable artifact and referenced from <code>results.json</code> and <code>results.csv</code>.</p>
      </div>
      <div class="prompt-actions">
        <a class="button primary" href="../../{cell(str(prompt["path"]))}">View prompt</a>
        <a class="button" href="../../data/results.json">results.json</a>
        <a class="button" href="../../data/results.csv">results.csv</a>
      </div>
      <dl>
        <div><dt>Prompt</dt><dd>{cell(str(prompt["title"]))}</dd></div>
        <div><dt>Source</dt><dd><code>{cell(str(prompt["source_path"]))}</code></dd></div>
        <div><dt>SHA-256</dt><dd><code>{cell(str(prompt["sha256"]))}</code></dd></div>
      </dl>
    </section>
    """


def official_run_button(group: dict) -> str:
    url = official_run_url(str(group["model"]))
    return f'<a class="button" href="{cell(url)}">Official ProgramBench baseline</a>' if url else ""


def render_run_detail(group: dict, rows: list[ResultRow]) -> str:
    matching = [
        row
        for row in rows
        if group_key(row)
        == (group["model"], group["agent"], group["mode"], group["compliance"], group.get("run_version", ""))
    ]
    heatmap = "\n".join(
        f'<a class="heat-cell" style="background:{heat_color(row.score)}" title="{cell(row.instance_id)}: {percent(row.score)}" href="../../{task_page_link(row)}"></a>'
        for row in sorted(matching, key=lambda item: item.instance_id)
    )
    table = "\n".join(
        f"""
        <tr>
          <td><a href="../../{task_page_link(row)}"><code>{cell(row.instance_id)}</code></a></td>
          <td>{percent(row.score)}</td>
          <td>{"yes" if row.resolved else "no"}</td>
          <td>{"yes" if row.almost_resolved else "no"}</td>
          <td>{eval_status(row)}</td>
          <td>{row.n_resolved_tests}/{row.n_tests}</td>
          <td>{money(row.estimated_cost_usd)}</td>
          <td>{row.calls}</td>
          <td>{evidence_links(row, "../../")}</td>
        </tr>
        """
        for row in sorted(matching, key=lambda item: item.score, reverse=True)
    )
    official_button = official_run_button(group)
    run_title = f"{group['model']} · {group['mode']} · {SITE_NAME}"
    run_description = (
        f"{group['instances']} evaluated tasks: {percent(group['resolved_rate'])} resolved, "
        f"{percent(group['almost_resolved_rate'])} almost resolved, "
        f"{percent(group['average_pass_rate'])} average pass rate."
    )
    run_path = f"run/{group['slug']}/"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{cell(str(group["model"]))} GoalBench Run</title>
  {social_meta(run_title, run_description, run_path)}
  <link rel="icon" href="../../favicon.svg" type="image/svg+xml">
  <style>
    :root {{
      --ink: #182026;
      --muted: #5b6b78;
      --line: #d9e0e6;
      --soft: #f4f8f6;
      --accent: #0f766e;
      --accent-strong: #0b5f59;
      --warn: #b45309;
      --bad: #be123c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background: linear-gradient(180deg, #fbfcfb 0%, #f6f9f7 360px, #ffffff 361px);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    a {{ color: #075985; text-decoration-thickness: 1px; text-underline-offset: 2px; }}
    header, main {{ max-width: 1180px; margin: 0 auto; padding: 22px 28px; }}
    .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; }}
    .nav-brand {{ display: inline-flex; align-items: center; gap: 10px; color: var(--ink); font-weight: 850; text-decoration: none; }}
    .brand-mark {{ width: 30px; height: 30px; display: block; flex: 0 0 auto; }}
    .nav-links {{ display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }}
    .nav-links a {{ color: #40515c; text-decoration: none; border-radius: 6px; padding: 7px 9px; font-size: 14px; }}
    .nav-links a:hover {{ color: #075985; background: #eef6f3; }}
    .run-hero {{
      padding: 34px 0 28px;
      border-bottom: 1px solid var(--line);
    }}
    .run-eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--accent-strong);
      font-size: 13px;
      font-weight: 850;
      margin: 0 0 10px;
    }}
    h1 {{
      margin: 0;
      max-width: 820px;
      color: var(--ink);
      font-size: clamp(38px, 7vw, 76px);
      line-height: 0.96;
      letter-spacing: 0;
    }}
    h2 {{ margin: 34px 0 8px; font-size: 20px; }}
    p {{ color: var(--muted); line-height: 1.5; }}
    .run-summary {{ max-width: 760px; margin: 14px 0 0; font-size: 17px; }}
    .run-actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 18px; }}
    .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 36px;
      border-radius: 7px;
      border: 1px solid var(--line);
      padding: 8px 12px;
      color: #263640;
      background: #ffffff;
      font-size: 13px;
      font-weight: 750;
      text-decoration: none;
      text-align: center;
      overflow-wrap: anywhere;
    }}
    .button:hover {{ border-color: #9fb4ad; background: #f5faf8; }}
    .button.primary {{ border-color: var(--accent); background: var(--accent); color: #ffffff; }}
    .button.primary:hover {{ background: #115e59; }}
    .run-chips {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }}
    .run-chip {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      border: 1px solid #cbd8d3;
      border-radius: 999px;
      padding: 5px 10px;
      color: #31434d;
      background: #ffffff;
      font-size: 12px;
      font-weight: 700;
    }}
    .run-kpis {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
      gap: 10px;
      margin: 24px 0 10px;
    }}
    .run-kpi {{
      min-height: 112px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: #ffffff;
    }}
    .run-kpi.primary {{ border-color: #b7d8cf; background: #f1fbf7; }}
    .run-kpi span {{ display: block; color: #4e606c; font-size: 12px; font-weight: 800; text-transform: uppercase; }}
    .run-kpi strong {{ display: block; margin-top: 14px; color: var(--ink); font-size: clamp(22px, 3vw, 30px); line-height: 1; }}
    .run-kpi em {{ display: block; margin-top: 8px; color: var(--muted); font-size: 12px; font-style: normal; }}
    .duration-panel, .prompt-panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      background: #ffffff;
      margin: 18px 0;
    }}
    .duration-panel h2, .prompt-panel h2 {{ margin-top: 0; }}
    .duration-stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .duration-stats div, .duration-bucket {{
      min-height: 76px;
      border: 1px solid #e2e9e5;
      border-radius: 7px;
      padding: 10px;
      background: #f8fbfa;
    }}
    .duration-stats span, .duration-bucket span, .prompt-panel dt {{
      display: block;
      color: #4e606c;
      font-size: 11px;
      font-weight: 850;
      text-transform: uppercase;
    }}
    .duration-stats strong, .duration-bucket strong {{
      display: block;
      margin-top: 8px;
      font-size: 20px;
      line-height: 1;
    }}
    .duration-stats em, .duration-bucket em {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      font-style: normal;
    }}
    .duration-buckets {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }}
    .duration-longest {{ margin-bottom: 0; }}
    .prompt-actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin: 12px 0; }}
    .prompt-panel dl {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin: 12px 0 0; }}
    .prompt-panel dl div {{ border: 1px solid #e2e9e5; border-radius: 7px; padding: 10px; background: #f8fbfa; min-width: 0; }}
    .prompt-panel dd {{ margin: 7px 0 0; overflow-wrap: anywhere; }}
    .plot-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px; margin: 14px 0 18px; }}
    .plot-card {{ border: 1px solid var(--line); border-radius: 8px; padding: 14px; background: #ffffff; }}
    .plot {{ width: 100%; height: auto; display: block; }}
    .plot line {{ stroke: var(--line); stroke-width: 1.5; }}
    .plot text {{ fill: var(--muted); font-size: 11px; }}
    .heatmap-wrap {{ border: 1px solid var(--line); border-radius: 8px; padding: 14px; background: #ffffff; }}
    .heatmap {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(16px, 1fr)); gap: 4px; max-width: 860px; }}
    .heat-cell {{ display: block; aspect-ratio: 1; border-radius: 4px; outline: 1px solid rgba(24, 32, 38, 0.06); }}
    .table-wrap {{ border: 1px solid var(--line); border-radius: 8px; overflow-x: auto; background: #ffffff; }}
    table {{ border-collapse: collapse; width: 100%; min-width: 860px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px 12px; text-align: left; font-size: 13px; vertical-align: middle; white-space: nowrap; }}
    td:last-child {{ min-width: 220px; white-space: normal; }}
    th {{ background: var(--soft); color: #33424d; font-weight: 750; }}
    tr:last-child td {{ border-bottom: 0; }}
    tbody tr:hover td {{ background: #fbfdfc; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }}
    .status {{
      display: inline-block;
      min-width: 64px;
      border-radius: 999px;
      padding: 3px 8px;
      font-weight: 750;
      font-size: 12px;
      text-align: center;
      background: var(--soft);
    }}
    .status.resolved {{ color: #065f46; background: #d1fae5; }}
    .status.almost {{ color: var(--warn); background: #fef3c7; }}
    .status.open {{ color: var(--bad); background: #ffe4e6; }}
    .muted {{ color: var(--muted); }}
    @media (max-width: 680px) {{
      header, main {{ padding-left: 16px; padding-right: 16px; }}
      .topbar {{ align-items: flex-start; flex-direction: column; }}
      .nav-links {{ justify-content: flex-start; }}
      .run-kpi {{ min-height: 104px; padding: 12px; }}
      .duration-buckets, .prompt-panel dl {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: clamp(34px, 11vw, 48px); }}
    }}
  </style>
</head>
<body>
  <header>
    <nav class="topbar" aria-label="Primary">
      <a class="nav-brand" href="../../">{brand_slash_svg()}<span>{SITE_NAME}</span></a>
      <div class="nav-links">
        <a href="../../">Leaderboard</a>
        <a href="../../extended/">Extended</a>
        <a href="../../task-details.html">Tasks</a>
        <a href="../../runbook.html">Runbook</a>
        <a href="{GOALBENCH_GITHUB}">GitHub</a>
        <a href="{PROGRAMBENCH_EXTENDED}">ProgramBench</a>
      </div>
    </nav>
    <section class="run-hero">
      <p class="run-eyebrow">Codex <code>/goal</code> run detail</p>
      <h1>{cell(str(group["model"]))}</h1>
      <p class="run-summary">A GoalBench scaffold run against ProgramBench tasks. This page shows the run's measured outcomes, task scores, and public evidence links; it is not an official mini-SWE-agent leaderboard submission.</p>
      <div class="run-chips">{run_chips(group)}</div>
      <div class="run-actions">
        <a class="button primary" href="../../">Back to leaderboard</a>
        {official_button}
      </div>
    </section>
  </header>
  <main>
    {run_metric_cards(group)}
    {render_duration_summary(group["duration"], "../../")}
    {render_prompt_panel(group)}
    {render_score_distribution(matching)}
    <h2>Score by Task</h2>
    <p class="muted">Each cell is one evaluated task instance. Dark green means fully resolved, amber means almost resolved, and muted cells are partial or open.</p>
    <div class="heatmap-wrap"><div class="heatmap">{heatmap}</div></div>
    <h2>Per-Instance Results</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Instance</th><th>Score</th><th>Resolved</th><th>Almost</th><th>Eval</th><th>Tests</th><th>Est. cost</th><th>Calls</th><th>Evidence</th></tr></thead>
        <tbody>{table}</tbody>
      </table>
    </div>
  </main>
</body>
</html>
"""


def evidence_link_path(row: ResultRow) -> str:
    return f"evidence/{row.run_name}/{row.instance_id}/manifest.json"


def read_public_evidence(row: ResultRow, name: str) -> dict:
    path = Path("docs") / "evidence" / row.run_name / row.instance_id / name
    return json.loads(path.read_text()) if path.is_file() else {}


def failed_test_reason(name: str) -> str:
    lowered = name.lower()
    if "version" in lowered:
        return "exact version/output mismatch"
    if "screensaver" in lowered or "interactive" in lowered or "key_" in lowered or "quit" in lowered:
        return "interactive terminal behavior mismatch"
    if "tui" in lowered or "screen" in lowered:
        return "terminal rendering mismatch"
    return "behavioral mismatch"


def render_evidence_highlights(rows: list[ResultRow]) -> str:
    if not rows:
        return ""
    cards = []
    for row in rows:
        summary = read_public_evidence(row, "eval-summary.json")
        if not summary:
            cards.append(
                f"""
      <div class="evidence-card">
        <h3>{cell(model_display(row))} · <code>{cell(version_label(row.run_version))}</code></h3>
        <p class="muted">Public eval evidence has not been exported for this row yet.</p>
      </div>
                """
            )
            continue
        counts = summary.get("status_counts", {})
        non_passed = [test for test in summary.get("failed_tests", []) if test.get("status") != "passed"]
        failures = [test for test in non_passed if test.get("status") != "skipped"]
        first_failures = failures[:6]
        reason_text = "No non-passing behavioral tests were reported."
        if failures:
            reasons = sorted({failed_test_reason(str(test.get("name", ""))) for test in failures})
            reason_text = "Likely miss class: " + ", ".join(reasons) + "."
        elif non_passed:
            reason_text = "Only skipped tests were reported in public eval evidence."
        tests = "\n".join(
            f"""
          <li><code>{cell(str(test.get("branch", "")))}</code> {cell(str(test.get("name", "")))} <span class="muted">({cell(str(test.get("status", "")))})</span></li>
            """
            for test in first_failures
        )
        if not tests:
            tests = '<li class="muted">No failing tests listed.</li>'
        cards.append(
            f"""
      <div class="evidence-card">
        <h3>{cell(model_display(row))} · <code>{cell(version_label(row.run_version))}</code></h3>
        <p>{percent(row.score)} from <strong>{row.n_resolved_tests}/{row.n_tests}</strong> ProgramBench-scored tests. Raw public eval statuses: {", ".join(f"{cell(str(key))}: {cell(str(value))}" for key, value in sorted(counts.items())) or "unavailable"}.</p>
        <p class="muted">{cell(reason_text)}</p>
        <ul>{tests}</ul>
        <p>{evidence_links(row, "../../")}</p>
      </div>
            """
        )
    return f"""
  <h2>Why Scores Differ</h2>
  <p class="muted">Official ProgramBench rows are the public mini-SWE-agent submissions. GoalBench rows are separate Codex <code>/goal</code> submissions. Same model label does not mean the same agent, prompt, tool loop, or generated implementation. A GoalBench row is resolved only when the ProgramBench-scored pass rate is exactly 100%.</p>
  <div class="evidence-grid">
    {"".join(cards)}
  </div>
    """


def render_task_detail(instance_id: str, rows: list[ResultRow], official_tasks: dict) -> str:
    matching = sorted([row for row in rows if row.instance_id == instance_id], key=lambda row: row.score, reverse=True)
    best_score = max((row.score for row in matching), default=None)
    scored_tests = max((row.n_tests for row in matching), default=None)
    official_task_url = f"https://programbench.com/task/{instance_id}/"
    official_task = official_tasks.get(instance_id, {})
    if matching:
        result_rows = "\n".join(
            f"""
        <tr>
          <td>{index}</td>
          <td>{cell(model_display(row))}</td>
          <td><code>{cell(version_label(row.run_version))}</code></td>
          <td>{cell(AGENT_NAME)}</td>
          <td>{cell(mode_label(row))}</td>
          <td>{percent(row.score)}</td>
          <td>{eval_status(row)}</td>
          <td>{row.n_resolved_tests}/{row.n_tests}</td>
          <td>{money(row.estimated_cost_usd)}</td>
          <td>{row.calls}</td>
          <td>{row.wall_clock_seconds / 3600:.2f}h</td>
          <td>{evidence_links(row, "../../")}</td>
        </tr>
        """
            for index, row in enumerate(matching, start=1)
        )
    else:
        result_rows = """
        <tr>
          <td colspan="11">No Codex results published for this task yet.</td>
        </tr>
        """
    task_title = f"{instance_id} · {SITE_NAME}"
    task_description = "GoalBench task detail with ProgramBench context, Codex /goal scores, evidence links, cost, calls, and wall-clock time."
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{cell(instance_id)} ProgramBench Task</title>
  {social_meta(task_title, task_description, f"task/{instance_id}/")}
  <link rel="icon" href="../../favicon.svg" type="image/svg+xml">
  <style>
    :root {{
      --ink: #182026;
      --muted: #5b6b78;
      --line: #d9e0e6;
      --soft: #f4f8f6;
      --accent: #0f766e;
      --accent-strong: #0b5f59;
      --warn: #b45309;
      --bad: #be123c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background: linear-gradient(180deg, #fbfcfb 0%, #f6f9f7 330px, #ffffff 331px);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    a {{ color: #075985; text-decoration-thickness: 1px; text-underline-offset: 2px; }}
    header, main {{ max-width: 1180px; margin: 0 auto; padding: 22px 28px; }}
    .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; }}
    .nav-brand {{ display: inline-flex; align-items: center; gap: 10px; color: var(--ink); font-weight: 850; text-decoration: none; }}
    .brand-mark {{ width: 30px; height: 30px; display: block; flex: 0 0 auto; }}
    .nav-links {{ display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }}
    .nav-links a {{ color: #40515c; text-decoration: none; border-radius: 6px; padding: 7px 9px; font-size: 14px; }}
    .nav-links a:hover {{ color: #075985; background: #eef6f3; }}
    .task-hero {{ padding: 34px 0 28px; border-bottom: 1px solid var(--line); }}
    .task-eyebrow {{ color: var(--accent-strong); font-size: 13px; font-weight: 850; margin: 0 0 10px; }}
    h1 {{ margin: 0; max-width: 920px; color: var(--ink); font-size: clamp(30px, 5vw, 54px); line-height: 1.02; letter-spacing: 0; overflow-wrap: anywhere; }}
    h2 {{ margin: 34px 0 8px; font-size: 22px; }}
    p {{ color: var(--muted); line-height: 1.5; }}
    .task-summary {{ max-width: 840px; margin: 14px 0 0; font-size: 17px; }}
    .task-actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 18px; }}
    .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 36px;
      border-radius: 7px;
      border: 1px solid var(--line);
      padding: 8px 12px;
      color: #263640;
      background: #ffffff;
      font-size: 13px;
      font-weight: 750;
      text-decoration: none;
      text-align: center;
      overflow-wrap: anywhere;
    }}
    .button:hover {{ border-color: #9fb4ad; background: #f5faf8; }}
    .button.primary {{ border-color: var(--accent); background: var(--accent); color: #ffffff; }}
    .button.primary:hover {{ background: #115e59; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }}
    h1 code {{ font-size: clamp(18px, 3vw, 30px); white-space: normal; }}
    .metric-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 24px 0 10px; }}
    .metric {{ min-height: 102px; border: 1px solid var(--line); border-radius: 8px; padding: 14px; background: #ffffff; }}
    .metric strong {{ display: block; margin-bottom: 8px; font-size: clamp(24px, 3vw, 32px); line-height: 1; }}
    .metric span {{ color: var(--muted); font-size: 13px; }}
    .table-wrap {{ border: 1px solid var(--line); border-radius: 8px; overflow-x: auto; background: #ffffff; }}
    table {{ border-collapse: collapse; width: 100%; min-width: 860px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 10px 12px; text-align: left; font-size: 13px; vertical-align: middle; white-space: nowrap; }}
    td:last-child {{ min-width: 220px; white-space: normal; }}
    th {{ background: var(--soft); color: #33424d; font-weight: 750; }}
    tr:last-child td {{ border-bottom: 0; }}
    tbody tr:hover td {{ background: #fbfdfc; }}
    .evidence-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; margin: 16px 0; }}
    .evidence-card {{ border: 1px solid var(--line); border-radius: 8px; padding: 16px; background: #ffffff; }}
    .evidence-card h3 {{ margin: 0 0 8px; font-size: 16px; }}
    .evidence-card ul {{ margin: 8px 0 0; padding-left: 18px; }}
    .evidence-card li {{ margin: 5px 0; font-size: 13px; }}
    .status {{
      display: inline-block;
      min-width: 64px;
      border-radius: 999px;
      padding: 3px 8px;
      font-weight: 750;
      font-size: 12px;
      text-align: center;
      background: var(--soft);
    }}
    .status.resolved {{ color: #065f46; background: #d1fae5; }}
    .status.almost {{ color: var(--warn); background: #fef3c7; }}
    .status.open {{ color: var(--bad); background: #ffe4e6; }}
    .muted {{ color: var(--muted); }}
    @media (max-width: 760px) {{
      header, main {{ padding-left: 16px; padding-right: 16px; }}
      .topbar {{ align-items: flex-start; flex-direction: column; }}
      .nav-links {{ justify-content: flex-start; }}
      .metric-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .metric {{ min-height: 96px; }}
    }}
  </style>
</head>
<body>
  <header>
    <nav class="topbar" aria-label="Primary">
      <a class="nav-brand" href="../../">{brand_slash_svg()}<span>{SITE_NAME}</span></a>
      <div class="nav-links">
        <a href="../../">Leaderboard</a>
        <a href="../../extended/">Extended</a>
        <a href="../../task-details.html">Tasks</a>
        <a href="../../runbook.html">Runbook</a>
        <a href="{GOALBENCH_GITHUB}">GitHub</a>
        <a href="{PROGRAMBENCH_EXTENDED}">ProgramBench</a>
      </div>
    </nav>
    <section class="task-hero">
      <p class="task-eyebrow">ProgramBench task detail</p>
      <h1><code>{cell(instance_id)}</code></h1>
      <p class="task-summary">Task-level results for this Codex <code>/goal</code> scaffold. ProgramBench baseline context is cached from the official task page; Codex scored tests are after active-branch and ignored-test filtering.</p>
      <div class="task-actions">
        <a class="button primary" href="../../">Back to leaderboard</a>
        <a class="button" href="{cell(official_task_url)}">Official ProgramBench task</a>
      </div>
    </section>
  </header>
  <main>
    <div class="metric-grid">
      <div class="metric"><strong>{official_task.get("generated_tests", scored_tests if scored_tests is not None else "pending")}</strong><span>generated behavioral tests</span></div>
      <div class="metric"><strong>{percent(float(official_task["best_score"])) if official_task.get("best_score") is not None else "pending"}</strong><span>official best score</span></div>
      <div class="metric"><strong>{percent(best_score) if best_score is not None else "pending"}</strong><span>Codex best score</span></div>
      <div class="metric"><strong>{len(matching)}</strong><span>Codex result rows</span></div>
    </div>
    <h2>Codex Results by Model</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Model</th><th>Run</th><th>Agent</th><th>Mode</th><th>Score</th><th>Eval</th><th>Tests</th><th>Est. cost</th><th>Calls</th><th>Wall</th><th>Evidence</th></tr></thead>
        <tbody>{result_rows}</tbody>
      </table>
    </div>
    {render_evidence_highlights(matching)}
    <h2>Official ProgramBench Results by Model</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Model</th><th>Provider</th><th>Score</th><th>Cost</th><th>Calls</th></tr></thead>
        <tbody>{official_task_result_rows(official_task) or '<tr><td colspan="6">Official task rows not cached yet.</td></tr>'}</tbody>
      </table>
    </div>
  </main>
</body>
</html>
"""


def render_comparison(groups: list[dict]) -> str:
    if len(groups) < 2:
        return ""
    rows = []
    for left in groups:
        for right in groups:
            if left is right:
                continue
            rows.append(
                f"""
                <tr>
                  <td>{cell(str(left["model"]))}</td>
                  <td>{cell(str(right["model"]))}</td>
                  <td>{percent(left["resolved_rate"] - right["resolved_rate"])}</td>
                  <td>{percent(left["almost_resolved_rate"] - right["almost_resolved_rate"])}</td>
                  <td>{percent(left["average_pass_rate"] - right["average_pass_rate"])}</td>
                </tr>
                """
            )
    return f"""
    <h2>Model Comparison</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Y model</th><th>X model</th><th>Resolved Δ</th><th>Almost Δ</th><th>Avg. pass Δ</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    </div>
    """


def render_repeatability(data: dict, prefix: str = "") -> str:
    repeated = data["repeatability"]
    if not repeated:
        return ""
    summary = data["repeatability_summary"]
    rows = "\n".join(
        f"""
        <tr>
          <td>{index}</td>
          <td><a href="{prefix}task/{cell(str(item["instance_id"]))}/"><code>{cell(str(item["instance_id"]))}</code></a></td>
          <td>{cell(str(item["model"]))}</td>
          <td>{cell(str(item["mode"]))}</td>
          <td>{cell(str(item["compliance"]))}</td>
          <td>{item["attempts"]}</td>
          <td><code>{cell(", ".join(item["versions"]))}</code></td>
          <td>{percent(float(item["score_mean"]))}</td>
          <td>{percent(float(item["score_stdev"]))}</td>
          <td>{percent(float(item["score_delta"]))}</td>
          <td>{money(float(item["cost_mean"]))}</td>
          <td>{money(float(item["cost_stdev"]))}</td>
          <td>{item["calls_mean"]:.1f}</td>
          <td>{item["calls_stdev"]:.1f}</td>
          <td>{item["wall_mean_hours"]:.2f}h</td>
          <td>{item["wall_stdev_hours"]:.2f}h</td>
          <td>{item["resolved_attempts"]}/{item["attempts"]}</td>
          <td>{item["almost_attempts"]}/{item["attempts"]}</td>
        </tr>
        """
        for index, item in enumerate(repeated, start=1)
    )
    return f"""
    <h2>Repeatability / Variance</h2>
    <p>This section appears only when the same task, model, mode, and compliance bucket has results from more than one run version. It keeps repeated sweeps visible instead of collapsing them into one row.</p>
    <div class="cards">
      <section class="summary-card">
        <div class="summary-title">Repeated cells</div>
        <div class="summary-meta">task x model x mode x compliance</div>
        <div class="metric-grid">
          <div><strong>{summary["repeated_cells"]}</strong><span>repeated cells</span></div>
          <div><strong>{summary["attempts"]}</strong><span>attempt rows</span></div>
          <div><strong>{percent(float(summary["average_score_stdev"]))}</strong><span>avg score stdev</span></div>
          <div><strong>{percent(float(summary["max_score_delta"]))}</strong><span>max score delta</span></div>
          <div><strong>{money(float(summary["average_cost_stdev"]))}</strong><span>avg est. cost stdev</span></div>
          <div><strong>{summary["average_calls_stdev"]:.1f}</strong><span>avg calls stdev</span></div>
        </div>
      </section>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Instance</th><th>Model</th><th>Mode</th><th>Compliance</th><th>Attempts</th><th>Versions</th><th>Mean score</th><th>Score stdev</th><th>Score delta</th><th>Mean cost</th><th>Cost stdev</th><th>Mean calls</th><th>Calls stdev</th><th>Mean wall</th><th>Wall stdev</th><th>Resolved</th><th>Almost</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """


def render_empty_state() -> str:
    return """
    <section class="empty-state">
      <div class="section-eyebrow">Clean slate</div>
      <h2>Current full-run evaluation is in progress</h2>
      <p>No Codex <code>/goal</code> result rows are published in this reset yet. When the current run finishes, this page will populate the leaderboard, run details, task pages, plots, and downloadable JSON/CSV artifacts from the freshly evaluated data.</p>
      <div class="mode-grid">
        <div class="mode-card">
          <strong>Headline track</strong>
          <p>GPT-5.5 xhigh with Codex <code>/goal</code>, no internet/source/package lookup, target binary-analysis tools blocked, black-box target access, and the mini-SWE-compatible prompt.</p>
        </div>
        <div class="mode-card">
          <strong>Comparison track</strong>
          <p>GPT-5.5 high uses the same mini-SWE-compatible no-internet scaffold, changing only reasoning effort for high-vs-xhigh comparison.</p>
        </div>
        <div class="mode-card">
          <strong>Local-tools ablation</strong>
          <p>Coming soon. External lookup remains blocked, but local binary-analysis/tracing tools are allowed. Reported separately as non-compliant.</p>
        </div>
      </div>
      <p class="link-row"><a class="button primary" href="extended/">Open extended view</a><a class="button" href="task-details.html">How task pages work</a></p>
    </section>
    """


def render_data_downloads(prefix: str = "") -> str:
    _ = prefix
    return f"""
    <div class="download-strip" aria-label="Report data downloads">
      <div class="download-actions">
        <a class="button primary" href="{SITE_URL}data/results.csv">results.csv</a>
        <a class="button" href="{SITE_URL}data/results.json">results.json</a>
        <a class="button" href="{SITE_URL}data/prompts.json">prompts.json</a>
      </div>
    </div>
    """


def render_data_buttons(prefix: str = "") -> str:
    _ = prefix
    return f"""
      <a class="button" href="{SITE_URL}data/results.csv">results.csv</a>
      <a class="button" href="{SITE_URL}data/results.json">results.json</a>
      <a class="button" href="{SITE_URL}data/prompts.json">prompts.json</a>
    """


def render_prompt_catalog(prompts: list[dict], prefix: str = "") -> str:
    cards = "\n".join(
        f"""
        <a class="prompt-card" href="{cell(prefix + str(prompt["path"]))}">
          <span>{cell(str(prompt["title"]))}</span>
          <strong>{cell(str(prompt["mode"]))}</strong>
          <em>{cell(str(prompt["summary"]))}</em>
        </a>
        """
        for prompt in prompts
    )
    return f"""
    <section class="section">
      <div class="section-eyebrow">Prompt artifacts</div>
      <div class="section-head">
        <div>
          <h2>Prompts by Mode</h2>
          <p>Exact Codex <code>/goal</code> prompts are published as first-class artifacts so each result row can be traced back to its scaffold.</p>
        </div>
      </div>
      <div class="prompt-grid">{cards}</div>
    </section>
    """


def render_duration_overview(data: dict, prefix: str = "") -> str:
    if not data["groups"]:
        return ""
    group = data["groups"][0]
    return f"""
    <section class="section">
      <div class="section-eyebrow">Latency</div>
      {render_duration_summary(group["duration"], prefix)}
    </section>
    """


def render_tweet_embed() -> str:
    return """
    <aside class="tweet-card" aria-label="Request tweet">
      <blockquote class="twitter-tweet"><p lang="en" dir="ltr">Would love to see the performance of 5.5 with /goal on ProgramBench!</p>&mdash; Noam Brown (@polynoamial) <a href="https://twitter.com/polynoamial/status/2054258259280994341?ref_src=twsrc%5Etfw">May 12, 2026</a></blockquote>
    </aside>
    """


def render_task_details_page() -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Task Details · {SITE_NAME}</title>
  {social_meta(f"Task Details · {SITE_NAME}", "How to read GoalBench per-task pages: ProgramBench context, Codex /goal rows, evidence links, scores, cost, calls, and latency.", "task-details.html")}
  <link rel="icon" href="favicon.svg" type="image/svg+xml">
  <style>
    :root {{
      color-scheme: light;
      --ink: #182026;
      --muted: #61707d;
      --line: #d9e0e6;
      --soft: #f5f7f8;
      --accent: #0f766e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: #f7faf9;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      background: #fbfdfb;
      padding: 18px max(24px, calc((100vw - 980px) / 2)) 30px;
    }}
    main {{ max-width: 980px; margin: 0 auto; padding: 24px; }}
    .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; margin-bottom: 28px; font-size: 14px; }}
    .nav-brand {{ display: inline-flex; align-items: center; gap: 10px; color: var(--ink); font-weight: 850; text-decoration: none; }}
    .brand-mark {{ width: 30px; height: 30px; display: block; flex: 0 0 auto; }}
    .nav-links {{ display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }}
    .nav-links a {{ color: #40515c; text-decoration: none; border-radius: 6px; padding: 7px 9px; }}
    .nav-links a:hover {{ color: #075985; background: #eef6f3; }}
    a {{ color: #075985; }}
    h1 {{ margin: 0 0 8px; font-size: clamp(34px, 5vw, 48px); line-height: 1.05; letter-spacing: 0; }}
    h2 {{ margin: 30px 0 10px; font-size: 19px; letter-spacing: 0; }}
    p, li {{ color: var(--muted); line-height: 1.55; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }}
    .panel {{ border: 1px solid var(--line); border-radius: 10px; padding: 18px; background: #ffffff; margin: 16px 0; }}
    .steps {{ display: grid; gap: 10px; margin-top: 16px; }}
    .step {{ border: 1px solid var(--line); border-left: 4px solid var(--accent); border-radius: 8px; padding: 12px 14px; background: #ffffff; }}
    @media (max-width: 760px) {{
      header, main {{ padding-left: 16px; padding-right: 16px; }}
      .topbar {{ align-items: flex-start; flex-direction: column; }}
      .nav-links {{ justify-content: flex-start; }}
    }}
  </style>
</head>
<body>
  <header>
    <nav class="topbar" aria-label="Primary">
      <a class="nav-brand" href="./">{brand_slash_svg()}<span>{SITE_NAME}</span></a>
      <div class="nav-links">
        <a href="./">Leaderboard</a>
        <a href="extended/">Extended</a>
        <a href="task-details.html">Tasks</a>
        <a href="runbook.html">Runbook</a>
        <a href="{GOALBENCH_GITHUB}">GitHub</a>
        <a href="{PROGRAMBENCH_HOME}">ProgramBench</a>
      </div>
    </nav>
    <h1>Task Details</h1>
    <p>Task pages mirror ProgramBench's per-task view for this Codex <code>/goal</code> scaffold: scored behavioral tests, best score, results by model/mode, and links to sanitized evidence.</p>
  </header>
  <main>
    <section class="panel">
      <p>Pending rows are full-run targets waiting for Codex results. Once a task is evaluated, its page shows the GoalBench score beside cached official ProgramBench task context, plus links to the official ProgramBench task page for baseline comparison.</p>
    </section>
    <h2>What Each Task Page Shows</h2>
    <div class="steps">
      <div class="step"><strong>Official context.</strong> Generated test count, official best score, and official model rows are cached from ProgramBench public task pages.</div>
      <div class="step"><strong>GoalBench results.</strong> Each Codex <code>/goal</code> result is shown by run version, model, inference mode, score, evaluated tests, estimated cost, calls, and wall-clock time.</div>
      <div class="step"><strong>Why scores differ.</strong> When public evidence exists, task pages list failed test names and a compact miss-class summary so near-solves like <code>cmatrix</code> are explainable without opening raw JSON.</div>
      <div class="step"><strong>Evidence links.</strong> Public artifacts include sanitized eval summaries, public eval JSON, usage audit, and manifest. Raw Codex session logs and submission tarballs remain local by default.</div>
    </div>
    <h2>Metric Contract</h2>
    <p>Resolved means the ProgramBench behavioral test pass rate is exactly 100%. Almost resolved follows ProgramBench's public threshold of at least 95%. Scores are computed with ProgramBench's own evaluation summary logic after active-branch and ignored-test filtering.</p>
    <h2>Scope</h2>
    <p>GoalBench is not the official mini-SWE-agent leaderboard. It is a scaffold measurement for Codex <code>/goal</code> on the same ProgramBench task family, with modes and compliance labels shown explicitly.</p>
  </main>
</body>
</html>
"""


def render_results_sections(data: dict, instances: list[ResultRow]) -> str:
    if not instances:
        return f"""
    {render_empty_state()}
    {render_pending_charts()}
    {render_task_index(data["tasks"], "../")}
    """
    return f"""
    <div class="cards">
      {"".join(render_summary_cards(summary_title(group), group) for group in data["groups"])}
    </div>

    {render_data_downloads("../")}
    {render_duration_overview(data, "../")}

    <h2>Score by Model × Task</h2>
    <div class="score-matrix">
      {"".join(f'<a class="heat-cell" style="background:{heat_color(row.score)}" title="{cell(row.instance_id)}: {percent(row.score)}" href="{task_page_link(row, "../")}"></a>' for row in sorted(instances, key=lambda row: (model_display(row), row.instance_id)))}
    </div>

    {render_score_distribution(instances)}

    {render_efficiency_plots(instances)}

    <h2>Extended Results</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Model</th><th>Run</th><th>Agent</th><th>Resolved</th><th>Almost</th><th>Avg. est. cost</th><th>Avg. calls</th><th>Avg /goal</th><th>Max /goal</th></tr></thead>
        <tbody>{render_leaderboard(data["groups"], "../")}</tbody>
      </table>
    </div>
    <p>Columns mirror ProgramBench's extended leaderboard shape while adding the latency fields needed for Codex <code>/goal</code>: average and max wall-clock session duration from launch to packaged submission. Run versions keep repeated same-config sweeps separate instead of silently merging attempts.</p>

    <h2>Run Disclosures</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Model</th><th>Run</th><th>Mode</th><th>Compliance</th><th>Host profile</th><th>Tasks</th><th>Avg. pass</th><th>Prompt</th><th>Avg /goal</th><th>Max /goal</th><th>Total wall</th></tr></thead>
        <tbody>{render_disclosures(data["groups"])}</tbody>
      </table>
    </div>
    <p>These disclosure fields make scaffold differences explicit: prompt, compliance label, host size, per-session latency, and total wall-clock sum. Rows labeled smaller VM are Codex <code>/goal</code> scaffold experiments on the disclosed runner size.</p>

    {render_comparison(data["groups"])}

    {render_repeatability(data, "../")}

    {render_task_index(data["tasks"], "../")}

    <h2>Per-Instance Results</h2>
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Instance</th><th>Run</th><th>Mode</th><th>Model</th><th>Compliance</th><th>Status</th><th>Eval</th><th>Score</th><th>Tests</th><th>Est. cost</th><th>Calls</th><th>Wall</th><th>Host</th><th>Docker</th><th>Evidence</th></tr></thead>
        <tbody>{render_instances(instances, "../")}</tbody>
      </table>
    </div>
    """


def render_home_results(data: dict, instances: list[ResultRow]) -> str:
    if not instances:
        return render_empty_state()
    return f"""
    <section class="section leaderboard-section">
      <div class="section-eyebrow">Leaderboard</div>
      <div class="section-head">
        <div>
          <h2>Current Results</h2>
        </div>
        <div class="section-actions" aria-label="Current results actions">
          <a class="button primary" href="extended/">See extended results</a>
        </div>
      </div>
      <div class="table-wrap priority-table">
        <table>
          <thead><tr><th>#</th><th>Model</th><th>Run</th><th>Agent</th><th>Resolved</th><th>Almost</th><th>Avg. est. cost</th><th>Avg. calls</th><th>Avg /goal</th><th>Max /goal</th></tr></thead>
          <tbody>{render_leaderboard(data["groups"])}</tbody>
        </table>
      </div>
    </section>

    <section class="section">
      <div class="section-eyebrow">Run summary</div>
      <div class="cards">
        {"".join(render_summary_cards(summary_title(group), group) for group in data["groups"])}
      </div>
    </section>

    {render_duration_overview(data)}
    """


def render_html(data: dict, extended: bool = False) -> str:
    result_fields = {field.name for field in fields(ResultRow)}
    instances = [
        ResultRow(**{key: value for key, value in row.items() if key in result_fields}) for row in data["rows"]
    ]
    nav = f"""
    <nav class="topbar" aria-label="Primary">
      <a class="nav-brand" href="./">{brand_slash_svg()}<span>{SITE_NAME}</span></a>
      <div class="nav-links">
        <a href="./">Leaderboard</a>
        <a href="extended/">Extended</a>
        <a href="task-details.html">Tasks</a>
        <a href="runbook.html">Runbook</a>
        <a href="{GOALBENCH_GITHUB}">GitHub</a>
        <a href="{PROGRAMBENCH_EXTENDED}">ProgramBench</a>
      </div>
    </nav>
    """
    title = f"Extended Results · {SITE_NAME}" if extended else SITE_NAME
    question = (
        "GoalBench extended results"
        if extended
        else f'<span>Can</span><span class="codex-mention">{codex_logo_img("codex-mention-mark")}Codex <code>/goal</code></span><span>rebuild programs from scratch?</span>'
    )
    heading = "Extended Results" if extended else "GoalBench"
    hero_copy = (
        "Explore Codex <code>/goal</code> results by model, mode, task, cost, calls, and latency. "
        "Each row is a separate scaffold run evaluated with ProgramBench's behavioral tests."
        if extended
        else "Given only a compiled binary and its documentation, the agent must architect and implement a replacement CLI that reproduces the original program's behavior. We score each submission with ProgramBench's behavioral tests."
    )
    body = render_results_sections(data, instances) if extended else render_home_results(data, instances)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  {social_meta(title, path="extended/" if extended else "")}
  <link rel="icon" href="favicon.svg" type="image/svg+xml">
  <style>
    :root {{
      color-scheme: light;
      --ink: #182026;
      --muted: #61707d;
      --line: #d9e0e6;
      --soft: #f5f7f8;
      --accent: #0f766e;
      --accent-strong: #115e59;
      --warn: #b45309;
      --bad: #9f1239;
      --gold: #f6c453;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: #f7faf9;
    }}
    header {{
      border-bottom: 1px solid #dfe7e2;
      background: #fbfdfb;
      padding: 18px max(24px, calc((100vw - 1180px) / 2)) 34px;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 28px 24px 68px;
    }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 34px;
      font-size: 14px;
    }}
    .nav-brand {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      color: var(--ink);
      font-weight: 850;
      text-decoration: none;
    }}
    .nav-links {{ display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }}
    .nav-links a {{
      color: #40515c;
      text-decoration: none;
      border-radius: 6px;
      padding: 7px 9px;
    }}
    .nav-links a:hover {{ color: #075985; background: #eef6f3; }}
    h1 {{ margin: 0 auto 10px; font-size: clamp(36px, 5vw, 56px); line-height: 1.03; letter-spacing: 0; max-width: 820px; }}
    h2 {{ margin: 0 0 10px; font-size: 21px; letter-spacing: 0; }}
    h3 {{ margin: 0 0 10px; font-size: 14px; letter-spacing: 0; }}
    p {{ color: var(--muted); line-height: 1.5; max-width: 900px; }}
    .brand-mark {{ width: 30px; height: 30px; display: block; flex: 0 0 auto; }}
    .hero {{
      max-width: 900px;
      margin: 0 auto;
      text-align: center;
    }}
    .hero-copy {{ font-size: 18px; max-width: 790px; margin: 0 auto; }}
    .question {{
      display: flex;
      align-items: center;
      justify-content: center;
      flex-wrap: wrap;
      gap: 0.34em;
      color: var(--accent-strong);
      margin: 0 auto 14px;
      font-size: clamp(22px, 2.8vw, 32px);
      line-height: 1.12;
      font-weight: 850;
      max-width: 820px;
    }}
    .question > span {{ display: inline-flex; align-items: center; }}
    .codex-mention {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 0.05em 0.18em;
      border-radius: 8px;
      background: #edf7f3;
      white-space: nowrap;
    }}
    .question code {{ font-size: 0.78em; }}
    .codex-mention-mark {{ width: 22px; height: 22px; display: inline-block; flex: 0 0 auto; }}
    .pill-row {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; margin-top: 18px; }}
    .pill {{
      border: 1px solid var(--line);
      background: #ffffff;
      padding: 5px 9px;
      border-radius: 6px;
      font-size: 13px;
      color: var(--muted);
    }}
    .section {{
      margin: 0 0 28px;
      padding: 22px;
      border: 1px solid #e1e8e4;
      border-radius: 10px;
      background: #ffffff;
      box-shadow: 0 1px 0 rgba(16, 32, 29, 0.03);
    }}
    .section.compact {{ padding: 18px; }}
    .section-eyebrow {{
      margin: 0 0 8px;
      color: var(--accent-strong);
      font-size: 12px;
      font-weight: 850;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .section-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 14px;
    }}
    .section-head > div:first-child {{
      min-width: 0;
      max-width: 720px;
    }}
    .section-head p {{ margin: 0; overflow-wrap: anywhere; }}
    .section-actions {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      flex: 0 1 auto;
      flex-wrap: wrap;
      max-width: min(100%, 520px);
    }}
    .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 36px;
      border-radius: 7px;
      border: 1px solid var(--line);
      padding: 8px 12px;
      color: #263640;
      background: #ffffff;
      font-size: 13px;
      font-weight: 750;
      text-decoration: none;
      text-align: center;
      overflow-wrap: anywhere;
      max-width: 100%;
    }}
    .button:hover {{ border-color: #9fb4ad; background: #f5faf8; }}
    .button.primary {{ border-color: #0f766e; background: #0f766e; color: #ffffff; }}
    .button.primary:hover {{ background: #115e59; }}
    .link-row {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 12px; }}
    .download-strip {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      margin: 4px 0 16px;
      padding: 0;
    }}
    .download-actions {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      flex-wrap: wrap;
      flex: 0 0 auto;
    }}
    .prompt-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 10px;
    }}
    .prompt-card {{
      display: block;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 13px;
      color: var(--ink);
      background: #fbfcfc;
      text-decoration: none;
    }}
    .prompt-card:hover {{ border-color: #9fb4ad; background: #f5faf8; }}
    .prompt-card span {{ display: block; font-weight: 800; }}
    .prompt-card strong {{ display: block; margin-top: 7px; color: var(--accent-strong); font-size: 12px; }}
    .prompt-card em {{ display: block; margin-top: 7px; color: var(--muted); font-size: 13px; line-height: 1.4; font-style: normal; }}
    .duration-panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      background: #ffffff;
      margin: 0;
    }}
    .duration-panel h2 {{ margin-top: 0; }}
    .duration-stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .duration-stats div, .duration-bucket {{
      min-height: 76px;
      border: 1px solid #e2e9e5;
      border-radius: 7px;
      padding: 10px;
      background: #f8fbfa;
    }}
    .duration-stats span, .duration-bucket span {{
      display: block;
      color: #4e606c;
      font-size: 11px;
      font-weight: 850;
      text-transform: uppercase;
    }}
    .duration-stats strong, .duration-bucket strong {{
      display: block;
      margin-top: 8px;
      font-size: 20px;
      line-height: 1;
    }}
    .duration-stats em, .duration-bucket em {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      font-style: normal;
    }}
    .duration-buckets {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }}
    .duration-longest {{ margin-bottom: 0; }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
      margin-top: 0;
    }}
    .summary-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      background: #fff;
    }}
    .summary-title {{ font-weight: 700; margin-bottom: 4px; }}
    .summary-meta {{ color: var(--muted); font-size: 13px; margin-bottom: 12px; }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }}
    .metric-grid div {{
      background: var(--soft);
      border-radius: 6px;
      padding: 10px;
      min-height: 62px;
    }}
    .metric-grid strong {{ display: block; font-size: 19px; }}
    .metric-grid span {{ display: block; color: var(--muted); font-size: 12px; margin-top: 4px; }}
    .table-wrap {{
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow-x: auto;
      background: #fff;
    }}
    .priority-table {{ border-color: #cbd8d3; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 820px;
    }}
    th, td {{
      text-align: left;
      border-bottom: 1px solid var(--line);
      padding: 10px 12px;
      font-size: 13px;
      vertical-align: middle;
    }}
    th {{ background: var(--soft); color: #33424d; font-weight: 700; }}
    tr:last-child td {{ border-bottom: 0; }}
    tbody tr:hover td {{ background: #fbfdfc; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }}
    .status {{
      display: inline-block;
      min-width: 68px;
      border-radius: 999px;
      padding: 3px 8px;
      font-weight: 700;
      font-size: 12px;
      text-align: center;
      background: var(--soft);
    }}
    .status.resolved {{ color: #065f46; background: #d1fae5; }}
    .status.almost {{ color: var(--warn); background: #fef3c7; }}
    .status.open {{ color: var(--bad); background: #ffe4e6; }}
    .empty-state {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 18px;
      margin: 20px 0;
    }}
    .empty-state h2 {{ margin-top: 0; }}
    .mode-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 10px;
      margin: 14px 0 18px;
    }}
    .mode-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fbfcfc;
    }}
    .mode-card strong {{ display: block; margin-bottom: 6px; }}
    .mode-card p {{ margin: 0; font-size: 13px; }}
    .method-notes-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(280px, 420px);
      gap: 28px;
      align-items: start;
    }}
    .method-notes-copy h2 {{ margin-top: 0; }}
    .method-notes-copy p:last-child {{ margin-bottom: 0; }}
    .tweet-card {{
      display: flex;
      justify-content: center;
      justify-self: end;
      width: 100%;
      max-width: 420px;
      min-height: 0;
      overflow: hidden;
      border-left: 3px solid var(--accent);
      padding: 2px 0 2px 18px;
    }}
    .tweet-card .twitter-tweet {{ margin: 0 !important; max-width: 100% !important; }}
    .tweet-card blockquote {{
      color: var(--text);
      font-size: 15px;
      line-height: 1.55;
    }}
    .plot-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
      margin: 14px 0 18px;
    }}
    .plot-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fff;
    }}
    .plot {{
      width: 100%;
      height: auto;
      display: block;
    }}
    .plot line {{ stroke: var(--line); stroke-width: 1.5; }}
    .plot text {{ fill: var(--muted); font-size: 11px; }}
    .score-matrix {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(12px, 1fr));
      gap: 3px;
      max-width: 920px;
      margin: 14px 0 18px;
    }}
    .heat-cell {{
      display: block;
      aspect-ratio: 1;
      border-radius: 3px;
      background: #e5e7eb;
    }}
    .heat-cell.pending {{
      background: linear-gradient(135deg, #f5f7f8 25%, #e5e7eb 25%, #e5e7eb 50%, #f5f7f8 50%, #f5f7f8 75%, #e5e7eb 75%);
      background-size: 8px 8px;
    }}
    a {{ color: #075985; }}
    @media (max-width: 760px) {{
      .topbar {{ align-items: flex-start; flex-direction: column; margin-bottom: 26px; }}
      .nav-links {{ justify-content: flex-start; }}
      .section-head {{ align-items: flex-start; flex-direction: column; }}
      .section-actions {{ justify-content: flex-start; width: 100%; }}
      .download-strip {{ justify-content: flex-start; }}
      .download-actions {{ justify-content: flex-start; }}
      .duration-buckets {{ grid-template-columns: 1fr; }}
      .method-notes-grid {{ grid-template-columns: 1fr; gap: 18px; }}
      .tweet-card {{ justify-self: stretch; max-width: 550px; }}
      .metric-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      header, main {{ padding-left: 16px; padding-right: 16px; }}
    }}
  </style>
</head>
<body>
  <header>
    {nav}
    <div class="hero">
      <div>
        <p class="question">{question}</p>
        <h1>{heading}</h1>
        <p class="hero-copy">{hero_copy}</p>
      </div>
    </div>
    <div class="pill-row">
      <span class="pill">Generated {cell(display_timestamp(data["generated_at"]))}</span>
      <span class="pill">{evaluated_instances_label(data["sample_instances"])}</span>
      <span class="pill">{PROGRAMBENCH_TASKS} tasks in full ProgramBench</span>
      <span class="pill">Sorted Resolved → Almost → Avg. pass</span>
    </div>
  </header>
  <main>
    {body}

    <section class="section">
    <h2>Official Baseline Context</h2>
    <p>For orientation only. ProgramBench's public extended table reports mini-SWE-agent over 200 tasks, sorted by resolved, almost-resolved, then average pass rate.</p>
    <div class="table-wrap">
      <table>
        <thead><tr><th>#</th><th>Model</th><th>Agent</th><th>Resolved</th><th>Almost</th><th>Avg. cost</th><th>Avg. calls</th></tr></thead>
        <tbody>{render_baselines(data["baselines"])}</tbody>
      </table>
    </div>
    <p>GPT-5.5 baseline rows link to ProgramBench's official run-detail pages for total cost, total calls, distribution plots, and all 200 per-instance results.</p>
    </section>

    <section class="section">
      <div class="method-notes-grid">
        <div class="method-notes-copy">
          <h2>Method Notes</h2>
          <p>GoalBench reports separate Codex <code>/goal</code> runs on ProgramBench tasks; these are not official mini-SWE-agent leaderboard submissions. Resolved means ProgramBench's filtered behavioral pass rate is exactly 100%, and almost resolved means at least 95%.</p>
          <p>The headline run is the closest GoalBench parity attempt: GPT-5.5 xhigh, strict no-internet enforcement, wrapper-only black-box target access, and a shorter mini-SWE-style prompt. The stricter <code>no-internet</code> scaffold adds an explicit behavior-audit prompt; <code>no-internet-local-tools</code> is a coming non-comparable ablation with local binary-analysis tools allowed.</p>
          <p>The public table is scoped to the latest published result set. Cost is estimated from Codex token logs, not billing. See <a href="task-details.html">Task Details</a> and the <a href="runbook.html">runbook</a> for scoring, evidence, egress, and setup details. Sources: <a href="https://programbench.com/extended/">ProgramBench extended results</a> and <a href="https://programbench.com/run/gpt-5-5-xhigh/">GPT 5.5 xhigh run detail</a>.</p>
        </div>
        {render_tweet_embed()}
      </div>
    </section>
  </main>
  <script async src="https://platform.twitter.com/widgets.js" charset="utf-8"></script>
</body>
</html>
"""


def build(args: argparse.Namespace) -> None:
    rows = [row for path in args.results_csv for row in read_results(Path(path).expanduser())]
    output_dir = Path(args.output_dir).expanduser()
    target_ids = read_target_ids(Path(args.target_set).expanduser())
    official_tasks = load_task_baselines(output_dir)
    if args.clean_output:
        for generated in (
            output_dir / "assets",
            output_dir / "run",
            output_dir / "task",
            output_dir / "prompt",
            output_dir / "official-run",
            output_dir / "extended",
        ):
            if generated.exists():
                shutil.rmtree(generated)
        for generated in (
            output_dir / "data" / "results.json",
            output_dir / "data" / "results.csv",
            output_dir / "data" / "prompts.json",
            output_dir / "data" / "programbench-run-baselines.json",
        ):
            if generated.exists():
                generated.unlink()
    if args.refresh_baselines:
        refresh_baselines(output_dir)
    repeated = repeatability_groups(rows)
    prompts = prompt_records()
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_instances": len(rows),
        "programbench_tasks": PROGRAMBENCH_TASKS,
        "groups": result_groups(rows),
        "tasks": task_groups(rows, target_ids, official_tasks),
        "repeatability": repeated,
        "repeatability_summary": repeatability_summary(repeated),
        "rows": [row_to_dict(row) for row in rows],
        "prompts": prompts,
        "baselines": load_baselines(output_dir),
    }
    (output_dir / "data").mkdir(parents=True, exist_ok=True)
    write_support_files(output_dir)
    (output_dir / "data" / "results.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    (output_dir / "data" / "results.csv").write_text(render_csv(rows))
    (output_dir / "data" / "prompts.json").write_text(json.dumps(prompts, indent=2, sort_keys=True) + "\n")
    write_html(output_dir / "index.html", render_html(data))
    (output_dir / "extended").mkdir(parents=True, exist_ok=True)
    write_html(output_dir / "extended" / "index.html", render_html(data, extended=True))
    write_html(output_dir / "task-details.html", render_task_details_page())
    write_html(output_dir / RUNBOOK_PAGE, render_doc_page(Path("docs/runbook.md"), "Runbook", RUNBOOK_PAGE))
    for group in data["groups"]:
        run_dir = output_dir / "run" / str(group["slug"])
        run_dir.mkdir(parents=True, exist_ok=True)
        write_html(run_dir / "index.html", render_run_detail(group, rows))
    for prompt in prompts:
        prompt_dir = output_dir / "prompt" / str(prompt["slug"])
        prompt_dir.mkdir(parents=True, exist_ok=True)
        write_html(prompt_dir / "index.html", render_prompt_page(prompt))
    for task in data["tasks"]:
        task_dir = output_dir / "task" / str(task["instance_id"])
        task_dir.mkdir(parents=True, exist_ok=True)
        write_html(task_dir / "index.html", render_task_detail(str(task["instance_id"]), rows, official_tasks))
    print(output_dir / "index.html")
    print(output_dir / "data" / "results.json")
    print(output_dir / "data" / "results.csv")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the static GoalBench report site")
    parser.add_argument("results_csv", nargs="*")
    parser.add_argument("--output-dir", default="docs")
    parser.add_argument("--target-set", default="target_sets/all_tasks.txt")
    parser.add_argument("--clean-output", action="store_true")
    parser.add_argument(
        "--refresh-baselines",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fetch latest ProgramBench public baseline rows before rendering.",
    )
    build(parser.parse_args())


if __name__ == "__main__":
    main()
