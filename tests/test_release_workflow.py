"""The workflow files stay valid YAML where it is easy to break them by hand (no YAML parser is a dependency)."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_a_one_line_run_value_holds_no_mapping_indicator():
    """`run: echo "{\\"commit\\": ...}"` is a plain scalar with ": " inside: GitHub rejects the whole file and a tag
    push publishes nothing (v0.3.1). A command with ": " goes in a block (`run: |`)."""
    bad = []
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        for i, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
            m = re.match(r"^\s*(?:- )?run:\s+(?![|>])(.+)$", line)
            if m and not m.group(1).startswith(("'", '"')) and (": " in m.group(1) or " #" in m.group(1)):
                bad.append(f"{wf.name}:{i}: {line.strip()}")
    assert not bad, bad


def test_the_release_build_is_stamped_before_it_is_built():
    text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert text.index("verinoda/data/build_stamp.json") < text.index("uv build --out-dir dist")
    assert 'verinoda --version | grep -q "commit ${GITHUB_SHA:0:12}"' in text
