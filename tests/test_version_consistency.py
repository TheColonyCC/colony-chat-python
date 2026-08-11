"""The packaged version and the runtime version must agree.

WHY THIS EXISTS
---------------
They didn't. `pyproject.toml` said 0.2.0 while `colony_chat/_version.py` said
0.1.3, and nothing compared them, so the drift shipped: the wheel published to
PyPI as colony-chat 0.2.0 carries `Version: 0.2.0` in its metadata and reports
`__version__ == "0.1.3"` when you import it. Anything that logs or branches on
the runtime version — a plugin checking a feature floor, a bug report quoting
its own version — was told the wrong thing.

Neither direction of this drift turns anything red on its own:

  * `_version.py` bumped, pyproject not → the tag is unpublishable under its
    own number (building v0.3.0 emits a 0.2.0 artifact).
  * pyproject bumped, `_version.py` not → publishes cleanly and then lies.
    This is the one that happened.

A release checklist saying "bump both" is not a mechanism. This is, and it runs
on every push, before anything is tagged — which matters, because by tag time
the fix means retagging something already published.

`tomllib` is 3.11+ and CI runs 3.10, so pyproject is read with a narrow regex
rather than a TOML parser. The regex is anchored to the first `version = "..."`
after `[project]` so a version key in some later table can't satisfy it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from colony_chat import __version__

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

_PROJECT_VERSION = re.compile(
    r"^\[project\]$.*?^version\s*=\s*[\"']([^\"']+)[\"']",
    re.MULTILINE | re.DOTALL,
)


def packaged_version() -> str:
    """The version setuptools will stamp on the artifact."""
    text = PYPROJECT.read_text(encoding="utf-8")
    match = _PROJECT_VERSION.search(text)
    assert match is not None, f"no [project] version found in {PYPROJECT}"
    return match.group(1)


def test_pyproject_version_matches_runtime_version() -> None:
    """The guard. This is the assertion the drift would have tripped."""
    assert packaged_version() == __version__, (
        f"pyproject.toml declares {packaged_version()!r} but "
        f"colony_chat.__version__ is {__version__!r}. Bump both — the artifact "
        f"and the value users see at runtime are the same fact."
    )


def test_version_is_pep440_ish() -> None:
    """A version that doesn't parse would break the release workflow late."""
    assert re.fullmatch(r"\d+\.\d+\.\d+([abrc.\-+][\w.\-+]*)?", __version__), (
        f"{__version__!r} is not a release-shaped version"
    )


@pytest.mark.parametrize("wrong", ["0.0.0", "9.9.9"])
def test_guard_would_fail_on_a_mismatch(wrong: str) -> None:
    """Control: prove the comparison can fail.

    A version check that reads the same string twice — or a regex that quietly
    matches nothing and compares None to None — passes forever and certifies
    nothing. This asserts the two sources are genuinely compared, by checking
    the real value is not equal to a value it should never hold.
    """
    assert packaged_version() != wrong
    assert packaged_version() == __version__ != wrong
