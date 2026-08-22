"""Authorized lane publication and exact-revision GitHub CI observation."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kasgraph.models import CiCheckResult, CiReceipt, LaneRecord, PublicationReceipt


class PublicationError(RuntimeError):
    """Raised when remote state cannot be mutated or observed safely."""


class LanePublisher(Protocol):
    """Provider boundary used after a lane converges locally."""

    async def publish(
        self,
        *,
        run_id: str,
        lane: LaneRecord,
        head_sha: str,
        previous: PublicationReceipt | None,
    ) -> PublicationReceipt: ...

    async def checks(self, receipt: PublicationReceipt) -> CiReceipt: ...

    async def mark_ready(self, receipt: PublicationReceipt) -> PublicationReceipt: ...


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


class GitHubPublisher:
    """Publish deterministic branches and observe GitHub checks at an exact SHA."""

    def __init__(
        self,
        *,
        gh_command: tuple[str, ...] = ("gh",),
        runner: CommandRunner | None = None,
    ):
        self._gh = gh_command
        self._runner = runner or SubprocessCommandRunner()

    async def publish(
        self,
        *,
        run_id: str,
        lane: LaneRecord,
        head_sha: str,
        previous: PublicationReceipt | None,
    ) -> PublicationReceipt:
        path = _worktree_path(lane)
        remote_url = await self._required(path, "git", "remote", "get-url", "origin")
        repository = _github_repository(remote_url.strip())
        default_branch = await self._default_branch(path, repository)
        branch = f"kasgraph/{run_id[:12]}/{lane.name}"
        if previous is not None and (
            previous.branch != branch or previous.remote_url != remote_url.strip()
        ):
            raise PublicationError("persisted publication target changed")
        remote_head = await self._remote_head(path, branch)
        if previous is None and remote_head is not None:
            raise PublicationError(f"refusing to overwrite existing unowned branch {branch}")
        if previous is not None and remote_head != previous.head_sha:
            raise PublicationError(
                f"remote branch {branch} diverged from published head {previous.head_sha}"
            )
        await self._required(path, "git", "push", "origin", f"{head_sha}:refs/heads/{branch}")

        pull_requests = await self._gh_json(
            path,
            "pr",
            "list",
            "--repo",
            repository,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "url,isDraft",
            "--limit",
            "1",
        )
        body = _pull_request_body(run_id, lane, head_sha)
        if not isinstance(pull_requests, list):
            raise PublicationError("GitHub returned an invalid pull-request list")
        if pull_requests:
            pull_request_url = str(pull_requests[0]["url"])
            await self._required(path, *self._gh, "pr", "edit", pull_request_url, "--body", body)
            draft = bool(pull_requests[0].get("isDraft", True))
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
                f"{lane.issue_id}: {lane.name.replace('-', ' ')}",
                "--body",
                body,
            )
            pull_request_url = result.strip().splitlines()[-1]
            draft = True
        return PublicationReceipt(
            run_id=run_id,
            lane=lane.name,
            remote_url=remote_url.strip(),
            branch=branch,
            pull_request_url=pull_request_url,
            head_sha=head_sha,
            draft=draft,
        )

    async def checks(self, receipt: PublicationReceipt) -> CiReceipt:
        path = Path.cwd()
        repository = _github_repository(receipt.remote_url)
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
        checks = _parse_checks(check_data, status_data, receipt.head_sha)
        if any(item.status in {"failed", "cancelled"} for item in checks):
            status = "failed"
        elif any(item.status == "pending" for item in checks):
            status = "pending"
        else:
            status = "passed"
        return CiReceipt(provider="github", head_sha=receipt.head_sha, status=status, checks=checks)

    async def mark_ready(self, receipt: PublicationReceipt) -> PublicationReceipt:
        if not receipt.draft:
            return receipt
        await self._required(Path.cwd(), *self._gh, "pr", "ready", receipt.pull_request_url)
        return receipt.model_copy(update={"draft": False})

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

    async def _required(self, path: Path, *arguments: str) -> str:
        result = await self._runner.run(path, *arguments)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:2_000]
            raise PublicationError(f"{' '.join(arguments)} failed: {detail}")
        return result.stdout


def _worktree_path(lane: LaneRecord) -> Path:
    if lane.worktree_id is None:
        raise PublicationError("lane has no Orca worktree")
    _, separator, raw_path = lane.worktree_id.partition("::")
    path = Path(raw_path)
    if not separator or not path.is_absolute() or not path.is_dir():
        raise PublicationError(f"lane worktree is not a local Orca identity: {lane.worktree_id}")
    return path


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
        "unsupported remote provider; Kasgraph publication currently supports GitHub remotes"
    )


def _pull_request_body(run_id: str, lane: LaneRecord, head_sha: str) -> str:
    return (
        f"Kasgraph run: `{run_id}`\n\n"
        f"Lane: `{lane.name}`\n"
        f"Issue: `{lane.issue_id}`\n"
        f"Published head: `{head_sha}`\n\n"
        "This pull request remains draft until required checks pass for this exact head."
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


def _conclusion(value: str) -> str:
    return {
        "success": "passed",
        "neutral": "passed",
        "skipped": "skipped",
        "cancelled": "cancelled",
    }.get(value, "failed")
