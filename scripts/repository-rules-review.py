#!/usr/bin/env python3
"""Review PR diffs against REPOSITORY_RULES.md using a delta-scoped LLM check.

Local API key loading uses the ``python-dotenv`` library. Install dev helpers with:

    python3 -m pip install -r scripts/requirements-dev.txt

When ``.env`` exists at the repository root it is loaded automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "REPOSITORY_RULES.md"
PROMPT_PATH = ROOT / "ai" / "prompts" / "repository-rules-review.md"
PROMPT_VERSION = "1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_API_URL = "https://api.deepseek.com/v1/chat/completions"
MAX_DIFF_CHARS = 120_000
MAX_FILE_DIFF_CHARS = 40_000

ALWAYS_SECTIONS = frozenset(
    {
        "Public Surface Discipline",
        "Work Logs And Design Records",
        "No Ad Hoc Fixes",
    }
)

SECTION_TRIGGERS: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    (
        re.compile(r"(^|/)ad/|linearize|transpose_rule|autodiff"),
        frozenset(
            {
                "Rule Source Of Truth",
                "AD Rule Coverage",
                "Oracle Gate",
            }
        ),
    ),
    (
        re.compile(r"tenferro-cpu/|/kernel/|strided"),
        frozenset({"Performance And Layout Rules", "Unsafe Code Boundary"}),
    ),
    (
        re.compile(r"tenferro-gpu/|cubecl|cuda|cutensor|cublas"),
        frozenset({"Performance And Layout Rules", "Unsafe Code Boundary"}),
    ),
    (
        re.compile(r"tenferro-einsum/|tenferro-linalg/|tenferro-fft/|ext/"),
        frozenset({"Standard Extension Boundary", "Wrapper DRY And Codegen"}),
    ),
    (
        re.compile(r"/tests/|_tests\.rs$|tests/"),
        frozenset({"Unit Test Organization", "AD Rule Coverage"}),
    ),
    (
        re.compile(r"^docs/worklogs/"),
        frozenset({"Work Logs And Design Records"}),
    ),
    (
        re.compile(r"^docs/design/"),
        frozenset({"Work Logs And Design Records"}),
    ),
    (
        re.compile(r"\.md$|\.qmd$|^README|^docs/"),
        frozenset(
            {
                "Public Surface Drift",
                "Documentation Policy",
                "Naming Style",
            }
        ),
    ),
    (
        re.compile(r"\.rs$"),
        frozenset(
            {
                "File Organization",
                "Public API Convention",
                "Generic Over Scalar Type",
            }
        ),
    ),
)


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str
    rule_section: str
    file: str
    line: int | None
    summary: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "rule_section": self.rule_section,
            "file": self.file,
            "line": self.line,
            "summary": self.summary,
            "detail": self.detail,
        }


def run_git(args: list[str], cwd: Path = ROOT) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def changed_files(base: str, head: str, *, worktree: bool = False) -> list[str]:
    if worktree:
        output = run_git(["diff", "--name-only", base])
    else:
        output = run_git(["diff", "--name-only", f"{base}...{head}"])
    return [line.strip() for line in output.splitlines() if line.strip()]


def unified_diff(base: str, head: str, *, worktree: bool = False) -> str:
    if worktree:
        return run_git(["diff", "--unified=3", base])
    return run_git(["diff", "--unified=3", f"{base}...{head}"])


def per_file_diffs(
    base: str,
    head: str,
    files: list[str],
    *,
    worktree: bool = False,
) -> dict[str, str]:
    diffs: dict[str, str] = {}
    for path in files:
        if worktree:
            diff = run_git(["diff", "--unified=3", base, "--", path])
        else:
            diff = run_git(["diff", "--unified=3", f"{base}...{head}", "--", path])
        if diff.strip():
            diffs[path] = diff
    return diffs


def configure_dotenv(*, explicit: Path | None, skip: bool) -> None:
    """Load environment variables with python-dotenv."""
    if skip:
        return

    path = explicit if explicit is not None else ROOT / ".env"
    if not path.is_file():
        if explicit is not None:
            print(f"dotenv file not found: {path}", file=sys.stderr)
            raise SystemExit(1)
        return

    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        print(
            "python-dotenv is required to load .env; install with: "
            "python3 -m pip install -r scripts/requirements-dev.txt",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    load_dotenv(path, override=False)


def parse_repository_rules_sections(path: Path = RULES_PATH) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    current_title: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines():
        if line.startswith("## "):
            if current_title is not None:
                sections[current_title] = "\n".join(current_lines).strip()
            current_title = line.removeprefix("## ").strip()
            current_lines = [line]
            continue
        if current_title is not None:
            current_lines.append(line)

    if current_title is not None:
        sections[current_title] = "\n".join(current_lines).strip()
    return sections


def select_rule_sections(files: list[str]) -> list[str]:
    selected = set(ALWAYS_SECTIONS)
    for path in files:
        for pattern, section_names in SECTION_TRIGGERS:
            if pattern.search(path):
                selected.update(section_names)
    return sorted(selected)


def build_rules_payload(section_names: list[str]) -> str:
    sections = parse_repository_rules_sections()
    chunks: list[str] = []
    for name in section_names:
        body = sections.get(name)
        if body:
            chunks.append(body)
    if not chunks:
        return sections.get("Public Surface Discipline", "")
    return "\n\n".join(chunks)


def added_lines_by_file(diff_text: str) -> dict[str, set[int]]:
    result: dict[str, set[int]] = {}
    current_file: str | None = None
    new_line = 0

    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            raw = line.removeprefix("+++ b/").removeprefix("+++ ")
            if raw != "/dev/null":
                current_file = raw
                result.setdefault(current_file, set())
            continue
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            new_line = int(match.group(1)) if match else 0
            continue
        if current_file is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            result[current_file].add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif line.startswith(" "):
            new_line += 1

    return result


def split_diff_chunks(file_diffs: dict[str, str]) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for path in sorted(file_diffs):
        piece = file_diffs[path]
        if len(piece) > MAX_FILE_DIFF_CHARS:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            for start in range(0, len(piece), MAX_FILE_DIFF_CHARS):
                chunks.append(piece[start : start + MAX_FILE_DIFF_CHARS])
            continue

        if current_len + len(piece) > MAX_DIFF_CHARS and current:
            chunks.append("\n".join(current))
            current = [piece]
            current_len = len(piece)
        else:
            current.append(piece)
            current_len += len(piece)

    if current:
        chunks.append("\n".join(current))
    return chunks


def parse_findings(raw: Any) -> tuple[str, list[Finding]]:
    if not isinstance(raw, dict):
        raise ValueError("model response must be a JSON object")

    verdict = raw.get("verdict")
    if verdict not in {"pass", "fail"}:
        raise ValueError("verdict must be 'pass' or 'fail'")

    findings_raw = raw.get("findings", [])
    if not isinstance(findings_raw, list):
        raise ValueError("findings must be a list")

    findings: list[Finding] = []
    for index, item in enumerate(findings_raw):
        if not isinstance(item, dict):
            raise ValueError(f"findings[{index}] must be an object")
        severity = item.get("severity")
        if severity not in {"block", "warn"}:
            raise ValueError(f"findings[{index}].severity must be block or warn")
        line = item.get("line")
        if line is not None and not isinstance(line, int):
            raise ValueError(f"findings[{index}].line must be an integer or null")
        findings.append(
            Finding(
                id=str(item.get("id", f"finding-{index + 1}")),
                severity=severity,
                rule_section=str(item.get("rule_section", "unknown")),
                file=str(item.get("file", "")),
                line=line,
                summary=str(item.get("summary", "")),
                detail=str(item.get("detail", "")),
            )
        )

    return verdict, findings


def extract_json_payload(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise ValueError("parsed JSON must be an object")
    return parsed


def filter_findings(
    findings: list[Finding],
    files: list[str],
    added_lines: dict[str, set[int]],
) -> list[Finding]:
    allowed_files = set(files)
    kept: list[Finding] = []
    for finding in findings:
        if finding.file and finding.file not in allowed_files:
            continue
        if finding.line is not None and finding.file in added_lines:
            if finding.line not in added_lines[finding.file]:
                continue
        kept.append(finding)
    return kept


def reconcile_verdict(findings: list[Finding]) -> str:
    return "fail" if any(item.severity == "block" for item in findings) else "pass"


def call_deepseek(
    *,
    api_key: str,
    model: str,
    api_url: str,
    system_prompt: str,
    user_content: str,
    timeout: float,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    request = urllib.request.Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    return extract_json_payload(content)


def review_chunk(
    *,
    api_key: str,
    model: str,
    api_url: str,
    system_prompt: str,
    rules_text: str,
    changed: list[str],
    diff_chunk: str,
    timeout: float,
) -> tuple[str, list[Finding]]:
    user_content = "\n\n".join(
        [
            f"Prompt version: {PROMPT_VERSION}",
            "Changed files:",
            "\n".join(f"- {path}" for path in changed),
            "Applicable REPOSITORY_RULES sections:",
            rules_text,
            "Unified diff (review only added/changed lines):",
            diff_chunk,
        ]
    )
    parsed = call_deepseek(
        api_key=api_key,
        model=model,
        api_url=api_url,
        system_prompt=system_prompt,
        user_content=user_content,
        timeout=timeout,
    )
    return parse_findings(parsed)


def merge_findings(all_findings: list[Finding]) -> list[Finding]:
    merged: dict[tuple[str, str, str, int | None], Finding] = {}
    for finding in all_findings:
        key = (finding.id, finding.file, finding.summary, finding.line)
        existing = merged.get(key)
        if existing is None or (
            finding.severity == "block" and existing.severity != "block"
        ):
            merged[key] = finding
    return list(merged.values())


def format_report(
    *,
    base: str,
    head: str,
    verdict: str,
    findings: list[Finding],
    waived: bool,
) -> str:
    lines = [
        f"Repository rules review ({base}...{head})",
        f"Verdict: {verdict}",
    ]
    if waived:
        lines.append("Waived by maintainer label.")
    if not findings:
        lines.append("No findings.")
        return "\n".join(lines)

    lines.append("Findings:")
    for finding in findings:
        location = finding.file or "<unknown>"
        if finding.line is not None:
            location = f"{location}:{finding.line}"
        lines.append(
            f"- [{finding.severity}] {finding.id} ({finding.rule_section}) "
            f"{location}: {finding.summary}"
        )
        if finding.detail:
            lines.append(f"  {finding.detail}")
    return "\n".join(lines)


def deterministic_checks(files: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    ad_touched = any(
        "ad/" in path or "linearize" in path or "transpose_rule" in path
        for path in files
    )
    runtime_touched = any(path.startswith("crates/tenferro-runtime/") for path in files)
    if ad_touched and runtime_touched:
        script = ROOT / "scripts" / "check-ad-boundaries.py"
        completed = subprocess.run(
            [sys.executable, str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stdout.strip() or completed.stderr.strip()
            findings.append(
                Finding(
                    id="ad-boundary-runtime",
                    severity="block",
                    rule_section="Rule Source Of Truth",
                    file="crates/tenferro-runtime",
                    line=None,
                    summary="AD symbols leaked into tenferro-runtime boundary",
                    detail=detail,
                )
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Merge base ref")
    parser.add_argument("--head", default="HEAD", help="Head ref (default: HEAD)")
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Write machine-readable report JSON to this path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip LLM call; run diff/section selection and deterministic checks only",
    )
    parser.add_argument(
        "--waived",
        action="store_true",
        help="Treat review as waived (maintainer label in CI)",
    )
    parser.add_argument(
        "--dotenv",
        type=Path,
        metavar="PATH",
        help="Load environment from this dotenv file (default: .env at repo root when present)",
    )
    parser.add_argument(
        "--no-dotenv",
        action="store_true",
        help="Do not load .env even when the file exists",
    )
    parser.add_argument(
        "--worktree",
        action="store_true",
        help="Diff the working tree against --base (includes uncommitted changes)",
    )
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--api-url",
        default=os.environ.get("DEEPSEEK_API_URL", DEFAULT_API_URL),
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    configure_dotenv(explicit=args.dotenv, skip=args.no_dotenv)

    if not RULES_PATH.is_file():
        print(f"Missing rules file: {RULES_PATH}", file=sys.stderr)
        return 1
    if not PROMPT_PATH.is_file():
        print(f"Missing prompt file: {PROMPT_PATH}", file=sys.stderr)
        return 1

    files = changed_files(args.base, args.head, worktree=args.worktree)
    if not files:
        report = {
            "verdict": "pass",
            "waived": args.waived,
            "findings": [],
            "summary": "No changed files; review skipped.",
        }
        print(json.dumps(report, indent=2))
        return 0

    diff_text = unified_diff(args.base, args.head, worktree=args.worktree)
    added_lines = added_lines_by_file(diff_text)
    section_names = select_rule_sections(files)
    rules_text = build_rules_payload(section_names)
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    findings = deterministic_checks(files)

    if args.waived:
        report_body = format_report(
            base=args.base,
            head=args.head,
            verdict="pass",
            findings=findings,
            waived=True,
        )
        print(report_body)
        payload = {
            "verdict": "pass",
            "waived": True,
            "findings": [item.to_dict() for item in findings],
            "changed_files": files,
            "rule_sections": section_names,
        }
        if args.output_json:
            args.output_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return 0

    if not args.dry_run:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            print("DEEPSEEK_API_KEY is not set", file=sys.stderr)
            return 1

        file_diffs = per_file_diffs(
            args.base,
            args.head,
            files,
            worktree=args.worktree,
        )
        chunks = split_diff_chunks(file_diffs)
        llm_findings: list[Finding] = []
        for chunk in chunks:
            _, chunk_findings = review_chunk(
                api_key=api_key,
                model=args.model,
                api_url=args.api_url,
                system_prompt=system_prompt,
                rules_text=rules_text,
                changed=files,
                diff_chunk=chunk,
                timeout=args.timeout,
            )
            llm_findings.extend(chunk_findings)
        findings.extend(merge_findings(llm_findings))

    findings = filter_findings(findings, files, added_lines)
    block_findings = [item for item in findings if item.severity == "block"]
    verdict = reconcile_verdict(block_findings)

    report_body = format_report(
        base=args.base,
        head=args.head,
        verdict=verdict,
        findings=findings,
        waived=False,
    )
    print(report_body)

    payload = {
        "verdict": verdict,
        "waived": False,
        "findings": [item.to_dict() for item in findings],
        "block_findings": [item.to_dict() for item in block_findings],
        "changed_files": files,
        "rule_sections": section_names,
        "prompt_version": PROMPT_VERSION,
    }
    if args.output_json:
        args.output_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return 1 if block_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
