"""Guard: no tracked file names the maintainer's private image project.

The pattern is assembled from pieces so this file does not match itself.
"""
import re
import subprocess
from pathlib import Path

import pytest

PATTERN = re.compile("cl" + r"aw[-_ ]?forge", re.I)


def _tracked_files():
    here = Path(__file__).resolve().parent
    try:
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=here,
                              capture_output=True, text=True, check=True).stdout.strip()
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                             capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return Path(root), [p for p in out.decode().split("\0") if p]


def test_no_tracked_file_names_the_private_project():
    root, files = _tracked_files()
    hits = []
    for rel in files:
        path = root / rel
        if PATTERN.search(rel):
            hits.append(f"{rel}: (path)")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if PATTERN.search(line):
                hits.append(f"{rel}:{n}")
    assert not hits, "private project name in tracked files:\n" + "\n".join(hits)
