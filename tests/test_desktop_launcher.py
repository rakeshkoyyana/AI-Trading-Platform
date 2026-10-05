"""The desktop launcher scripts: start once, don't double-start, stop cleanly (fake python, no broker)."""
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ["alphawave_launcher.sh", "alphawave_stop.sh", "install_desktop_app.sh"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for n in SCRIPTS:
        shutil.copy(ROOT / "scripts" / n, repo / "scripts" / n)
    py = repo / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "-c" ]; then exit 0; fi\n'
        'if [ "$2" = "src.scheduler.run_loop" ]; then exec sleep 300; fi\n'
        # streamlit run ... --server.port N  -> a tiny server on that port
        'while [ $# -gt 0 ]; do [ "$1" = "--server.port" ] && P=$2; shift; done\n'
        "exec python3 -m http.server $P --bind 127.0.0.1\n"
    )
    py.chmod(0o755)
    yield repo
    subprocess.run(["bash", str(repo / "scripts/alphawave_stop.sh")], env={**os.environ, "ALPHAWAVE_REPO": str(repo)})


def _run(repo, script, port):
    env = {**os.environ, "ALPHAWAVE_REPO": str(repo), "ALPHAWAVE_PORT": str(port)}
    return subprocess.run(["bash", str(repo / "scripts" / script)], env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_have_valid_syntax(name):
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)]).returncode == 0


def test_start_is_idempotent_and_stop_cleans_up(fake_repo):
    port = _free_port()
    r1 = _run(fake_repo, "alphawave_launcher.sh", port)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    pids = {n: (fake_repo / "data/run" / f"{n}.pid").read_text() for n in ("scheduler", "dashboard")}
    for pid in pids.values():
        os.kill(int(pid), 0)

    r2 = _run(fake_repo, "alphawave_launcher.sh", port)  # second double-click
    assert r2.returncode == 0
    assert "already running" in r2.stdout.lower()
    assert {n: (fake_repo / "data/run" / f"{n}.pid").read_text() for n in pids} == pids

    r3 = _run(fake_repo, "alphawave_stop.sh", port)
    assert r3.returncode == 0 and "Stopped" in r3.stdout
    time.sleep(0.5)
    for pid in pids.values():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid), 0)


def test_launcher_reports_missing_python_env(fake_repo):
    (fake_repo / "venv/bin/python").write_text("#!/bin/bash\nexit 1\n")
    r = _run(fake_repo, "alphawave_launcher.sh", _free_port())
    assert r.returncode == 1 and "not ready" in r.stdout


def test_installer_builds_both_apps(fake_repo, tmp_path):
    dest = tmp_path / "Desktop"
    r = subprocess.run(["bash", str(fake_repo / "scripts/install_desktop_app.sh"), str(dest)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    for app, script in (("AlphaWave", "alphawave_launcher.sh"), ("Stop AlphaWave", "alphawave_stop.sh")):
        run = dest / f"{app}.app/Contents/MacOS/run"
        assert os.access(run, os.X_OK) and script in run.read_text()
        assert (dest / f"{app}.app/Contents/Resources/repo_path").read_text() == str(fake_repo)
        assert (dest / f"{app}.app/Contents/Info.plist").exists()
