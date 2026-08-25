"""Deterministic local Git operations for fixer isolation and integration."""

from __future__ import annotations

import asyncio
import hashlib
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from orkastrator.models import ValidationRequirement, ValidationResult
from orkastrator.runners import condense


class GitError(RuntimeError):
    """Raised when a repository operation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    """Bounded output from one Git or validation subprocess."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class BaseResolution:
    """Fetched source identity used to freeze one lane base."""

    requested_ref: str
    requested_sha: str
    resolved_ref: str
    base_sha: str
    tracking_ref: str | None = None
    tracking_sha: str | None = None
    behind_by: int = 0


class LocalGit:
    """Operate only on explicit local Orca worktree identities."""

    async def head(self, worktree_id: str) -> str:
        """Return the exact checked-out commit."""

        return (await self._git(worktree_id, "rev-parse", "HEAD")).stdout.strip()

    async def resolve_ref(self, worktree_id: str, ref: str) -> str:
        """Resolve a configured lane base ref to one commit."""

        return (await self._git(worktree_id, "rev-parse", f"{ref}^{{commit}}")).stdout.strip()

    async def resolve_base(self, worktree_id: str, ref: str) -> BaseResolution:
        """Fetch, then resolve a lane base and its remote tracking source."""

        await self._git(worktree_id, "fetch", "--all", "--prune")
        requested_sha = await self.resolve_ref(worktree_id, ref)
        symbolic = await self._git(
            worktree_id, "rev-parse", "--symbolic-full-name", ref, check=False
        )
        full_ref = symbolic.stdout.strip() if symbolic.returncode == 0 else ""
        tracking_ref: str | None = None
        if full_ref.startswith("refs/heads/"):
            upstream = await self._git(
                worktree_id,
                "for-each-ref",
                "--format=%(upstream:short)",
                full_ref,
            )
            tracking_ref = upstream.stdout.strip() or None
        if tracking_ref is None:
            return BaseResolution(
                requested_ref=ref,
                requested_sha=requested_sha,
                resolved_ref=ref,
                base_sha=requested_sha,
            )

        tracking_sha = await self.resolve_ref(worktree_id, tracking_ref)
        counts = await self._git(
            worktree_id,
            "rev-list",
            "--left-right",
            "--count",
            f"{requested_sha}...{tracking_sha}",
        )
        try:
            ahead_by, behind_by = (int(value) for value in counts.stdout.split())
        except ValueError as exc:
            raise GitError(
                f"git rev-list returned invalid ahead/behind counts: {counts.stdout!r}"
            ) from exc
        if ahead_by and behind_by:
            raise GitError(
                f"base ref {ref} resolved to {requested_sha}, which is {behind_by} commit(s) "
                f"behind its remote tracking branch {tracking_ref} at {tracking_sha} and "
                f"{ahead_by} commit(s) ahead; refusing to discard the divergent local commits"
            )
        return BaseResolution(
            requested_ref=ref,
            requested_sha=requested_sha,
            resolved_ref=tracking_ref,
            base_sha=tracking_sha,
            tracking_ref=tracking_ref,
            tracking_sha=tracking_sha,
            behind_by=behind_by,
        )

    async def pin_base(self, worktree_id: str, base_sha: str) -> None:
        """Move a clean, newly accepted lane checkout to its fetched base."""

        if await self.head(worktree_id) == base_sha:
            return
        if not await self.is_clean(worktree_id):
            raise GitError(f"cannot move dirty lane checkout to fetched base {base_sha}")
        await self._git(worktree_id, "reset", "--hard", base_sha)

    async def changed_paths(
        self,
        worktree_id: str,
        base_sha: str,
        head_sha: str,
        paths: Sequence[str] = (),
    ) -> list[str]:
        """Return normalized paths changed by one exact commit range."""

        result = await self._git(
            worktree_id,
            "diff",
            "--name-only",
            "--no-renames",
            f"{base_sha}..{head_sha}",
            "--",
            *paths,
        )
        return sorted({line for line in result.stdout.splitlines() if line})

    async def render_diff(
        self,
        worktree_id: str,
        base_sha: str,
        head_sha: str,
        paths: Sequence[str] = (),
    ) -> bytes:
        """Render the binary-capable frozen diff without decoding its stdout."""

        cwd = worktree_path(worktree_id)
        process = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--binary",
            "--full-index",
            f"{base_sha}..{head_sha}",
            "--",
            *paths,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise GitError(f"git diff failed: {stderr.decode(errors='replace')[:2_000]}")
        return stdout

    async def diff_sha256(self, worktree_id: str, base_sha: str, head_sha: str) -> str:
        """Hash the exact binary-capable diff frozen for review."""

        cwd = worktree_path(worktree_id)
        process = await asyncio.create_subprocess_exec(
            "git",
            "diff",
            "--binary",
            "--full-index",
            f"{base_sha}..{head_sha}",
            "--",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise GitError(f"git diff failed: {stderr.decode(errors='replace')[:2_000]}")
        return hashlib.sha256(stdout).hexdigest()

    async def is_ancestor(self, worktree_id: str, base_sha: str, head_sha: str) -> bool:
        """Return whether the assigned base is an ancestor of the reported head."""

        result = await self._git(
            worktree_id, "merge-base", "--is-ancestor", base_sha, head_sha, check=False
        )
        if result.returncode not in {0, 1}:
            raise GitError(f"git merge-base failed: {result.stderr.strip()[:2_000]}")
        return result.returncode == 0

    async def commit_count(self, worktree_id: str, base_sha: str, head_sha: str) -> int:
        """Count commits in one assigned fixer range."""

        result = await self._git(worktree_id, "rev-list", "--count", f"{base_sha}..{head_sha}")
        return int(result.stdout.strip())

    async def commits_between(self, worktree_id: str, base_sha: str, head_sha: str) -> list[str]:
        """Return source commits in application order for an accepted composite fix."""

        result = await self._git(worktree_id, "rev-list", "--reverse", f"{base_sha}..{head_sha}")
        return [line for line in result.stdout.splitlines() if line]

    async def is_clean(self, worktree_id: str) -> bool:
        """Return whether tracked and untracked worktree state is empty."""

        result = await self._git(worktree_id, "status", "--porcelain=v1", "--untracked-files=all")
        return not result.stdout.strip()

    async def cherry_pick(self, worktree_id: str, commit_sha: str) -> GitCommandResult:
        """Apply one approved commit with an auditable source trailer."""

        return await self.cherry_pick_many(worktree_id, [commit_sha])

    async def cherry_pick_many(self, worktree_id: str, commit_shas: list[str]) -> GitCommandResult:
        """Apply one accepted source chain as a single abortable sequence."""

        if not commit_shas:
            raise GitError("integration source commit list cannot be empty")
        return await self._git(worktree_id, "cherry-pick", "-x", *commit_shas, check=False)

    async def abort_cherry_pick(self, worktree_id: str) -> None:
        """Abort only the cherry-pick started by orkastrator."""

        result = await self._git(worktree_id, "cherry-pick", "--abort", check=False)
        if result.returncode != 0:
            raise GitError(f"failed to abort cherry-pick: {result.stderr.strip()[:2_000]}")

    async def cherry_pick_in_progress_commits(self, worktree_id: str) -> list[str] | None:
        """Return source commits owned by the active Git sequencer, if any."""

        cwd = worktree_path(worktree_id)
        commits: list[str] = []
        head = await self._git(
            worktree_id, "rev-parse", "--verify", "-q", "CHERRY_PICK_HEAD", check=False
        )
        if head.returncode == 0 and head.stdout.strip():
            commits.append(head.stdout.strip())
        todo_result = await self._git(worktree_id, "rev-parse", "--git-path", "sequencer/todo")
        todo = Path(todo_result.stdout.strip())
        if not todo.is_absolute():
            todo = cwd / todo
        if todo.exists():
            for line in todo.read_text(errors="replace").splitlines():
                parts = line.split(maxsplit=2)
                if len(parts) >= 2 and parts[0] in {"pick", "p"}:
                    resolved = await self._git(worktree_id, "rev-parse", f"{parts[1]}^{{commit}}")
                    commits.append(resolved.stdout.strip())
        if commits:
            return list(dict.fromkeys(commits))
        sequencer_result = await self._git(worktree_id, "rev-parse", "--git-path", "sequencer")
        sequencer = Path(sequencer_result.stdout.strip())
        if not sequencer.is_absolute():
            sequencer = cwd / sequencer
        return [] if sequencer.exists() else None

    async def find_cherry_pick(self, worktree_id: str, commit_sha: str) -> str | None:
        """Find an already-integrated commit after a create-before-record crash."""

        result = await self._git(
            worktree_id,
            "log",
            "--format=%H%x00%B%x00%x00",
            "--fixed-strings",
            "--grep",
            f"(cherry picked from commit {commit_sha})",
            check=False,
        )
        if result.returncode != 0 or not result.stdout:
            return None
        return result.stdout.split("\x00", maxsplit=1)[0].strip() or None

    async def pytest_coverage_configured(self, worktree_id: str | None) -> bool:
        """Report whether a checkout's own pytest configuration turns coverage on.

        `--no-cov` is a pytest-cov flag, so it exists only where pytest-cov loads.
        A repository that configures no coverage does not quietly ignore it: pytest
        exits on `unrecognized arguments: --no-cov` before collecting a test, and
        a check governed that way fails identically on every head forever. Read as
        text rather than parsed, because the question is only whether a coverage
        flag appears in this repository's pytest section, and answering it must
        never fail on a config shape no parser here anticipated. No checkout to
        read keeps the flag, leaving that refusal to the caller that owns it.
        """

        if worktree_id is None:
            return True
        root = worktree_path(worktree_id)
        for name, section in _PYTEST_CONFIG_SECTIONS:
            try:
                text = (root / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            _, marker, tail = text.partition(section)
            if not marker:
                continue
            body, separator, _ = tail.partition("\n[")
            if "--cov" in (body if separator else tail):
                return True
        return False

    async def validate(
        self, worktree_id: str, requirements: list[ValidationRequirement]
    ) -> list[ValidationResult]:
        """Run configured validation commands without a shell.

        A command that cannot be executed is a failed validation, never a raised
        error. Reviewers write these commands, and a reviewer that writes
        `cd app/ui && npx tsc -b` has written something exec cannot run - which is
        worth failing that finding over, and is not worth killing the monitor tick
        that every other lane in the run is waiting on. Say what the rule is in the
        output so the adjudicator can revise the contract instead of guessing.
        """

        root = worktree_path(worktree_id)
        results: list[ValidationResult] = []
        for requirement in requirements:
            cwd = root if requirement.workdir is None else root / requirement.workdir
            if not cwd.is_dir():
                results.append(
                    ValidationResult(
                        command=requirement.command,
                        status="failed",
                        output=f"validation workdir does not exist: {requirement.workdir}",
                    )
                )
                break
            arguments = shlex.split(requirement.command)
            if not arguments:
                results.append(
                    ValidationResult(
                        command=requirement.command,
                        status="failed",
                        output="validation command is empty",
                    )
                )
                break
            try:
                process = await asyncio.create_subprocess_exec(
                    *arguments,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                results.append(
                    ValidationResult(
                        command=requirement.command,
                        status="failed",
                        output=(
                            f"could not execute {arguments[0]!r}: {exc}. Validation commands run "
                            "without a shell, so operators, redirection and `cd` are unavailable; "
                            "name one executable and pass its working directory as an argument."
                        ),
                    )
                )
                break
            stdout, stderr = await process.communicate()
            satisfied = process.returncode == requirement.expect_exit
            # Condense before the output becomes contract bytes. Everything this
            # drops would otherwise be handed to an agent and then re-billed on
            # every turn that agent takes afterwards, and a passing suite's
            # progress dots say nothing its summary line does not.
            output = condense(
                (stdout + stderr).decode(errors="replace"),
                returncode=process.returncode or 0,
                satisfied=satisfied,
            )
            results.append(
                ValidationResult(
                    command=requirement.command,
                    status="passed" if satisfied else "failed",
                    output=output,
                )
            )
            if not satisfied:
                break
        return results

    async def _git(self, worktree_id: str, *arguments: str, check: bool = True) -> GitCommandResult:
        cwd = worktree_path(worktree_id)
        process = await asyncio.create_subprocess_exec(
            "git",
            *arguments,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        result = GitCommandResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip()[:2_000]
            raise GitError(f"git {' '.join(arguments)} failed: {detail}")
        return result


_PYTEST_CONFIG_SECTIONS = (
    ("pyproject.toml", "[tool.pytest.ini_options]"),
    ("pytest.ini", "[pytest]"),
    ("tox.ini", "[pytest]"),
    ("setup.cfg", "[tool:pytest]"),
)
"""Where a repository declares pytest options, and the section that holds them."""


def worktree_path(worktree_id: str) -> Path:
    """Resolve the local path from Orca's stable ``repo::path`` identity."""

    _, separator, raw_path = worktree_id.partition("::")
    path = Path(raw_path)
    if not separator or not path.is_absolute():
        raise GitError(f"worktree is not a local Orca identity: {worktree_id}")
    if not path.is_dir():
        raise GitError(f"worktree path does not exist: {path}")
    return path
