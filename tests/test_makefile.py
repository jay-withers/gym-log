"""The Makefile's recipes: `make run` serves on PORT, so a second worktree's app
can run beside the first, and a release tag deploys the image it was published
as.

Checked with `make -n`, which prints the recipe without running it.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="needs make")


def _run_recipe(*args: str, port_env: str | None = None) -> str:
    # Drop what a parent `make test` passes down, so only these args count.
    env = {
        k: v for k, v in os.environ.items() if k not in {"PORT", "MAKEFLAGS", "MFLAGS", "MAKELEVEL"}
    }
    if port_env is not None:
        env["PORT"] = port_env
    result = subprocess.run(
        ["make", "-n", "run", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_run_serves_on_8000_by_default() -> None:
    assert '--port "8000"' in _run_recipe()


def test_run_serves_on_the_port_given() -> None:
    assert '--port "8123"' in _run_recipe("PORT=8123")


def test_run_serves_on_a_port_from_the_environment() -> None:
    assert '--port "8124"' in _run_recipe(port_env="8124")


def test_an_empty_port_falls_back_to_8000() -> None:
    assert '--port "8000"' in _run_recipe("PORT=")
    assert '--port "8000"' in _run_recipe(port_env="")


# --- the image a tag deploys -------------------------------------------------
#
# Releases up to v0.51.2 were published under the repo's old name. A rollback
# to one of them (cd-publish rolls back to `make deployed-tag`'s answer) must
# deploy and smoke-test the image that exists, not one under the new name.

OLD_IMAGE = "ghcr.io/jay-withers/gym-log/gymlog"
NEW_IMAGE = "ghcr.io/jay-withers/health/health"


def _make_n(target: str, tag: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL"}}
    result = subprocess.run(
        ["make", "-n", target, f"IMAGE_TAG={tag}"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.mark.parametrize("tag", ["v0.51.2", "v0.51.1", "v0.9.0"])
def test_a_release_from_before_the_rename_deploys_its_old_image(tag: str) -> None:
    deploy = _make_n("deploy", tag)
    assert deploy.count(f"--image {OLD_IMAGE}:{tag}") == 4
    assert NEW_IMAGE not in deploy
    assert f"{OLD_IMAGE} " in _make_n("smoke", tag)


@pytest.mark.parametrize("tag", ["v0.51.3", "v0.51.10", "v0.52.0", "v1.0.0", "abc1234"])
def test_a_later_release_or_a_commit_deploys_the_new_image(tag: str) -> None:
    deploy = _make_n("deploy", tag)
    assert deploy.count(f"--image {NEW_IMAGE}:{tag}") == 4
    assert OLD_IMAGE not in deploy
    assert f"{NEW_IMAGE} " in _make_n("smoke", tag)


def test_deployed_tag_reads_either_image() -> None:
    recipe = _make_n("deployed-tag", "v0.52.0")
    assert f"s|^{NEW_IMAGE}:||p" in recipe
    assert f"s|^{OLD_IMAGE}:||p" in recipe
