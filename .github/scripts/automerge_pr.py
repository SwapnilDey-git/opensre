#!/usr/bin/env python3
"""Merge a pull request when it is labeled automerge and CI checks are green.

Also closes issues linked via closing keywords (Fixes/Closes/Resolves #N). GitHub's
native auto-close does not run when the merge is performed with GITHUB_TOKEN, so
this script closes those issues explicitly after a successful merge.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

AUTOMERGE_LABEL = "automerge"
AUTOMERGE_WORKFLOW_NAME = "Auto-merge"
AUTOMERGE_JOB_CHECK_NAME = "Merge when CI is green"
# External Mintlify app check; flaky on doc-only wording and not a merge blocker.
AUTOMERGE_SKIPPED_CHECK_SUBSTRINGS = ("vale-spellcheck",)
CHECK_RUN_PENDING_STATUSES = frozenset({"IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "REQUESTED"})
CHECK_RUN_ALLOWED_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
STATUS_CONTEXT_PENDING_STATES = frozenset({"PENDING", "EXPECTED"})
STATUS_CONTEXT_ALLOWED_STATES = frozenset({"SUCCESS"})
ALREADY_CLOSED_MARKERS = ("already closed", "not open", "is closed")


def _check_display_name(check: dict[str, Any]) -> str:
    return str(check.get("name") or check.get("context") or "unknown")


def _is_automerge_workflow_check(check: dict[str, Any]) -> bool:
    if check.get("workflowName") == AUTOMERGE_WORKFLOW_NAME:
        return True
    return check.get("name") == AUTOMERGE_JOB_CHECK_NAME


def _is_skipped_automerge_check(check: dict[str, Any]) -> bool:
    if _is_automerge_workflow_check(check):
        return True
    name = _check_display_name(check).casefold()
    return any(marker in name for marker in AUTOMERGE_SKIPPED_CHECK_SUBSTRINGS)


def _check_run_is_green(check: dict[str, Any]) -> tuple[bool, str]:
    name = _check_display_name(check)
    status = check.get("status", "")
    conclusion = check.get("conclusion") or ""

    if status in CHECK_RUN_PENDING_STATUSES:
        return False, f"check still running: {name}"

    if status != "COMPLETED":
        return False, f"unexpected check status for {name}: {status or 'missing status'}"

    if conclusion not in CHECK_RUN_ALLOWED_CONCLUSIONS:
        return False, f"check not green: {name} ({conclusion or 'missing conclusion'})"

    return True, ""


def _status_context_is_green(check: dict[str, Any]) -> tuple[bool, str]:
    name = _check_display_name(check)
    state = check.get("state") or ""

    if state in STATUS_CONTEXT_PENDING_STATES:
        return False, f"status still pending: {name}"

    if state not in STATUS_CONTEXT_ALLOWED_STATES:
        return False, f"status not green: {name} ({state or 'missing state'})"

    return True, ""


def _rollup_item_is_green(check: dict[str, Any]) -> tuple[bool, str]:
    typename = check.get("__typename", "")
    if typename == "StatusContext":
        return _status_context_is_green(check)
    if typename == "CheckRun":
        return _check_run_is_green(check)
    if "state" in check and "status" not in check:
        return _status_context_is_green(check)
    return _check_run_is_green(check)


def _squash_commit_subject(title: str, pr_number: str) -> str:
    suffix = f"(#{pr_number})"
    stripped = title.rstrip()
    if stripped.endswith(suffix):
        return stripped
    return f"{stripped} {suffix}"


def _checks_are_green(status_rollup: list[dict[str, Any]]) -> tuple[bool, str]:
    if not status_rollup:
        return False, "no status checks reported yet"

    for check in status_rollup:
        if _is_skipped_automerge_check(check):
            continue
        green, reason = _rollup_item_is_green(check)
        if not green:
            return False, reason

    return True, "all checks green"


def _same_repo_closing_issue_numbers(pr: dict[str, Any], repo: str) -> list[int]:
    """Return issue numbers this PR should close in *repo* (owner/name)."""
    try:
        owner, name = repo.split("/", 1)
    except ValueError:
        return []

    numbers: list[int] = []
    for ref in pr.get("closingIssuesReferences") or []:
        ref_repo = ref.get("repository") or {}
        ref_owner = (ref_repo.get("owner") or {}).get("login")
        ref_name = ref_repo.get("name")
        if ref_owner != owner or ref_name != name:
            continue
        number = ref.get("number")
        if isinstance(number, int):
            numbers.append(number)
    return numbers


def _is_already_closed_error(stderr: str) -> bool:
    lowered = stderr.casefold()
    return any(marker in lowered for marker in ALREADY_CLOSED_MARKERS)


def _close_linked_issues(repo: str, pr_number: str, issue_numbers: list[int]) -> None:
    """Close issues linked by Fixes/Closes/Resolves after a successful merge."""
    for issue_number in issue_numbers:
        result = subprocess.run(
            [
                "gh",
                "issue",
                "close",
                str(issue_number),
                "--repo",
                repo,
                "--reason",
                "completed",
                "--comment",
                f"Closed automatically by merge of #{pr_number}.",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"Closed issue #{issue_number}.")
            continue
        err = (result.stderr or result.stdout or "").strip()
        if _is_already_closed_error(err):
            print(f"Issue #{issue_number} already closed; skipping.")
            continue
        print(f"Warning: failed to close issue #{issue_number}: {err}", file=sys.stderr)


def _run_gh(args: list[str]) -> Any:
    result = subprocess.run(
        ["gh", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = os.environ["PR_NUMBER"]

    pr = _run_gh(
        [
            "pr",
            "view",
            pr_number,
            "--repo",
            repo,
            "--json",
            "baseRefName,closingIssuesReferences,isDraft,mergeable,mergeStateStatus,labels,state,statusCheckRollup,title",
        ]
    )

    if pr.get("baseRefName") != "main":
        print(f"PR #{pr_number} does not target main; skipping.")
        return 0

    if pr.get("state") != "OPEN":
        print(f"PR #{pr_number} is not open; skipping.")
        return 0

    if pr.get("isDraft"):
        print(f"PR #{pr_number} is a draft; skipping.")
        return 0

    label_names = {label["name"] for label in pr.get("labels", [])}
    if AUTOMERGE_LABEL not in label_names:
        print(f"PR #{pr_number} does not have the {AUTOMERGE_LABEL} label; skipping.")
        return 0

    if pr.get("mergeable") != "MERGEABLE":
        print(f"PR #{pr_number} is not mergeable ({pr.get('mergeStateStatus')}); skipping.")
        return 0

    green, reason = _checks_are_green(pr.get("statusCheckRollup") or [])
    if not green:
        print(f"PR #{pr_number} not ready to merge: {reason}")
        return 0

    title = pr["title"]
    linked_issues = _same_repo_closing_issue_numbers(pr, repo)
    print(f"Merging PR #{pr_number}: {title}")
    subprocess.run(
        [
            "gh",
            "pr",
            "merge",
            pr_number,
            "--repo",
            repo,
            "--squash",
            "--delete-branch",
            "--subject",
            _squash_commit_subject(title, pr_number),
        ],
        check=True,
    )
    print(f"Merged PR #{pr_number}.")
    if linked_issues:
        _close_linked_issues(repo, pr_number, linked_issues)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or exc.stdout or str(exc), file=sys.stderr)
        raise SystemExit(exc.returncode) from exc
