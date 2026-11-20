"""`make run` serves on PORT, so a second worktree's app can run beside the first.

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
