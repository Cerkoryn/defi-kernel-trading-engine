"""Exercise the actual Compose startup command without Docker access or downloads."""

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def deployment():
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI required (no daemon needed)")
    path = Path(__file__).resolve().parents[1] / "deploy/dolos-preprod/compose.yaml"
    result = subprocess.run(
        ["docker", "compose", "-f", str(path), "config", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    config = json.loads(result.stdout)
    assert not config.get("configs")
    assert config["services"]["dolos"]["read_only"] is True
    return config["services"]["dolos"]


@pytest.mark.parametrize("state", ["empty", "existing", "failure"])
def test_daemon_start_and_restart_preserve_data(deployment, tmp_path, state):
    data = tmp_path / "data"
    data.mkdir()
    if state != "empty":
        (data / "store").write_text("existing database")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "dolos"
    executable.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$DOLOS_TEST_LOG"
test "$3" = daemon || exit 99
if [ "$DOLOS_TEST_STATE" = failure ]; then
  echo 'test storage failure' >&2
  exit 23
fi
""")
    executable.chmod(0o755)
    log = tmp_path / "calls"
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        DOLOS_TEST_LOG=str(log),
        DOLOS_TEST_STATE=state,
    )
    config_path = tmp_path / "dolos.toml"
    command = (
        deployment["command"][0]
        .replace("$$", "$")
        .replace("/data", str(data))
        .replace("/tmp/dolos.toml", str(config_path))
    )
    for _ in range(2):
        result = subprocess.run(
            [*deployment["entrypoint"], command],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == (23 if state == "failure" else 0)
        if state == "failure":
            assert "test storage failure" in result.stderr
    dolos = tomllib.loads(config_path.read_text())
    assert dolos["chain"]["magic"] == 1
    assert dolos["storage"] == {"path": str(data), "version": "v4"}
    assert dolos["genesis"]["conway_path"] == "/etc/genesis/preprod/conway.json"
    assert "max_history" not in dolos["sync"]
    assert log.read_text().splitlines() == [f"-c {config_path} daemon"] * 2
    if state != "empty":
        assert (data / "store").read_text() == "existing database"
    assert {x.name for x in data.iterdir()} == (
        set() if state == "empty" else {"store"}
    )


@pytest.mark.parametrize(
    "case", ["ready", "running", "mount", "docker_failure", "mount_failure", "symlink"]
)
def test_documented_cleanup_is_scoped_and_refuses_uncertain_state(tmp_path, case):
    root = tmp_path / "dolos-preprod"
    data = root / "data"
    data.mkdir(parents=True)
    (data / ".old-marker").touch()
    for name in ("state", ".bootstrap-in-progress", ".dolos-snapshot-tmp"):
        (root / name).mkdir()
        (root / name / "old-store").touch()
    unrelated = tmp_path / "wallet"
    unrelated.mkdir()
    (unrelated / "keep").write_text("unrelated data")
    mode = data.stat().st_mode
    if case == "symlink":
        (data / ".old-marker").unlink()
        data.rmdir()
        data.symlink_to(unrelated, target_is_directory=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "docker": """case "$DOLOS_TEST_CASE" in
  docker_failure) exit 1;;
  running) echo active-container;;
esac
""",
        "findmnt": """case "$DOLOS_TEST_CASE" in
  mount_failure) exit 1;;
  mount) echo "$DOLOS_TEST_ROOT/data/nested";;
  *) echo /;;
esac
""",
    }.items():
        command = bin_dir / name
        command.write_text("#!/bin/sh\n" + body)
        command.chmod(0o755)
    document = Path("docs/dolos-preprod.md").read_text()
    command = document.split("sudo bash <<'SH'\n", 1)[1].split("\nSH", 1)[0]
    command = command.replace("/mnt/Business/Crypto/dolos-preprod", str(root))
    result = subprocess.run(
        ["bash", "-c", command],
        env=dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            DOLOS_TEST_CASE=case,
            DOLOS_TEST_ROOT=str(root),
        ),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) == (case == "ready")
    assert (unrelated / "keep").read_text() == "unrelated data"
    if case == "ready":
        assert list(root.iterdir()) == [data]
        assert list(data.iterdir()) == []
        assert data.stat().st_mode == mode
    else:
        assert (root / "state" / "old-store").exists()
        if case != "symlink":
            assert (data / ".old-marker").exists()
