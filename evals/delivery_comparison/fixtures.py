"""Frozen fixture discovery, reset, and filesystem snapshots."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import HiddenTaskTruth, TaskManifest


@dataclass(frozen=True)
class FrozenTask:
    root: Path
    manifest: TaskManifest
    hidden: HiddenTaskTruth

    @property
    def repo_template(self) -> Path:
        return self.root / "repo"


@dataclass(frozen=True)
class PreparedFixture:
    task: FrozenTask
    trial_root: Path
    repo: Path
    public_manifest: Path
    baseline: dict[str, str]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_tree(root: Path) -> dict[str, str]:
    """Return a stable path-to-content hash map, excluding VCS/runtime metadata."""

    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in {".git", "__pycache__", ".pytest_cache"} for part in relative.parts):
            continue
        if path.is_symlink():
            files[relative.as_posix()] = "symlink:" + str(path.readlink())
        elif path.is_file():
            files[relative.as_posix()] = sha256_file(path)
    return files


def load_task(path: Path) -> FrozenTask:
    manifest = TaskManifest.model_validate_json((path / "manifest.json").read_text())
    hidden = HiddenTaskTruth.model_validate_json((path / "hidden_truth" / "truth.json").read_text())
    return FrozenTask(root=path.resolve(), manifest=manifest, hidden=hidden)


def discover_tasks(fixtures_root: Path) -> list[FrozenTask]:
    return [load_task(path) for path in sorted(fixtures_root.iterdir()) if path.is_dir()]


def prepare_fixture(task: FrozenTask, destination: Path) -> PreparedFixture:
    """Create a fresh isolated repository and a prompt-safe public manifest copy."""

    if destination.exists():
        shutil.rmtree(destination)
    repo = destination / "repo"
    shutil.copytree(task.repo_template, repo, symlinks=True)
    public_manifest = destination / "task-manifest.json"
    public_manifest.write_text(
        json.dumps(task.manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    baseline = snapshot_tree(repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "eval@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Delivery Eval"], cwd=repo, check=True)
    subprocess.run(["git", "add", "--all"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "frozen fixture"], cwd=repo, check=True)
    return PreparedFixture(task, destination, repo, public_manifest, baseline)


def reset_fixture(prepared: PreparedFixture) -> PreparedFixture:
    """Reset by replacement, preventing untracked or ignored state from leaking trials."""

    return prepare_fixture(prepared.task, prepared.trial_root)
