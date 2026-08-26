"""Authorized lane publication and exact-revision GitHub CI observation."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from orkastrator.git import GitError, worktree_path
from orkastrator.models import (
    CiCheckResult,
    CiReceipt,
    LaneRecord,
    PublicationReceipt,
    ValidationResult,
)


class PublicationError(RuntimeError):
    """Raised when remote state cannot be mutated or observed safely."""


class PullRequestLanded(PublicationError):
    """The authorized pull request was merged while the lane was still publishing.

    Merged and closed are opposite outcomes, and collapsing both into "no longer
    open" reports the lane succeeding as the lane failing. This carries the
    landed receipt so the caller can preserve both the published and merged heads.
    """

    def __init__(self, receipt: PublicationReceipt):
        merged_head_sha = receipt.merged_head_sha or receipt.head_sha
        super().__init__(f"the authorized lane pull request was merged at {merged_head_sha}")
        self.head_sha = merged_head_sha
        self.receipt = receipt


class IntegrationConflict(PublicationError):
    """The lane cannot merge into the current base branch without intervention."""


@dataclass(frozen=True, slots=True)
class PublicationContent:
    """Frozen workflow evidence rendered into one owned pull request."""

    issue_id: str
    accepted_scope: str
    stop_condition: str
    implementation_summary: str
    validation_results: tuple[ValidationResult, ...]
    review_summary: str | None
    unresolved_findings: tuple[str, ...]
    published_head: str
    ci: CiReceipt | None = None


class LanePublisher(Protocol):
    """Provider boundary used after a lane converges locally."""

    async def publish(
        self,
        *,
        run_id: str,
        lane: LaneRecord,
        head_sha: str,
        previous: PublicationReceipt | None,
        content: PublicationContent,
    ) -> PublicationReceipt: ...

    async def reconcile(self, receipt: PublicationReceipt, content: PublicationContent) -> None: ...

    async def checks(self, receipt: PublicationReceipt) -> CiReceipt: ...

    async def mark_ready(self, receipt: PublicationReceipt) -> PublicationReceipt: ...

    async def land(self, receipt: PublicationReceipt) -> PublicationReceipt: ...


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded subprocess result used by the GitHub adapter."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Injectable shell-free command runner."""

    async def run(self, cwd: Path, *arguments: str) -> CommandResult: ...


class SubprocessCommandRunner:
    """Run explicit commands without a shell."""

    async def run(self, cwd: Path, *arguments: str) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return CommandResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode(errors="replace")[-64_000:],
            stderr=stderr.decode(errors="replace")[-8_000:],
        )


_CHECK_DISCOVERY = "check-discovery"
"""Name of the synthetic result standing in for "nothing has reported here"."""


