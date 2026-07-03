#!/usr/bin/env python3
"""Report upstream changes that may affect the Kurukin MoneyPrinterTurbo fork."""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from typing import Iterable


HIGH_RISK_PATHS = (
    "app/models/schema.py",
    "app/services/task.py",
    "app/services/video.py",
    "app/services/material.py",
    "app/controllers/",
    "webui/",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    "config.example.toml",
)


@dataclass(frozen=True)
class ChangedFile:
    path: str
    risk: str


class CheckError(Exception):
    """Expected command or repository error."""


def is_high_risk_path(path: str) -> bool:
    normalized = path.strip().replace("\\", "/")
    for pattern in HIGH_RISK_PATHS:
        if pattern.endswith("/"):
            if normalized.startswith(pattern):
                return True
        elif normalized == pattern:
            return True
    return False


def classify_changed_files(paths: Iterable[str]) -> list[ChangedFile]:
    changed_files = []
    for path in paths:
        normalized = path.strip()
        if not normalized:
            continue
        changed_files.append(
            ChangedFile(
                path=normalized,
                risk="HIGH RISK" if is_high_risk_path(normalized) else "normal",
            )
        )
    return changed_files


def parse_lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip()]


def run_git(args: list[str]) -> str:
    command = ["git", *args]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "").strip()
        raise CheckError(f"{' '.join(command)} failed: {details}")
    return completed.stdout


def fetch_upstream(upstream_ref: str) -> None:
    remote = upstream_ref.split("/", 1)[0]
    if not remote:
        raise CheckError(f"cannot determine remote from upstream ref: {upstream_ref}")
    run_git(["fetch", remote])


def collect_report(base_ref: str, upstream_ref: str) -> tuple[list[str], list[ChangedFile]]:
    commits = parse_lines(run_git(["log", "--oneline", f"{base_ref}..{upstream_ref}"]))
    changed_paths = parse_lines(
        run_git(["diff", "--name-only", f"{base_ref}...{upstream_ref}"])
    )
    return commits, classify_changed_files(changed_paths)


def print_report(base_ref: str, upstream_ref: str, commits: list[str], files: list[ChangedFile]) -> None:
    print("MoneyPrinterTurbo upstream update check")
    print(f"base: {base_ref}")
    print(f"upstream: {upstream_ref}")
    print()

    print("New upstream commits:")
    if commits:
        for commit in commits:
            print(f"  {commit}")
    else:
        print("  none")
    print()

    print("Changed files:")
    if files:
        for item in files:
            print(f"  [{item.risk}] {item.path}")
    else:
        print("  none")
    print()

    high_risk_count = sum(1 for item in files if item.risk == "HIGH RISK")
    print(f"summary: commits={len(commits)} files={len(files)} high_risk={high_risk_count}")
    if high_risk_count:
        print("recommendation: review HIGH RISK files before rebasing or merging upstream.")
    else:
        print("recommendation: no high-risk paths changed, but still run the fork smoke tests.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check upstream/main changes without merging or switching branches."
    )
    parser.add_argument("--base", default="custom/mvp")
    parser.add_argument("--upstream", default="upstream/main")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        fetch_upstream(args.upstream)
        commits, files = collect_report(args.base, args.upstream)
        print_report(args.base, args.upstream, commits, files)
    except CheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
