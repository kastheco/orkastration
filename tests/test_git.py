"""Temporary-repository tests for exact Git boundaries and serial integration."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from orkastrator.git import LocalGit
from orkastrator.models import ValidationRequirement


def run(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(arguments, cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def commit(repo: Path, path: str, content: str, message: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    run(repo, "git", "add", path)
    run(repo, "git", "commit", "-m", message)
    return run(repo, "git", "rev-parse", "HEAD")


def repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(repo, "git", "init", "-b", "main")
    run(repo, "git", "config", "user.name", "orkastrator test")
    run(repo, "git", "config", "user.email", "orkastrator@example.test")
    base = commit(repo, "src/shared.py", "base\n", "base")
    return repo, base


def add_worktree(repo: Path, path: Path, branch: str, base: str) -> str:
    run(repo, "git", "worktree", "add", "-b", branch, str(path), base)
    return f"repo::{path}"


async def test_disjoint_fixer_commits_integrate_serially(tmp_path: Path) -> None:
    repo, base = repository(tmp_path)
    lane = tmp_path / "lane"
    fixer_one = tmp_path / "fixer-one"
    fixer_two = tmp_path / "fixer-two"
    lane_id = add_worktree(repo, lane, "lane", base)
    one_id = add_worktree(repo, fixer_one, "fix-one", base)
    two_id = add_worktree(repo, fixer_two, "fix-two", base)
    one_sha = commit(fixer_one, "src/one.py", "one\n", "fix one")
    two_sha = commit(fixer_two, "src/two.py", "two\n", "fix two")
    git = LocalGit()

    assert await git.changed_paths(one_id, base, one_sha) == ["src/one.py"]
    assert await git.changed_paths(two_id, base, two_sha) == ["src/two.py"]
    assert await git.is_ancestor(one_id, base, one_sha)
    assert await git.commit_count(one_id, base, one_sha) == 1
    assert len(await git.diff_sha256(one_id, base, one_sha)) == 64
    assert (await git.cherry_pick(lane_id, one_sha)).returncode == 0
    first_head = await git.head(lane_id)
    assert (await git.cherry_pick(lane_id, two_sha)).returncode == 0
    final_head = await git.head(lane_id)

    assert first_head != final_head
    assert (lane / "src/one.py").read_text() == "one\n"
    assert (lane / "src/two.py").read_text() == "two\n"
    assert await git.find_cherry_pick(lane_id, one_sha) == first_head


async def test_render_diff_returns_the_complete_path_scoped_frozen_text(tmp_path: Path) -> None:
    repo, base = repository(tmp_path)
    lane = tmp_path / "render"
    lane_id = add_worktree(repo, lane, "render", base)
    commit(lane, "src/one.py", "one\n", "add one")
    head = commit(lane, "src/two.py", "two\n", "add two")
    git = LocalGit()

    rendered = await git.render_diff(lane_id, base, head, ["src/two.py"])
    complete = await git.render_diff(lane_id, base, head)

    assert b"diff --git a/src/two.py b/src/two.py" in rendered
    assert b"+two" in rendered
    assert b"src/one.py" not in rendered
    assert await git.changed_paths(lane_id, base, head, ["src/two.py"]) == ["src/two.py"]
    assert hashlib.sha256(complete).hexdigest() == await git.diff_sha256(
        lane_id, base, head
    )


async def test_render_diff_preserves_non_utf8_frozen_bytes(tmp_path: Path) -> None:
    repo, base = repository(tmp_path)
    lane = tmp_path / "render"
    lane_id = add_worktree(repo, lane, "render", base)
    target = lane / "src" / "latin1.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"value = 'caf\xe9'\n")
    run(lane, "git", "add", "src/latin1.py")
    run(lane, "git", "commit", "-m", "add latin1 source")
    head = run(lane, "git", "rev-parse", "HEAD")
    git = LocalGit()

    rendered = await git.render_diff(lane_id, base, head)

    expected = subprocess.run(
        ["git", "diff", "--binary", "--full-index", f"{base}..{head}", "--"],
        cwd=lane,
        check=True,
        capture_output=True,
    ).stdout
    assert rendered == expected
    assert hashlib.sha256(rendered).hexdigest() == await git.diff_sha256(lane_id, base, head)


async def test_conflicting_commit_aborts_without_resetting_prior_head(tmp_path: Path) -> None:
    repo, base = repository(tmp_path)
    lane = tmp_path / "lane"
    fixer_one = tmp_path / "fixer-one"
    fixer_two = tmp_path / "fixer-two"
    lane_id = add_worktree(repo, lane, "lane", base)
    one_id = add_worktree(repo, fixer_one, "fix-one", base)
    two_id = add_worktree(repo, fixer_two, "fix-two", base)
    one_sha = commit(fixer_one, "src/shared.py", "one\n", "fix one")
    two_sha = commit(fixer_two, "src/shared.py", "two\n", "fix two")
    git = LocalGit()

    assert await git.changed_paths(one_id, base, one_sha) == ["src/shared.py"]
    assert await git.changed_paths(two_id, base, two_sha) == ["src/shared.py"]
    assert (await git.cherry_pick(lane_id, one_sha)).returncode == 0
    prior_head = await git.head(lane_id)
    assert (await git.cherry_pick(lane_id, two_sha)).returncode != 0
    assert await git.cherry_pick_in_progress_commits(lane_id) == [two_sha]
    await git.abort_cherry_pick(lane_id)

    assert await git.head(lane_id) == prior_head
    assert await git.is_clean(lane_id)
    assert (lane / "src/shared.py").read_text() == "one\n"


async def test_dirty_state_and_validation_are_observed_without_shell(tmp_path: Path) -> None:
    repo, base = repository(tmp_path)
    lane = tmp_path / "lane"
    lane_id = add_worktree(repo, lane, "lane", base)
    git = LocalGit()
    (lane / "unrelated.txt").write_text("owner work\n")

    assert not await git.is_clean(lane_id)
    results = await git.validate(
        lane_id,
        [ValidationRequirement(command="git diff --check", expected="passes")],
    )
    assert results[0].status == "passed"


async def test_unrunnable_validation_command_fails_the_finding_not_the_tick(
    tmp_path: Path,
) -> None:
    repo, base = repository(tmp_path)
    lane = tmp_path / "unrunnable"
    lane_id = add_worktree(repo, lane, "unrunnable", base)

    results = await LocalGit().validate(
        lane_id,
        [
            ValidationRequirement(
                command="cd app/ui && npx tsc -b", expected="Both exit 0 with no diagnostics."
            )
        ],
    )

    assert [item.status for item in results] == ["failed"]
    assert "without a shell" in results[0].output


async def test_a_requirement_runs_in_its_own_workdir(tmp_path: Path) -> None:
    repo, base = repository(tmp_path)
    lane = tmp_path / "workdir"
    lane_id = add_worktree(repo, lane, "workdir", base)
    (lane / "app" / "ui").mkdir(parents=True)
    (lane / "app" / "ui" / "marker.txt").write_text("here\n")

    results = await LocalGit().validate(
        lane_id,
        [ValidationRequirement(command="cat marker.txt", expected="prints here", workdir="app/ui")],
    )

    assert [item.status for item in results] == ["passed"]
    assert "here" in results[0].output


async def test_a_missing_workdir_fails_the_finding_not_the_tick(tmp_path: Path) -> None:
    repo, base = repository(tmp_path)
    lane = tmp_path / "missing"
    lane_id = add_worktree(repo, lane, "missing", base)

    results = await LocalGit().validate(
        lane_id,
        [ValidationRequirement(command="true", expected="exit 0", workdir="app/ui")],
    )

    assert [item.status for item in results] == ["failed"]
    assert "workdir does not exist" in results[0].output


async def test_an_absence_check_passes_on_its_own_expected_exit(tmp_path: Path) -> None:
    repo, base = repository(tmp_path)
    lane = tmp_path / "absence"
    lane_id = add_worktree(repo, lane, "absence", base)
    git = LocalGit()

    gone = await git.validate(
        lane_id,
        [
            ValidationRequirement(
                command="rg -c removed_symbol src/shared.py",
                expected="no match (exit status 1)",
                expect_exit=1,
            )
        ],
    )
    still_there = await git.validate(
        lane_id,
        [
            ValidationRequirement(
                command="rg -c base src/shared.py",
                expected="no match (exit status 1)",
                expect_exit=1,
            )
        ],
    )

    assert [item.status for item in gone] == ["passed"]
    assert [item.status for item in still_there] == ["failed"]
