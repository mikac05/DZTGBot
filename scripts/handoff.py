#!/usr/bin/env python3
"""Validate, synchronize, and restore DZTGBot's durable AI handoff."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
HANDOFF_PATH = ROOT / "docs" / "context" / "HANDOFF.md"
METADATA_START = "<!-- HANDOFF-METADATA:START -->"
METADATA_END = "<!-- HANDOFF-METADATA:END -->"
MAX_SCAN_BYTES = 5_000_000

REQUIRED_HEADINGS = {
    "docs/context/HANDOFF.md": (
        "## Current objective",
        "## Completed",
        "## Decisions",
        "## Open items",
        "## Exact next action",
        "## Verification",
        "## Git snapshot metadata",
    ),
    "docs/context/CONTINUE_HERE.md": (
        "## Current status",
        "## Exact next action",
        "## Inputs still required from the user or target environment",
        "## Do not redo",
        "## Required verification on resume",
    ),
    "docs/context/PROJECT_CONTEXT.md": (
        "## Purpose and boundary",
        "## Current architecture",
        "## Settled decisions",
        "## Required private inputs",
        "## Evidence boundaries",
        "## Durable context model",
    ),
}

CREDENTIAL_PATTERNS = (
    ("Telegram-token-shaped value", re.compile(r"\b[0-9]{6,12}:[A-Za-z0-9_-]{20,}\b")),
    ("Google-key-shaped value", re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")),
    (
        "private-key material",
        re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    ),
    (
        "credential embedded in URL",
        re.compile(r"https?://[^\s/:@]+:[^\s@]+@", re.IGNORECASE),
    ),
    (
        "Jira cloud endpoint",
        re.compile(r"https?://[^\s/]+\.atlassian\.net(?:/[^\s]*)?", re.IGNORECASE),
    ),
)

SENSITIVE_ASSIGNMENTS = (
    re.compile(
        r"^\s*(TELEGRAM_BOT_TOKEN|GEMINI_API_KEY|VPN_PASSWORD|IPSEC_PSK)\s*=\s*(.*?)\s*$",
        re.MULTILINE,
    ),
    re.compile(
        r"^\s*(ipsec-psk|password|gateway)\s*=\s*(.*?)\s*$",
        re.MULTILINE,
    ),
)
SAFE_VALUE_PREFIXES = (
    "TODO",
    "PLACEHOLDER",
    "TEST_ONLY",
    "<",
    "${",
    '"TODO',
    "'TODO",
)


class HandoffError(RuntimeError):
    """Raised when a handoff operation cannot proceed safely."""


def _safe_git_error(stderr: str) -> str:
    sanitized = re.sub(
        r"(https?://)[^\s/@:]+:[^\s@]+@",
        r"\1<redacted>@",
        stderr,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"\b(?:gh[pousr]_|github_pat_|AIza)[A-Za-z0-9_-]+\b",
        "<redacted-credential>",
        sanitized,
    )
    lines = [line.strip() for line in sanitized.splitlines() if line.strip()]
    return " ".join(lines[-2:])[:500]


def _run_git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        ("git", *arguments),
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )
    if check and result.returncode != 0:
        command = "git " + " ".join(arguments[:2])
        detail = _safe_git_error(result.stderr)
        suffix = f" {detail}" if detail else ""
        raise HandoffError(
            f"{command} failed with exit code {result.returncode}.{suffix}"
        )
    return result


def _git_output(*arguments: str) -> str | None:
    result = _run_git(*arguments, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _require_repository() -> None:
    if _git_output("rev-parse", "--is-inside-work-tree") != "true":
        raise HandoffError("Run this helper from the DZTGBot Git repository.")


def _current_branch() -> str:
    branch = _git_output("branch", "--show-current")
    if not branch:
        raise HandoffError("A named Git branch is required; detached HEAD is not supported.")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch):
        raise HandoffError("The current branch name contains unsupported characters.")
    return branch


def _upstream_name(branch: str) -> str:
    upstream = _git_output("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if upstream:
        return upstream
    remote = _git_output("config", "--get", f"branch.{branch}.remote")
    merge_ref = _git_output("config", "--get", f"branch.{branch}.merge")
    if remote and merge_ref and merge_ref.startswith("refs/heads/"):
        return f"{remote}/{merge_ref.removeprefix('refs/heads/')}"
    return "not configured"


def _candidate_paths() -> list[str]:
    result = _run_git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted(path for path in result.stdout.split("\0") if path)


def _path_forbidden(relative_path: str) -> str | None:
    normalized = relative_path.replace("\\", "/")
    path = PurePosixPath(normalized)
    lower = normalized.lower()
    parts = {part.lower() for part in path.parts}

    if lower == ".env" or (lower.startswith(".env.") and lower != ".env.example"):
        return "environment file"
    if lower.startswith("src/ref/") or path.name.lower() == "vpnsettings.xml":
        return "private VPN reference"
    if parts.intersection({"secrets", "credentials", "private"}):
        return "private-data directory"
    if path.suffix.lower() in {".key", ".pem", ".p12", ".pfx", ".ovpn", ".wg"}:
        return "credential or VPN file"
    if lower.endswith(".nmconnection") and not lower.endswith(".example.nmconnection"):
        return "private NetworkManager profile"
    return None


def _secret_findings_in_text(text: str) -> list[str]:
    findings = [label for label, pattern in CREDENTIAL_PATTERNS if pattern.search(text)]
    for assignment_pattern in SENSITIVE_ASSIGNMENTS:
        for match in assignment_pattern.finditer(text):
            value = match.group(2).strip()
            if value and not value.startswith(SAFE_VALUE_PREFIXES):
                findings.append(f"configured {match.group(1)} assignment")
    return sorted(set(findings))


def _secret_scan() -> None:
    findings: list[str] = []
    for relative_path in _candidate_paths():
        forbidden_reason = _path_forbidden(relative_path)
        if forbidden_reason:
            findings.append(f"{relative_path}: forbidden {forbidden_reason}")
            continue

        absolute_path = ROOT / relative_path
        if not absolute_path.exists():
            continue
        if absolute_path.is_symlink():
            findings.append(f"{relative_path}: symbolic links are not accepted by the handoff gate")
            continue
        if not absolute_path.is_file():
            continue

        data = absolute_path.read_bytes()
        if len(data) > MAX_SCAN_BYTES:
            findings.append(f"{relative_path}: too large for the handoff safety scan")
            continue
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        for reason in _secret_findings_in_text(text):
            findings.append(f"{relative_path}: {reason}")

    if findings:
        formatted = "\n".join(f"- {finding}" for finding in findings)
        raise HandoffError(f"Secret-safety validation failed:\n{formatted}")


def _validate_required_files() -> None:
    for relative_path, headings in REQUIRED_HEADINGS.items():
        path = ROOT / relative_path
        if not path.is_file():
            raise HandoffError(f"Required context file is missing: {relative_path}")
        text = path.read_text(encoding="utf-8")
        missing = [heading for heading in headings if heading not in text]
        if missing:
            raise HandoffError(f"{relative_path} is missing heading: {missing[0]}")

    handoff = HANDOFF_PATH.read_text(encoding="utf-8")
    if handoff.count(METADATA_START) != 1 or handoff.count(METADATA_END) != 1:
        raise HandoffError("HANDOFF.md must contain exactly one metadata marker pair.")
    if handoff.index(METADATA_START) >= handoff.index(METADATA_END):
        raise HandoffError("HANDOFF.md metadata markers are out of order.")

    for required_file in ("AGENTS.md", "GEMINI.md", "README.md"):
        if not (ROOT / required_file).is_file():
            raise HandoffError(f"Required agent entry point is missing: {required_file}")


def _validate_ignore_rules() -> None:
    private_examples = (
        ".env",
        "src/ref/vpnsettings.xml",
        "config/private.nmconnection",
    )
    for path in private_examples:
        result = _run_git("check-ignore", "--quiet", "--", path, check=False)
        if result.returncode != 0:
            raise HandoffError(f"The Git ignore policy does not protect {path}.")


def validate_repository() -> None:
    _require_repository()
    _validate_required_files()
    _validate_ignore_rules()
    _secret_scan()


def snapshot() -> None:
    _require_repository()
    branch = _current_branch()
    base_commit = _git_output("rev-parse", "--verify", "HEAD") or "initial repository"
    if base_commit != "initial repository":
        base_commit = base_commit[:12]
    status = _git_output("status", "--porcelain") or ""
    status_entries = len(status.splitlines())
    generated = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    metadata = (
        f"{METADATA_START}\n"
        f"- Generated UTC: `{generated}`\n"
        f"- Branch: `{branch}`\n"
        f"- Upstream: `{_upstream_name(branch)}`\n"
        f"- Base commit before this handoff: `{base_commit}`\n"
        f"- Working-tree entries before metadata refresh: `{status_entries}`\n"
        "- The handoff commit is the commit containing this file.\n"
        f"{METADATA_END}"
    )

    existing = HANDOFF_PATH.read_text(encoding="utf-8")
    start = existing.index(METADATA_START)
    end = existing.index(METADATA_END) + len(METADATA_END)
    HANDOFF_PATH.write_text(f"{existing[:start]}{metadata}{existing[end:]}", encoding="utf-8", newline="\n")


def _remote_branch_exists(branch: str) -> bool:
    result = _run_git(
        "ls-remote",
        "--exit-code",
        "--heads",
        "origin",
        f"refs/heads/{branch}",
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    raise HandoffError("Could not inspect the origin branch. Check Git authentication and connectivity.")


def _ensure_remote_not_ahead(branch: str) -> None:
    if not _remote_branch_exists(branch):
        return
    if _git_output("rev-parse", "--verify", "HEAD") is None:
        raise HandoffError("The remote branch already exists but the local branch has no commit.")

    _run_git("fetch", "--prune", "origin", branch)
    remote_ref = f"refs/remotes/origin/{branch}"
    result = _run_git("merge-base", "--is-ancestor", remote_ref, "HEAD", check=False)
    if result.returncode != 0:
        raise HandoffError(
            "origin has commits not contained in the local branch. Run DZTGBot continue from a clean checkout before creating another handoff."
        )


def sync(message: str | None) -> None:
    snapshot()
    validate_repository()
    branch = _current_branch()
    _ensure_remote_not_ahead(branch)

    _run_git("add", "-A", "--", ".")
    _secret_scan()
    staged = _run_git("diff", "--cached", "--quiet", "--", check=False)
    if staged.returncode not in (0, 1):
        raise HandoffError("Could not inspect the staged handoff changes.")
    if staged.returncode == 1:
        commit_message = message or f"handoff: DZTGBot {datetime.now(UTC).date().isoformat()}"
        _run_git("commit", "-m", commit_message)

    _run_git("push", "--set-upstream", "origin", branch)
    commit = _git_output("rev-parse", "--short", "HEAD") or "unknown"
    print(f"[handoff] Synced branch {branch} at commit {commit}.")


def continue_from_remote() -> None:
    _require_repository()
    status = _git_output("status", "--porcelain") or ""
    if status:
        raise HandoffError(
            "The checkout has local changes. Nothing was pulled; preserve and review them before integrating remote work."
        )

    branch = _current_branch()
    if not _remote_branch_exists(branch):
        raise HandoffError(f"origin/{branch} does not exist, so there is no remote handoff to load.")
    _run_git("fetch", "--prune", "origin", branch)
    _run_git("pull", "--ff-only", "origin", branch)
    validate_repository()
    commit = _git_output("rev-parse", "--short", "HEAD") or "unknown"
    print(f"[handoff] Loaded branch {branch} at commit {commit}.")
    print("[handoff] Read docs/context/HANDOFF.md, CONTINUE_HERE.md, PROJECT_CONTEXT.md, then README.md.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("snapshot", "validate", "sync", "continue"))
    parser.add_argument("--message", help="Optional Git commit message for sync mode.")
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    try:
        if arguments.mode == "snapshot":
            snapshot()
            print("[handoff] Snapshot metadata refreshed.")
        elif arguments.mode == "validate":
            validate_repository()
            print("[handoff] Context and secret-safety validation passed.")
        elif arguments.mode == "sync":
            sync(arguments.message)
        else:
            continue_from_remote()
    except (HandoffError, OSError, UnicodeError, ValueError) as error:
        print(f"[handoff] ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
