"""deploy/home/backup.sh: the one copy of the log that is not on the PC itself.

Run as a real subprocess against temporary directories. `BACKUP_MOUNT=/`
stands in for the external drive, because `/` is a mountpoint everywhere this
suite runs and the script's mount check is exactly what most needs exercising.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import time

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "home" / "backup.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("mountpoint") is None, reason="needs util-linux's mountpoint"
)


@pytest.fixture
def dirs(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "gymlog.json").write_text('{"sessions": []}')
    (data / "gymlog.chat.json").write_text('{"messages": []}')
    return data, tmp_path / "backup"


def run(data, backup, mount="/", **env):
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": "/usr/bin:/bin",
            "DATA_DIR": str(data),
            "BACKUP_MOUNT": mount,
            "BACKUP_DIR": str(backup),
            **env,
        },
        capture_output=True,
        text=True,
    )


def snapshots(backup):
    return sorted(p for p in backup.iterdir() if p.name.startswith("20"))


def test_first_run_snapshots_every_file(dirs):
    data, backup = dirs
    result = run(data, backup)

    assert result.returncode == 0, result.stderr
    [snapshot] = snapshots(backup)
    assert (snapshot / "gymlog.json").read_text() == '{"sessions": []}'
    assert (snapshot / "gymlog.chat.json").exists()
    assert (snapshot / "SHA256SUMS").exists()


def test_an_unchanged_log_writes_no_new_snapshot(dirs):
    data, backup = dirs
    run(data, backup)
    time.sleep(1.1)  # snapshot names have one-second resolution

    result = run(data, backup)

    assert result.returncode == 0, result.stderr
    assert "unchanged" in result.stdout
    assert len(snapshots(backup)) == 1


def test_a_changed_log_writes_a_new_snapshot(dirs):
    data, backup = dirs
    run(data, backup)
    time.sleep(1.1)
    (data / "gymlog.json").write_text('{"sessions": [1]}')

    run(data, backup)

    newest = snapshots(backup)[-1]
    assert len(snapshots(backup)) == 2
    assert (newest / "gymlog.json").read_text() == '{"sessions": [1]}'


def test_refuses_when_the_drive_is_not_mounted(dirs, tmp_path):
    """An unplugged drive leaves an empty directory; writing into it protects nothing."""
    data, backup = dirs
    unmounted = tmp_path / "mnt"
    unmounted.mkdir()

    result = run(data, backup, mount=str(unmounted))

    assert result.returncode != 0
    assert "not mounted" in result.stderr
    assert not backup.exists()


def test_refuses_to_snapshot_a_corrupt_log(dirs):
    data, backup = dirs
    (data / "gymlog.json").write_text('{"sessions": [')

    result = run(data, backup)

    assert result.returncode != 0
    assert "not valid JSON" in result.stderr
    assert list(backup.iterdir()) == []  # no snapshot, and no leftover .incoming


def test_prunes_old_snapshots_but_always_keeps_the_newest(dirs):
    data, backup = dirs
    backup.mkdir()
    for year in range(2010, 2022):  # twelve snapshots, all far past KEEP_DAYS
        (backup / f"{year}-01-01T000000Z").mkdir()

    result = run(data, backup, KEEP_MIN="10")

    assert result.returncode == 0, result.stderr
    kept = [p.name for p in snapshots(backup)]
    assert len(kept) == 10
    assert "2010-01-01T000000Z" not in kept
    assert "2021-01-01T000000Z" in kept
