"""Deterministic local Git operations for fixer isolation and integration."""

from __future__ import annotations

import asyncio
import hashlib
import shlex
from dataclasses import dataclass
from pathlib import Path

from kasgraph.models import ValidationRequirement, ValidationResult


class GitError(RuntimeError):
    """Raised when a repository operation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    """Bounded output from one Git or validation subprocess."""

    returncode: int
    stdout: str
    stderr: str


class LocalGit:
    """Operate only on explicit local Orca worktree identities."""

    async def head(self, worktree_id: str) -> str:
        """Return the exact checked-out commit."""

        return (await self._git(worktree_id, "rev-parse", "HEAD")).stdout.strip()

    async def resolve_ref(self, worktree_id: str, ref: str) -> str:
        """Resolve a configured lane base ref to one commit."""

        return (await self._git(worktree_id, "rev-parse", f"{ref}^{{commit}}")).stdout.strip()

    async def changed_paths(self, worktree_id: str, base_sha: str, head_sha: str) -> list[str]:
        """Return normalized paths changed by one exact commit range."""

        result = await self._git(
            worktree_id,
            "diff",
            "--name-only",
            "--no-renames",
            f"{base_sha}..{head_sha}",
            "--",
        )
        return sorted({line for line in result.stdout.splitlines() if line})

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
        """Abort only the cherry-pick started by Kasgraph."""

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

    async def validate(
        self, worktree_id: str, requirements: list[ValidationRequirement]
    ) -> list[ValidationResult]:
        """Run configured validation commands without a shell."""

        cwd = worktree_path(worktree_id)
        results: list[ValidationResult] = []
        for requirement in requirements:
            arguments = shlex.split(requirement.command)
            if not arguments:
                raise GitError("validation command cannot be empty")
            process = await asyncio.create_subprocess_exec(
                *arguments,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            output = (stdout + stderr).decode(errors="replace")[-8_000:]
            results.append(
                ValidationResult(
                    command=requirement.command,
                    status="passed" if process.returncode == 0 else "failed",
                    output=output,
                )
            )
            if process.returncode != 0:
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


def worktree_path(worktree_id: str) -> Path:
    """Resolve the local path from Orca's stable ``repo::path`` identity."""

    _, separator, raw_path = worktree_id.partition("::")
    path = Path(raw_path)
    if not separator or not path.is_absolute():
        raise GitError(f"worktree is not a local Orca identity: {worktree_id}")
    if not path.is_dir():
        raise GitError(f"worktree path does not exist: {path}")
    return path