class GitHubPublisher:
    """Publish deterministic branches and observe GitHub checks at an exact SHA."""

    def __init__(
        self,
        *,
        gh_command: tuple[str, ...] = ("gh",),
        runner: CommandRunner | None = None,
        advisory_checks: Iterable[str] = (),
    ):
        self._gh = gh_command
        self._runner = runner or SubprocessCommandRunner()
        self._advisory = frozenset(advisory_checks)

    async def publish(
        self,
        *,
        run_id: str,
        lane: LaneRecord,
        head_sha: str,
        previous: PublicationReceipt | None,
        content: PublicationContent,
    ) -> PublicationReceipt:
        if content.published_head != head_sha:
            raise PublicationError("pull-request content does not match the published revision")
        try:
            path = worktree_path(lane.worktree_id or "")
        except GitError as exc:
            raise PublicationError(str(exc)) from exc
        remote_url = await self._required(path, "git", "remote", "get-url", "origin")
        repository = _github_repository(remote_url.strip())
        default_branch = await self._default_branch(path, repository)
        branch = f"orkastrator/{run_id[:12]}/{lane.name}"
        if previous is not None and (
            previous.branch != branch
            or previous.remote_url != remote_url.strip()
            or previous.base_branch != default_branch
        ):
            raise PublicationError("persisted publication target changed")
        # The pull request is read before anything is pushed. A merged pull
        # request usually means its branch is gone, and pushing first would
        # recreate the branch GitHub deleted on merge before anyone had looked
        # at why it was missing.
        pull_requests = await self._gh_json(
            path,
            "pr",
            "list",
            "--repo",
            repository,
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "url,isDraft,state,body,headRefOid",
            "--limit",
            "10",
        )
        title = _pull_request_title(content)
        body = _pull_request_body(run_id, content)
        if not isinstance(pull_requests, list):
            raise PublicationError("GitHub returned an invalid pull-request list")
        if len(pull_requests) > 1:
            raise PublicationError(f"multiple pull requests exist for lane branch {branch}")
        pull_request = pull_requests[0] if pull_requests else None
        if pull_request is not None:
            pull_request_url = str(pull_request["url"])
            marker = f"orkastrator run: `{run_id}`"
            if previous is None and marker not in str(pull_request.get("body") or ""):
                raise PublicationError("existing pull request is not owned by this accepted run")
            if previous is not None and pull_request_url != previous.pull_request_url:
                raise PublicationError("pull request identity changed after publication")
            state = _pull_request_state(pull_request)
            if state == "MERGED":
                merged_head = str(pull_request.get("headRefOid") or "an unreported head")
                if merged_head != head_sha:
                    raise PublicationError(
                        f"the authorized lane pull request was merged at {merged_head}, "
                        f"which is not this lane's integrated head {head_sha}"
                    )
                return PublicationReceipt(
                    run_id=run_id,
                    lane=lane.name,
                    remote_url=remote_url.strip(),
                    base_branch=default_branch,
                    branch=branch,
                    pull_request_url=pull_request_url,
                    head_sha=head_sha,
                    draft=False,
                    landed=True,
                )

        remote_head = await self._remote_head(path, branch)
        if previous is None and remote_head is not None and remote_head != head_sha:
            raise PublicationError(f"existing branch {branch} points to an unrelated revision")
        if previous is not None and remote_head not in {previous.head_sha, head_sha}:
            raise PublicationError(
                f"remote branch {branch} diverged from published head {previous.head_sha}"
            )
        if remote_head != head_sha:
            await self._required(path, "git", "push", "origin", f"{head_sha}:refs/heads/{branch}")

        if pull_request is not None:
            await self._required(
                path,
                *self._gh,
                "pr",
                "edit",
                pull_request_url,
                "--title",
                title,
                "--body",
                body,
            )
            draft = bool(pull_request.get("isDraft", True))
        else:
            result = await self._required(
                path,
                *self._gh,
                "pr",
                "create",
                "--repo",
                repository,
                "--draft",
                "--base",
                default_branch,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            )
            pull_request_url = result.strip().splitlines()[-1]
            draft = True
        return PublicationReceipt(
            run_id=run_id,
            lane=lane.name,
            remote_url=remote_url.strip(),
            base_branch=default_branch,
            branch=branch,
            pull_request_url=pull_request_url,
            head_sha=head_sha,
            draft=draft,
        )

    async def reconcile(self, receipt: PublicationReceipt, content: PublicationContent) -> None:
        """Enrich the same owned draft as exact-head evidence becomes available."""

        if content.published_head != receipt.head_sha:
            raise PublicationError("pull-request content does not match the published revision")
        state = await self._gh_json(
            Path.cwd(),
            "pr",
            "view",
            receipt.pull_request_url,
            "--json",
            "headRefOid,state,isDraft,body",
        )
        _verify_pull_request(state, receipt)
        marker = f"orkastrator run: `{receipt.run_id}`"
        if not isinstance(state, dict) or marker not in str(state.get("body") or ""):
            raise PublicationError("existing pull request is not owned by this accepted run")
        await self._required(
            Path.cwd(),
            *self._gh,
            "pr",
            "edit",
            receipt.pull_request_url,
            "--title",
            _pull_request_title(content),
            "--body",
            _pull_request_body(receipt.run_id, content),
        )

    async def checks(self, receipt: PublicationReceipt) -> CiReceipt:
        path = Path.cwd()
        repository = _github_repository(receipt.remote_url)
        pull_request = await self._gh_json(
            path,
            "pr",
            "view",
            receipt.pull_request_url,
            "--json",
            "headRefOid,state,isDraft,mergeCommit",
        )
        _verify_pull_request(pull_request, receipt)
        required = await self._required_pr_checks(path, receipt.pull_request_url)
        if required:
            checks = required
        else:
            check_data = await self._gh_json(
                path,
                "api",
                f"repos/{repository}/commits/{receipt.head_sha}/check-runs",
                "--paginate",
                "--slurp",
            )
            status_data = await self._gh_json(
                path, "api", f"repos/{repository}/commits/{receipt.head_sha}/status"
            )
            checks = _required_checks(
                _parse_checks(check_data, status_data, receipt.head_sha), None
            )
        # Every check is recorded, so the receipt shows what the advisory suite
        # actually did; only the enforced ones decide the lane.
        gating = [item for item in checks if item.name not in self._advisory]
        if any(item.status in {"failed", "cancelled"} for item in gating):
            status = "failed"
        elif (
            not gating
            or any(item.status == "pending" for item in gating)
            or not any(item.status == "passed" for item in gating)
        ):
            # No checks at all is not a pass. GitHub needs a moment to register a
            # push's workflow runs, so a lane that asks right after publishing sees
            # an empty list, and reading that as "passed" would merge a lane whose
            # CI never ran. Report pending and let the next tick ask again. A head
            # whose only reported check is advisory is the same situation: nothing
            # enforced has spoken yet.
            status = "pending"
        else:
            status = "passed"
        return CiReceipt(provider="github", head_sha=receipt.head_sha, status=status, checks=checks)

    async def mark_ready(self, receipt: PublicationReceipt) -> PublicationReceipt:
        if not receipt.draft:
            return receipt
        state = await self._gh_json(
            Path.cwd(),
            "pr",
            "view",
            receipt.pull_request_url,
            "--json",
            "headRefOid,state,isDraft,mergeCommit",
        )
        _verify_pull_request(state, receipt)
        if isinstance(state, dict) and state.get("isDraft") is False:
            return receipt.model_copy(update={"draft": False})
        await self._required(Path.cwd(), *self._gh, "pr", "ready", receipt.pull_request_url)
        return receipt.model_copy(update={"draft": False})

    async def land(self, receipt: PublicationReceipt) -> PublicationReceipt:
        """Merge the checked lane into the current base with a real merge commit."""

        if receipt.landed:
            return receipt
        path = Path.cwd()
        fields = "headRefOid,state,isDraft,mergeable,mergeStateStatus,mergeCommit"
        state = await self._gh_json(path, "pr", "view", receipt.pull_request_url, "--json", fields)
        landed = _landed_receipt(state, receipt, require_merge_commit=True)
        if landed is not None:
            return landed
        _raise_if_integration_conflict(state, receipt)

        result = await self._runner.run(
            path,
            *self._gh,
            "pr",
            "merge",
            receipt.pull_request_url,
            "--match-head-commit",
            receipt.head_sha,
            "--merge",
            "--subject",
            _merge_subject(receipt),
        )
        if result.returncode != 0:
            # Main may have moved between the preflight and merge request. Read
            # the current mergeability before classifying the provider error.
            state = await self._gh_json(
                path, "pr", "view", receipt.pull_request_url, "--json", fields
            )
            landed = _landed_receipt(state, receipt)
            if landed is not None:
                return landed
            _raise_if_integration_conflict(state, receipt)
            detail = (result.stderr or result.stdout).strip()[:2_000]
            raise PublicationError(f"GitHub merge failed: {detail}")

        state = await self._gh_json(path, "pr", "view", receipt.pull_request_url, "--json", fields)
        landed = _landed_receipt(state, receipt, require_merge_commit=True)
        if landed is None:
            raise PublicationError("GitHub did not report the pull request merged")
        return landed

    async def record_external_merge(self, receipt: PublicationReceipt) -> PublicationReceipt:
        """Observe and record a merge performed outside orkastrator."""

        fields = "headRefOid,state,isDraft,mergeable,mergeStateStatus,mergeCommit"
        state = await self._gh_json(
            Path.cwd(), "pr", "view", receipt.pull_request_url, "--json", fields
        )
        landed = _landed_receipt(state, receipt)
        if landed is None:
            raise PublicationError("the authorized lane pull request is not merged")
        return landed

    async def _default_branch(self, path: Path, repository: str) -> str:
        payload = await self._gh_json(
            path, "repo", "view", repository, "--json", "defaultBranchRef"
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("defaultBranchRef"), dict):
            raise PublicationError("GitHub did not return a default branch")
        name = payload["defaultBranchRef"].get("name")
        if not isinstance(name, str) or not name:
            raise PublicationError("GitHub default branch is empty")
        return name

    async def _remote_head(self, path: Path, branch: str) -> str | None:
        result = await self._runner.run(
            path, "git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"
        )
        if result.returncode != 0:
            raise PublicationError(
                f"could not inspect remote branch: {result.stderr.strip()[:2000]}"
            )
        return result.stdout.split(maxsplit=1)[0] if result.stdout.strip() else None

    async def _gh_json(self, path: Path, *arguments: str) -> object:
        raw = await self._required(path, *self._gh, *arguments)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PublicationError("GitHub CLI returned invalid JSON") from exc

    async def _required_pr_checks(self, path: Path, pull_request_url: str) -> list[CiCheckResult]:
        result = await self._runner.run(
            path,
            *self._gh,
            "pr",
            "checks",
            pull_request_url,
            "--required",
            "--json",
            "name,bucket,link,description",
        )
        if not result.stdout.strip():
            # `gh pr checks` writes "no checks reported on the ... branch" to stderr
            # and nothing to stdout when the push's workflow runs have not appeared
            # yet. That is a branch GitHub has not caught up with, not a failed
            # query, and the caller already falls back to the check-runs API.
            return []
        try:
            payload = cast(object, json.loads(result.stdout))
        except json.JSONDecodeError as exc:
            detail = (result.stderr or result.stdout).strip()[:2_000]
            raise PublicationError(f"GitHub required-check query failed: {detail}") from exc
        if not isinstance(payload, list):
            raise PublicationError("GitHub returned an invalid required-check list")
        checks = [_required_check(item) for item in payload]
        if result.returncode not in {0, 1, 8}:
            detail = (result.stderr or result.stdout).strip()[:2_000]
            raise PublicationError(f"GitHub required-check query failed: {detail}")
        return checks

    async def _required(self, path: Path, *arguments: str) -> str:
        result = await self._runner.run(path, *arguments)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:2_000]
            raise PublicationError(f"{' '.join(arguments)} failed: {detail}")
        return result.stdout


