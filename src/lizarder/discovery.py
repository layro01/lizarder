"""Locate Git repositories beneath a root directory."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, Optional


def is_git_repo(path: Path) -> bool:
    # `.git` is a directory for normal clones and a file for worktrees/submodules.
    return (path / ".git").exists()


def find_git_repos(
    root: Path,
    nested: bool = False,
    max_depth: Optional[int] = None,
) -> Iterator[Path]:
    """Yield Git repository roots under ``root`` (including ``root`` itself).

    By default the search stops descending once a repository is found. With
    ``nested=True`` it keeps going, so submodules and repos vendored inside
    other repos are reported as separate projects. Symlinked directories are
    not followed, which avoids cycles.
    """
    root = root.resolve()
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if is_git_repo(current):
            yield current
            if not nested:
                continue
        if max_depth is not None and depth >= max_depth:
            continue
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name, reverse=True)
        except OSError:
            continue
        for entry in entries:
            if entry.name == ".git":
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), depth + 1))
            except OSError:
                continue
