"""Temporary-repository tests for exact Git boundaries and serial integration."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from orkastrator.git import GitError, LocalGit, worktree_path
from orkastrator.models import ValidationRequirement


def run(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(arguments, cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def run_bytes(cwd: Path, *arguments: str) -> bytes:
    """Capture a command's stdout without decoding it."""

    return subprocess.run(arguments, cwd=cwd, check=True, capture_output=True).stdout


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


async def test_base_resolution_fetches_and_reports_a_stale_tracking_branch(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    run(tmp_path, "git", "init", "--bare", str(remote))
    source, local_base = repository(tmp_path)
    run(source, "git", "remote", "add", "origin", str(remote))
    run(source, "git", "push", "-u", "origin", "main")
    lane_id = add_worktree(source, tmp_path / "lane", "lane", local_base)
    run(source, "git", "branch", "--set-upstream-to", "origin/main", "main")

    other = tmp_path / "other"
    run(tmp_path, "git", "clone", str(remote), str(other))
    run(other, "git", "config", "user.name", "orkastrator test")
    run(other, "git", "config", "user.email", "orkastrator@example.test")
    run(other, "git", "checkout", "main")
    remote_head = commit(other, "src/remote.py", "remote\n", "remote change")
    run(other, "git", "push", "origin", "main")

    git = LocalGit()
    resolution = await git.resolve_base(lane_id, "main")
    await git.pin_base(lane_id, resolution.base_sha)

    assert resolution.requested_sha == local_base
    assert resolution.resolved_ref == "origin/main"
    assert resolution.base_sha == remote_head
    assert resolution.tracking_sha == remote_head
    assert resolution.behind_by == 1
    assert await git.head(lane_id) == remote_head


async def test_base_resolution_ignores_an_unrelated_unreachable_remote(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    run(tmp_path, "git", "init", "--bare", str(remote))
    source, local_base = repository(tmp_path)
    run(source, "git", "remote", "add", "origin", str(remote))
    run(source, "git", "push", "-u", "origin", "main")
    lane_id = add_worktree(source, tmp_path / "lane", "lane", local_base)
    run(source, "git", "branch", "--set-upstream-to", "origin/main", "main")

    other = tmp_path / "other"
    run(tmp_path, "git", "clone", str(remote), str(other))
    run(other, "git", "config", "user.name", "orkastrator test")
    run(other, "git", "config", "user.email", "orkastrator@example.test")
    run(other, "git", "checkout", "main")
    remote_head = commit(other, "src/remote.py", "remote\n", "remote change")
    run(other, "git", "push", "origin", "main")
    run(source, "git", "remote", "add", "upstream", str(tmp_path / "missing.git"))

    resolution = await LocalGit().resolve_base(lane_id, "main")

    assert resolution.requested_sha == local_base
    assert resolution.resolved_ref == "origin/main"
    assert resolution.base_sha == remote_head
    assert resolution.tracking_sha == remote_head
    assert resolution.behind_by == 1


async def test_base_resolution_does_not_rewind_an_ahead_local_branch(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    run(tmp_path, "git", "init", "--bare", str(remote))
    source, _base = repository(tmp_path)
    run(source, "git", "remote", "add", "origin", str(remote))
    run(source, "git", "push", "-u", "origin", "main")
    local_head = commit(source, "src/local.py", "local\n", "local change")
    lane_id = add_worktree(source, tmp_path / "lane", "lane", local_head)

    git = LocalGit()
    resolution = await git.resolve_base(lane_id, "main")
    await git.pin_base(lane_id, resolution.base_sha)

    assert resolution.requested_sha == local_head
    assert resolution.resolved_ref == "main"
    assert resolution.base_sha == local_head
    assert resolution.tracking_ref == "origin/main"
    assert resolution.tracking_sha == _base
    assert resolution.behind_by == 0
    assert await git.head(lane_id) == local_head
    assert await git.is_ancestor(lane_id, resolution.base_sha, local_head)


async def test_divergent_tracking_base_refuses_with_both_shas_and_distance(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    run(tmp_path, "git", "init", "--bare", str(remote))
    source, _base = repository(tmp_path)
    run(source, "git", "remote", "add", "origin", str(remote))
    run(source, "git", "push", "-u", "origin", "main")
    local_head = commit(source, "src/local.py", "local\n", "local change")
    lane_id = add_worktree(source, tmp_path / "lane", "lane", local_head)

    other = tmp_path / "other"
    run(tmp_path, "git", "clone", str(remote), str(other))
    run(other, "git", "config", "user.name", "orkastrator test")
    run(other, "git", "config", "user.email", "orkastrator@example.test")
    run(other, "git", "checkout", "main")
    remote_head = commit(other, "src/remote.py", "remote\n", "remote change")
    run(other, "git", "push", "origin", "main")

    with pytest.raises(GitError) as failure:
        await LocalGit().resolve_base(lane_id, "main")

    message = str(failure.value)
    assert local_head in message
    assert remote_head in message
    assert "1 commit(s) behind" in message


async def test_explicit_older_commit_remains_the_requested_base(tmp_path: Path) -> None:
    repo, older = repository(tmp_path)
    newer = commit(repo, "src/newer.py", "newer\n", "newer")
    lane_id = add_worktree(repo, tmp_path / "lane", "lane", newer)

    resolution = await LocalGit().resolve_base(lane_id, older)

    assert resolution.requested_ref == older
    assert resolution.resolved_ref == older
    assert resolution.base_sha == older
    assert resolution.tracking_ref is None
    assert resolution.behind_by == 0


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
    assert hashlib.sha256(complete).hexdigest() == await git.diff_sha256(lane_id, base, head)


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

    expected = run_bytes(lane, "git", "diff", "--binary", "--full-index", f"{base}..{head}", "--")
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


async def test_coverage_is_read_from_the_checkout_that_will_run_the_command(
    tmp_path: Path,
) -> None:
    """Ask the repository whether `--no-cov` is a flag it has.

    pytest-cov defines that option, so a repository configuring no coverage
    exits on `unrecognized arguments: --no-cov` rather than ignoring it. The
    answer therefore has to come from the checkout the command will run in, not
    from the supervisor's own repository.
    """

    repo, base = repository(tmp_path)
    worktree = add_worktree(repo, tmp_path / "wt", "coverage", base)
    git = LocalGit()

    assert await git.pytest_coverage_configured(worktree) is False

    (worktree_path(worktree) / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '--cov=pkg'\ntestpaths = ['tests']\n"
    )
    assert await git.pytest_coverage_configured(worktree) is True

    # A coverage flag in a neighbouring section is not this repository asking
    # pytest for coverage, and reading it as one puts the fatal flag back.
    (worktree_path(worktree) / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n\n[tool.coverage.run]\n"
        "source = ['--cov']\n"
    )
    assert await git.pytest_coverage_configured(worktree) is False

    # No checkout to read is the caller's refusal to make, not this one's.
    assert await git.pytest_coverage_configured(None) is True