def _github_repository(remote_url: str) -> str:
    patterns = (
        r"^git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$",
        r"^ssh://git@github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.match(pattern, remote_url)
        if match:
            return match.group("repo")
    raise PublicationError(
        "unsupported remote provider; orkastrator publication currently supports GitHub remotes"
    )


def _pull_request_title(content: PublicationContent) -> str:
    """Use the implementation summary, never the machine-facing lane slug."""

    summary = next(
        (
            line.strip().lstrip("#*- ")
            for line in content.implementation_summary.splitlines()
            if line.strip()
        ),
        "",
    )
    title = re.split(r"(?<=[.!?])\s", summary, maxsplit=1)[0].rstrip(".!?")
    title = re.sub(re.escape(content.issue_id), "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s{2,}", " ", title).strip(" :-")
    if not title:
        raise PublicationError("worker summary did not contain a usable pull-request title")
    return title[:120].rstrip()


def _pull_request_body(run_id: str, content: PublicationContent) -> str:
    sections = ["## What changed", content.implementation_summary.strip()]
    scope = [content.accepted_scope.strip(), f"Stop condition: {content.stop_condition.strip()}"]
    sections.extend(("## Scope", "\n\n".join(item for item in scope if item)))

    checks = [
        _check_line(result.command, result.status, result.output)
        for result in content.validation_results
    ]
    if content.ci is not None:
        checks.append(f"CI for exact head `{content.ci.head_sha}`: {content.ci.status}")
        checks.extend(
            _check_line(check.name, check.status, check.output) for check in content.ci.checks
        )
    if checks:
        sections.extend(("## Checks", "\n".join(checks)))

    review: list[str] = []
    if content.review_summary:
        review.append(content.review_summary.strip())
    if content.unresolved_findings:
        review.append(
            "Unresolved findings:\n"
            + "\n".join(f"- {finding.strip()}" for finding in content.unresolved_findings)
        )
    if review:
        sections.extend(("## Review", "\n\n".join(review)))

    sections.extend(
        (
            "## Traceability",
            f"Issue: `{content.issue_id}`\n"
            f"Published head: `{content.published_head}`\n"
            f"orkastrator run: `{run_id}`",
        )
    )
    return "\n\n".join(part for part in sections if part)


def _check_line(name: str, status: str, output: str) -> str:
    line = f"- `{name}` - {status}"
    detail = output.strip()
    if detail:
        line += "\n" + "\n".join(f"  - {item}" for item in detail.splitlines())
    return line


def _pull_request_state(payload: dict[object, object]) -> str:
    """Name the state GitHub reported, refusing the ones a lane cannot proceed from.

    GitHub returns exactly three states here. `OPEN` is the working case and
    `MERGED` is the lane's goal, so both are answers rather than errors; only
    `CLOSED` means somebody rejected the branch. Branching on `!= "OPEN"`
    collapsed all three into one verdict and reported the goal as a failure.
    """

    state = str(payload.get("state"))
    if state in {"OPEN", "MERGED"}:
        return state
    if state == "CLOSED":
        raise PublicationError("the authorized lane pull request was closed without merging")
    raise PublicationError(f"GitHub reported unknown pull-request state {state}")


def _verify_pull_request(payload: object, receipt: PublicationReceipt) -> None:
    if not isinstance(payload, dict):
        raise PublicationError("GitHub returned invalid pull-request state")
    if _pull_request_state(payload) == "MERGED":
        landed = _landed_receipt(payload, receipt)
        assert landed is not None
        raise PullRequestLanded(landed)
    if payload.get("headRefOid") != receipt.head_sha:
        raise PublicationError("pull request head does not match the published revision")


def _raise_if_integration_conflict(payload: object, receipt: PublicationReceipt) -> None:
    if not isinstance(payload, dict):
        raise PublicationError("GitHub returned invalid pull-request state")
    if _pull_request_state(payload) == "MERGED":
        return
    if payload.get("headRefOid") != receipt.head_sha:
        raise PublicationError("pull request head does not match the published revision")
    if payload.get("mergeable") == "CONFLICTING" or payload.get("mergeStateStatus") == "DIRTY":
        raise IntegrationConflict(
            f"lane {receipt.lane} conflicts with current {receipt.base_branch}"
        )


def _landed_receipt(
    payload: object,
    receipt: PublicationReceipt,
    *,
    require_merge_commit: bool = False,
) -> PublicationReceipt | None:
    if not isinstance(payload, dict):
        raise PublicationError("GitHub returned invalid pull-request state")
    state = _pull_request_state(payload)
    if state != "MERGED":
        if payload.get("headRefOid") != receipt.head_sha:
            raise PublicationError("pull request head does not match the published revision")
        return None
    merged_head_sha = payload.get("headRefOid")
    if not isinstance(merged_head_sha, str) or not merged_head_sha:
        raise PublicationError("GitHub did not report the merged pull-request head")
    merge_commit = payload.get("mergeCommit")
    merge_sha = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    if require_merge_commit and (not isinstance(merge_sha, str) or not merge_sha):
        raise PublicationError("GitHub did not report the resulting merge commit")
    if require_merge_commit and merge_sha == merged_head_sha:
        raise PublicationError("GitHub did not create a merge commit")
    if not isinstance(merge_sha, str) or not merge_sha or merge_sha == merged_head_sha:
        merge_sha = None
    return receipt.model_copy(
        update={
            "draft": False,
            "landed": True,
            "merged_head_sha": merged_head_sha,
            "merge_sha": merge_sha,
        }
    )


def _merge_subject(receipt: PublicationReceipt) -> str:
    prefix = "feat(publication): land "
    return prefix + receipt.lane[: 72 - len(prefix)]


def _required_check(raw: object) -> CiCheckResult:
    if not isinstance(raw, dict) or not raw.get("name"):
        raise PublicationError("GitHub returned an invalid required check")
    bucket = str(raw.get("bucket"))
    status = {
        "pass": "passed",
        "fail": "failed",
        "pending": "pending",
        "skipping": "skipped",
        "cancel": "cancelled",
    }.get(bucket)
    if status is None:
        raise PublicationError(f"GitHub returned unknown required-check bucket {bucket}")
    return CiCheckResult(
        name=str(raw["name"]),
        status=status,
        details_url=str(raw["link"]) if raw.get("link") else None,
        output=str(raw.get("description") or "")[-8_000:],
    )


def _parse_checks(check_data: object, status_data: object, head_sha: str) -> list[CiCheckResult]:
    parsed: dict[str, CiCheckResult] = {}
    pages = check_data if isinstance(check_data, list) else [check_data]
    check_runs = [
        raw
        for page in pages
        if isinstance(page, dict) and isinstance(page.get("check_runs"), list)
        for raw in page["check_runs"]
    ]
    if isinstance(check_runs, list):
        for raw in check_runs:
            if not isinstance(raw, dict) or raw.get("head_sha") != head_sha:
                continue
            conclusion = raw.get("conclusion")
            status = raw.get("status")
            mapped = "pending" if status != "completed" else _conclusion(str(conclusion))
            raw_output = raw.get("output")
            output: dict[object, object] = raw_output if isinstance(raw_output, dict) else {}
            detail = "\n".join(
                str(output.get(key, "")) for key in ("title", "summary", "text")
            ).strip()[-8_000:]
            name = str(raw.get("name") or "unnamed-check")
            parsed[name] = CiCheckResult(
                name=name,
                status=mapped,
                details_url=str(raw["details_url"]) if raw.get("details_url") else None,
                output=detail,
            )
    statuses = status_data.get("statuses", []) if isinstance(status_data, dict) else []
    if isinstance(statuses, list):
        for raw in statuses:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("context") or "unnamed-status")
            if name in parsed:
                continue
            state = str(raw.get("state"))
            mapped = {"success": "passed", "failure": "failed", "error": "failed"}.get(
                state, "pending"
            )
            parsed[name] = CiCheckResult(
                name=name,
                status=mapped,
                details_url=str(raw["target_url"]) if raw.get("target_url") else None,
                output=str(raw.get("description") or "")[-8_000:],
            )
    return [parsed[name] for name in sorted(parsed)]


def _required_checks(
    observed: list[CiCheckResult], protection: object | None
) -> list[CiCheckResult]:
    """Require every protected context, or wait until unprotected checks appear."""

    contexts: set[str] = set()
    if isinstance(protection, dict):
        raw_contexts = protection.get("contexts", [])
        if isinstance(raw_contexts, list):
            contexts.update(str(item) for item in raw_contexts if item)
        raw_checks = protection.get("checks", [])
        if isinstance(raw_checks, list):
            contexts.update(
                str(item["context"])
                for item in raw_checks
                if isinstance(item, dict) and item.get("context")
            )
    by_name = {item.name: item for item in observed}
    if contexts:
        return [
            by_name.get(
                name,
                CiCheckResult(
                    name=name,
                    status="pending",
                    output="Required check has not reported for this exact head.",
                ),
            )
            for name in sorted(contexts)
        ]
    if observed:
        return observed
    return [
        CiCheckResult(
            name=_CHECK_DISCOVERY,
            status="pending",
            output="No checks have reported for this exact head yet.",
        )
    ]


def _conclusion(value: str) -> str:
    return {
        "success": "passed",
        "neutral": "passed",
        "skipped": "skipped",
        "cancelled": "cancelled",
    }.get(value, "failed")
