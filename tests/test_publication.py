"""GitHub publication adapter tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orkastrator.git import GitError, worktree_path
from orkastrator.models import LanePhase, LaneRecord, PublicationReceipt
from orkastrator.publication import (
    CommandResult,
    GitHubPublisher,
    IntegrationConflict,
    PublicationError,
    PullRequestLanded,
    _github_repository,
    _raise_if_integration_conflict,
)


class QueueRunner:
    def __init__(self, *results: CommandResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, ...]] = []

    async def run(self, cwd: Path, *arguments: str) -> CommandResult:
        self.calls.append(arguments)
        assert self.results, arguments
        return self.results.pop(0)


def result(stdout: str = "", stderr: str = "", returncode: int = 0) -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


def lane(tmp_path: Path) -> LaneRecord:
    return LaneRecord.model_validate(
        {
            "lane_id": "lane-1",
            "run_id": "run-1234567890",
            "name": "issue-123",
            "issue_id": "ISSUE-123",
            "repo_selector": "id:repo",
            "base_ref": "main",
            "phase": LanePhase.ACTIVE,
            "worktree_id": f"repo::{tmp_path}",
            "review_head_sha": "a" * 40,
            "integration_head_sha": "b" * 40,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )


async def test_create_draft_pr_observe_exact_checks_and_mark_ready(tmp_path: Path) -> None:
    sha = "b" * 40
    runner = QueueRunner(
        result("git@github.com:owner/repo.git\n"),
        result(json.dumps({"defaultBranchRef": {"name": "main"}})),
        result("[]"),
        result(),
        result(),
        result("https://github.com/owner/repo/pull/7\n"),
        result(json.dumps({"headRefOid": sha, "state": "OPEN", "isDraft": True})),
        result(
            json.dumps(
                [
                    {
                        "name": "tests",
                        "bucket": "pass",
                        "link": "https://github.com/check/1",
                        "description": "ok",
                    }
                ]
            )
        ),
        result(json.dumps({"headRefOid": sha, "state": "OPEN", "isDraft": True})),
        result(),
    )
    publisher = GitHubPublisher(runner=runner)

    receipt = await publisher.publish(
        run_id="run-1234567890", lane=lane(tmp_path), head_sha=sha, previous=None
    )
    checks = await publisher.checks(receipt)
    ready = await publisher.mark_ready(receipt)

    assert receipt.draft is True
    assert receipt.branch == "orkastrator/run-12345678/issue-123"
    assert checks.status == "passed"
    assert [item.name for item in checks.checks] == ["tests"]
    assert ready.draft is False
    assert "--draft" in runner.calls[5]
    assert runner.calls[-1][-3:] == ("pr", "ready", receipt.pull_request_url)


async def test_land_uses_a_merge_commit_and_records_its_sha() -> None:
    head_sha = "b" * 40
    merge_sha = "c" * 40
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha=head_sha,
        draft=False,
    )
    open_state = {
        "headRefOid": head_sha,
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "mergeCommit": None,
    }
    merged_state = {
        **open_state,
        "state": "MERGED",
        "mergeCommit": {"oid": merge_sha},
    }
    runner = QueueRunner(result(json.dumps(open_state)), result(), result(json.dumps(merged_state)))

    landed = await GitHubPublisher(runner=runner).land(receipt)

    assert landed.landed is True
    assert landed.merge_sha == merge_sha
    merge_call = runner.calls[1]
    assert merge_call[:4] == ("gh", "pr", "merge", receipt.pull_request_url)
    match_head_index = merge_call.index("--match-head-commit")
    assert merge_call[match_head_index + 1] == receipt.head_sha
    assert "--merge" in merge_call
    assert "--squash" not in merge_call
    assert "--rebase" not in merge_call
    assert "--no-verify" not in merge_call
    subject = merge_call[merge_call.index("--subject") + 1]
    assert subject.startswith("feat(publication): ")


async def test_land_reports_a_lane_integration_conflict() -> None:
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha="b" * 40,
        draft=False,
    )
    runner = QueueRunner(
        result(
            json.dumps(
                {
                    "headRefOid": receipt.head_sha,
                    "state": "OPEN",
                    "isDraft": False,
                    "mergeable": "CONFLICTING",
                    "mergeStateStatus": "DIRTY",
                    "mergeCommit": None,
                }
            )
        )
    )

    with pytest.raises(IntegrationConflict, match="conflicts with current main"):
        await GitHubPublisher(runner=runner).land(receipt)

    assert len(runner.calls) == 1


async def test_land_preserves_a_non_conflict_provider_failure() -> None:
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha="b" * 40,
        draft=False,
    )
    open_state = {
        "headRefOid": receipt.head_sha,
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "mergeCommit": None,
    }
    runner = QueueRunner(
        result(json.dumps(open_state)),
        result(stderr="merge commits are disabled", returncode=1),
        result(json.dumps(open_state)),
    )

    with pytest.raises(PublicationError, match="merge commits are disabled"):
        await GitHubPublisher(runner=runner).land(receipt)


async def test_a_merge_sha_requires_a_landed_receipt() -> None:
    with pytest.raises(ValueError, match="requires a landed receipt"):
        PublicationReceipt(
            run_id="run-1234567890",
            lane="issue-123",
            remote_url="git@github.com:owner/repo.git",
            base_branch="main",
            branch="orkastrator/run-12345678/issue-123",
            pull_request_url="https://github.com/owner/repo/pull/7",
            head_sha="b" * 40,
            draft=False,
            merge_sha="c" * 40,
        )


async def test_update_owned_branch_and_existing_pr(tmp_path: Path) -> None:
    old_sha = "a" * 40
    new_sha = "b" * 40
    previous = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha=old_sha,
        draft=True,
    )
    runner = QueueRunner(
        result("git@github.com:owner/repo.git\n"),
        result(json.dumps({"defaultBranchRef": {"name": "main"}})),
        result(
            json.dumps(
                [
                    {
                        "url": previous.pull_request_url,
                        "isDraft": True,
                        "state": "OPEN",
                        "headRefOid": old_sha,
                        "body": "orkastrator run: `run-1234567890`",
                    }
                ]
            )
        ),
        result(f"{old_sha}\trefs/heads/{previous.branch}\n"),
        result(),
        result(),
    )

    receipt = await GitHubPublisher(runner=runner).publish(
        run_id=previous.run_id,
        lane=lane(tmp_path),
        head_sha=new_sha,
        previous=previous,
    )

    assert receipt.head_sha == new_sha
    assert runner.calls[4][:3] == ("git", "push", "origin")
    assert runner.calls[-1][1:3] == ("pr", "edit")


async def test_refuses_divergence_existing_unowned_branch_and_non_github_remote(
    tmp_path: Path,
) -> None:
    previous = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha="a" * 40,
        draft=True,
    )
    diverged = QueueRunner(
        result(previous.remote_url),
        result(json.dumps({"defaultBranchRef": {"name": "main"}})),
        result("[]"),
        result(f"{'c' * 40}\trefs/heads/{previous.branch}\n"),
    )
    with pytest.raises(PublicationError, match="diverged"):
        await GitHubPublisher(runner=diverged).publish(
            run_id=previous.run_id,
            lane=lane(tmp_path),
            head_sha="b" * 40,
            previous=previous,
        )

    unowned = QueueRunner(
        result(previous.remote_url),
        result(json.dumps({"defaultBranchRef": {"name": "main"}})),
        result("[]"),
        result(f"{'c' * 40}\trefs/heads/{previous.branch}\n"),
    )
    with pytest.raises(PublicationError, match="unrelated revision"):
        await GitHubPublisher(runner=unowned).publish(
            run_id=previous.run_id,
            lane=lane(tmp_path),
            head_sha="b" * 40,
            previous=None,
        )

    assert _github_repository("https://github.com/owner/repo") == "owner/repo"
    assert _github_repository("ssh://git@github.com/owner/repo.git") == "owner/repo"
    with pytest.raises(PublicationError, match="unsupported remote provider"):
        _github_repository("git@gitlab.com:owner/repo.git")


async def test_recovers_push_and_pr_created_before_local_receipt(tmp_path: Path) -> None:
    sha = "b" * 40
    branch = "orkastrator/run-12345678/issue-123"
    runner = QueueRunner(
        result("git@github.com:owner/repo.git"),
        result(json.dumps({"defaultBranchRef": {"name": "main"}})),
        result(
            json.dumps(
                [
                    {
                        "url": "https://github.com/owner/repo/pull/7",
                        "isDraft": True,
                        "state": "OPEN",
                        "headRefOid": sha,
                        "body": "orkastrator run: `run-1234567890`",
                    }
                ]
            )
        ),
        result(f"{sha}\trefs/heads/{branch}\n"),
        result(),
    )

    receipt = await GitHubPublisher(runner=runner).publish(
        run_id="run-1234567890", lane=lane(tmp_path), head_sha=sha, previous=None
    )

    assert receipt.head_sha == sha
    assert not any(call[:2] == ("git", "push") for call in runner.calls)

    previous = receipt.model_copy(update={"head_sha": "a" * 40})
    update_runner = QueueRunner(
        result(receipt.remote_url),
        result(json.dumps({"defaultBranchRef": {"name": "main"}})),
        result(
            json.dumps(
                [
                    {
                        "url": receipt.pull_request_url,
                        "isDraft": True,
                        "state": "OPEN",
                        "headRefOid": sha,
                        "body": "orkastrator run: `run-1234567890`",
                    }
                ]
            )
        ),
        result(f"{sha}\trefs/heads/{branch}\n"),
        result(),
    )
    recovered_update = await GitHubPublisher(runner=update_runner).publish(
        run_id=receipt.run_id,
        lane=lane(tmp_path),
        head_sha=sha,
        previous=previous,
    )
    assert recovered_update.head_sha == sha
    assert not any(call[:2] == ("git", "push") for call in update_runner.calls)


async def test_missing_required_check_stays_pending() -> None:
    sha = "b" * 40
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha=sha,
        draft=True,
    )
    runner = QueueRunner(
        result(json.dumps({"headRefOid": sha, "state": "OPEN", "isDraft": True})),
        result(
            json.dumps(
                [
                    {
                        "name": "tests",
                        "bucket": "pending",
                        "link": "https://github.com/check/1",
                        "description": "queued",
                    }
                ]
            ),
            returncode=8,
        ),
    )

    observed = await GitHubPublisher(runner=runner).checks(receipt)

    assert observed.status == "pending"
    assert observed.checks[0].name == "tests"
    assert observed.checks[0].output == "queued"


async def test_a_branch_github_has_not_registered_yet_stays_pending() -> None:
    sha = "c" * 40
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha=sha,
        draft=True,
    )
    runner = QueueRunner(
        result(json.dumps({"headRefOid": sha, "state": "OPEN", "isDraft": True})),
        # `gh pr checks` says this on stderr, with nothing on stdout, for the
        # seconds between pushing a branch and GitHub registering its workflows.
        result("", "no checks reported on the 'orkastrator/run-12345678/issue-123' branch", 1),
        result(json.dumps([{"check_runs": []}])),
        result(json.dumps({"state": "pending", "statuses": []})),
    )

    observed = await GitHubPublisher(runner=runner).checks(receipt)

    # Reading "no checks yet" as a pass would merge a lane whose CI never ran.
    assert observed.status == "pending"
    assert [item.name for item in observed.checks] == ["check-discovery"]


def _no_checks_runner(sha: str) -> QueueRunner:
    return QueueRunner(
        result(json.dumps({"headRefOid": sha, "state": "OPEN", "isDraft": True})),
        result("", "no checks reported on the 'orkastrator/run-12345678/issue-123' branch", 1),
        result(json.dumps([{"check_runs": []}])),
        result(json.dumps({"state": "pending", "statuses": []})),
    )


def _no_checks_receipt(sha: str) -> PublicationReceipt:
    return PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha=sha,
        draft=True,
    )


async def test_a_repository_that_publishes_no_checks_can_be_declared_ungated() -> None:
    """A repository with no workflows never leaves the "nothing reported" state.

    Without an explicit opt-out its lane polls forever: the empty result is read
    as a race that will resolve, and it never does. `treat_no_checks_as_passed`
    is the owner saying the empty result is final for this repository.
    """

    sha = "d" * 40

    observed = await GitHubPublisher(
        runner=_no_checks_runner(sha), treat_no_checks_as_passed=True
    ).checks(_no_checks_receipt(sha))

    assert observed.status == "passed"
    assert [item.name for item in observed.checks] == ["check-discovery"]


async def test_declaring_a_repository_ungated_does_not_pass_a_real_pending_check() -> None:
    """The opt-out answers "this repository reports nothing", not "stop waiting".

    A check that exists and reported pending is still a race worth another tick,
    so the flag must not shortcut it.
    """

    sha = "e" * 40
    runner = QueueRunner(
        result(json.dumps({"headRefOid": sha, "state": "OPEN", "isDraft": True})),
        result(
            json.dumps(
                [
                    {
                        "name": "tests",
                        "bucket": "pending",
                        "link": "https://github.com/check/1",
                        "description": "queued",
                    }
                ]
            ),
            returncode=8,
        ),
    )

    observed = await GitHubPublisher(
        runner=runner, treat_no_checks_as_passed=True
    ).checks(_no_checks_receipt(sha))

    assert observed.status == "pending"
    assert [item.name for item in observed.checks] == ["tests"]


async def test_a_check_the_repository_does_not_enforce_cannot_block_the_lane() -> None:
    """The supervisor must not be stricter than the repository it publishes into.

    kashh's `all-checks` job deletes its Chrome conformance suite from the set it
    requires, by name, while that suite's launch boundary is intermittent. The
    supervisor read every reported check instead and blocked a lane whose four
    enforced suites had all passed. The advisory result still belongs in the
    receipt - it is what the lane was told - it just does not decide anything.
    """

    sha = "d" * 40
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha=sha,
        draft=True,
    )
    reported = [
        {"name": "conformance (advisory)", "bucket": "fail", "link": "", "description": "flaked"},
        {"name": "pytest", "bucket": "pass", "link": "", "description": "ok"},
    ]

    def runner() -> QueueRunner:
        return QueueRunner(
            result(json.dumps({"headRefOid": sha, "state": "OPEN", "isDraft": True})),
            result(json.dumps(reported), returncode=8),
        )

    strict = await GitHubPublisher(runner=runner()).checks(receipt)
    assert strict.status == "failed"

    observed = await GitHubPublisher(
        runner=runner(), advisory_checks=("conformance (advisory)",)
    ).checks(receipt)

    assert observed.status == "passed"
    # Recorded, not hidden: the receipt is the audit of what CI actually said.
    assert [(item.name, item.status) for item in observed.checks] == [
        ("conformance (advisory)", "failed"),
        ("pytest", "passed"),
    ]


async def test_a_head_whose_only_reported_check_is_advisory_stays_pending() -> None:
    """Nothing enforced has spoken yet, which is the same as no checks at all."""

    sha = "e" * 40
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha=sha,
        draft=True,
    )
    runner = QueueRunner(
        result(json.dumps({"headRefOid": sha, "state": "OPEN", "isDraft": True})),
        result(
            json.dumps(
                [
                    {
                        "name": "conformance (advisory)",
                        "bucket": "pass",
                        "link": "",
                        "description": "ok",
                    }
                ]
            ),
            returncode=8,
        ),
    )

    observed = await GitHubPublisher(
        runner=runner, advisory_checks=("conformance (advisory)",)
    ).checks(receipt)

    assert observed.status == "pending"


async def test_ready_transition_recovers_after_remote_success() -> None:
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha="b" * 40,
        draft=True,
    )
    runner = QueueRunner(
        result(json.dumps({"headRefOid": receipt.head_sha, "state": "OPEN", "isDraft": False}))
    )

    ready = await GitHubPublisher(runner=runner).mark_ready(receipt)

    assert ready.draft is False
    assert len(runner.calls) == 1


async def test_closed_lane_pr_blocks_instead_of_creating_another(tmp_path: Path) -> None:
    sha = "b" * 40
    runner = QueueRunner(
        result("git@github.com:owner/repo.git"),
        result(json.dumps({"defaultBranchRef": {"name": "main"}})),
        result(
            json.dumps(
                [
                    {
                        "url": "https://github.com/owner/repo/pull/7",
                        "isDraft": True,
                        "state": "CLOSED",
                        "headRefOid": sha,
                        "body": "orkastrator run: `run-1234567890`",
                    }
                ]
            )
        ),
    )

    with pytest.raises(PublicationError, match="closed without merging"):
        await GitHubPublisher(runner=runner).publish(
            run_id="run-1234567890", lane=lane(tmp_path), head_sha=sha, previous=None
        )


async def test_check_mapping_and_adapter_error_boundaries(tmp_path: Path) -> None:
    sha = "b" * 40
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha=sha,
        draft=False,
    )
    runner = QueueRunner(
        result(json.dumps({"headRefOid": sha, "state": "OPEN", "isDraft": False})),
        result("[]", returncode=1),
        result(
            json.dumps(
                [
                    {
                        "check_runs": [
                            {
                                "name": "failed",
                                "head_sha": sha,
                                "status": "completed",
                                "conclusion": "failure",
                                "output": None,
                            },
                            {
                                "name": "pending",
                                "head_sha": sha,
                                "status": "in_progress",
                                "conclusion": None,
                            },
                            {
                                "name": "skipped",
                                "head_sha": sha,
                                "status": "completed",
                                "conclusion": "skipped",
                            },
                        ]
                    }
                ]
            )
        ),
        result(
            json.dumps(
                {
                    "statuses": [
                        {
                            "context": "legacy",
                            "sha": sha,
                            "state": "success",
                            "description": "ok",
                        }
                    ]
                }
            )
        ),
    )
    publisher = GitHubPublisher(runner=runner)

    observed = await publisher.checks(receipt)

    assert observed.status == "failed"
    assert {item.status for item in observed.checks} == {
        "failed",
        "passed",
        "pending",
        "skipped",
    }
    assert await publisher.mark_ready(receipt) == receipt

    invalid_json = GitHubPublisher(runner=QueueRunner(result("not-json")))
    with pytest.raises(PublicationError, match="invalid JSON"):
        await invalid_json.checks(receipt)

    failed_command = GitHubPublisher(
        runner=QueueRunner(result(stderr="authentication failed", returncode=1))
    )
    with pytest.raises(PublicationError, match="authentication failed"):
        await failed_command.publish(
            run_id=receipt.run_id,
            lane=lane(tmp_path),
            head_sha=sha,
            previous=None,
        )

    missing = lane(tmp_path).model_copy(update={"worktree_id": None})
    with pytest.raises(GitError, match="not a local Orca identity"):
        worktree_path(missing.worktree_id or "")

    bad_default = GitHubPublisher(runner=QueueRunner(result(receipt.remote_url), result("{}")))
    with pytest.raises(PublicationError, match="default branch"):
        await bad_default.publish(
            run_id=receipt.run_id,
            lane=lane(tmp_path),
            head_sha=sha,
            previous=None,
        )


async def test_a_merged_pull_request_is_the_lane_landing_not_a_failure(tmp_path: Path) -> None:
    """MERGED is the outcome the lane was working toward, not a reason to stop it.

    The pull request is also read before anything is pushed. GitHub deletes the
    head branch on merge, and looking at the remote first meant the adapter
    recreated the branch GitHub had just removed before anyone could see why it
    was missing.
    """

    sha = "b" * 40
    runner = QueueRunner(
        result("git@github.com:owner/repo.git"),
        result(json.dumps({"defaultBranchRef": {"name": "main"}})),
        result(
            json.dumps(
                [
                    {
                        "url": "https://github.com/owner/repo/pull/7",
                        "isDraft": False,
                        "state": "MERGED",
                        "headRefOid": sha,
                        "body": "orkastrator run: `run-1234567890`",
                    }
                ]
            )
        ),
    )

    receipt = await GitHubPublisher(runner=runner).publish(
        run_id="run-1234567890", lane=lane(tmp_path), head_sha=sha, previous=None
    )

    assert receipt.landed is True
    assert receipt.draft is False
    assert receipt.head_sha == sha
    assert [call[:2] for call in runner.calls] == [
        ("git", "remote"),
        ("gh", "repo"),
        ("gh", "pr"),
    ]


async def test_a_pull_request_merged_at_another_head_is_refused(tmp_path: Path) -> None:
    """What landed has to be what this lane published, or landing says nothing about it."""

    runner = QueueRunner(
        result("git@github.com:owner/repo.git"),
        result(json.dumps({"defaultBranchRef": {"name": "main"}})),
        result(
            json.dumps(
                [
                    {
                        "url": "https://github.com/owner/repo/pull/7",
                        "isDraft": False,
                        "state": "MERGED",
                        "headRefOid": "c" * 40,
                        "body": "orkastrator run: `run-1234567890`",
                    }
                ]
            )
        ),
    )

    with pytest.raises(PublicationError, match="which is not this lane's integrated head"):
        await GitHubPublisher(runner=runner).publish(
            run_id="run-1234567890", lane=lane(tmp_path), head_sha="b" * 40, previous=None
        )


async def test_a_merge_observed_after_publishing_raises_landed_not_an_error() -> None:
    """`checks` sees the merge when it happens between publishing and observing."""

    sha = "b" * 40
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha=sha,
        draft=True,
    )
    merge_sha = "c" * 40
    runner = QueueRunner(
        result(
            json.dumps(
                {
                    "headRefOid": sha,
                    "state": "MERGED",
                    "isDraft": False,
                    "mergeCommit": {"oid": merge_sha},
                }
            )
        )
    )

    with pytest.raises(PullRequestLanded) as landed:
        await GitHubPublisher(runner=runner).checks(receipt)

    assert landed.value.head_sha == sha
    assert landed.value.receipt.head_sha == sha
    assert landed.value.receipt.merged_head_sha == sha
    assert landed.value.receipt.merge_sha == merge_sha
    assert isinstance(landed.value, PublicationError)


async def test_a_moved_head_is_recorded_when_the_pull_request_is_merged() -> None:
    published_sha = "b" * 40
    merged_head_sha = "c" * 40
    merge_sha = "d" * 40
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha=published_sha,
        draft=False,
    )
    runner = QueueRunner(
        result(
            json.dumps(
                {
                    "headRefOid": merged_head_sha,
                    "state": "MERGED",
                    "isDraft": False,
                    "mergeCommit": {"oid": merge_sha},
                }
            )
        )
    )

    landed = await GitHubPublisher(runner=runner).record_external_merge(receipt)

    assert landed.landed is True
    assert landed.head_sha == published_sha
    assert landed.merged_head_sha == merged_head_sha
    assert landed.merge_sha == merge_sha


async def test_checks_report_a_moved_merged_head_as_landed() -> None:
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha="b" * 40,
        draft=False,
    )
    runner = QueueRunner(
        result(
            json.dumps(
                {
                    "headRefOid": "c" * 40,
                    "state": "MERGED",
                    "isDraft": False,
                    "mergeCommit": {"oid": "d" * 40},
                }
            )
        )
    )

    with pytest.raises(PullRequestLanded) as landed:
        await GitHubPublisher(runner=runner).checks(receipt)

    assert landed.value.receipt.head_sha == "b" * 40
    assert landed.value.receipt.merged_head_sha == "c" * 40
    assert landed.value.receipt.merge_sha == "d" * 40


async def test_a_moved_head_is_still_refused_while_the_pull_request_is_open() -> None:
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha="b" * 40,
        draft=False,
    )
    runner = QueueRunner(
        result(json.dumps({"headRefOid": "c" * 40, "state": "OPEN", "isDraft": False}))
    )

    with pytest.raises(PublicationError, match="does not match the published revision"):
        await GitHubPublisher(runner=runner).checks(receipt)


def test_a_merged_moved_head_is_not_reported_as_an_integration_conflict() -> None:
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha="b" * 40,
        draft=False,
    )

    _raise_if_integration_conflict(
        {
            "headRefOid": "c" * 40,
            "state": "MERGED",
            "mergeable": "CONFLICTING",
            "mergeStateStatus": "DIRTY",
        },
        receipt,
    )


async def test_an_unknown_pull_request_state_is_named_rather_than_guessed() -> None:
    """Three states are enumerated; a fourth is a question, not a default verdict."""

    sha = "b" * 40
    receipt = PublicationReceipt(
        run_id="run-1234567890",
        lane="issue-123",
        remote_url="git@github.com:owner/repo.git",
        base_branch="main",
        branch="orkastrator/run-12345678/issue-123",
        pull_request_url="https://github.com/owner/repo/pull/7",
        head_sha=sha,
        draft=True,
    )
    runner = QueueRunner(
        result(json.dumps({"headRefOid": sha, "state": "LOCKED", "isDraft": False}))
    )

    with pytest.raises(PublicationError, match="unknown pull-request state LOCKED"):
        await GitHubPublisher(runner=runner).checks(receipt)
