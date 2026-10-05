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
    g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True, capture_output=True)
    g("init", "-q", "-b", "main"); g("add", "-A"); g("commit", "-qm", "one")
    yield repo
    subprocess.run(["bash", str(repo / "scripts/alphawave_stop.sh")], env={**os.environ, "ALPHAWAVE_REPO": str(repo)})


def _run(repo, script, port):
    env = {**os.environ, "ALPHAWAVE_REPO": str(repo), "ALPHAWAVE_PORT": str(port), "ALPHAWAVE_NO_PULL": "1"}
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
    assert "already running" in r2.stdout.lower()  # same code: no restart
    assert {n: (fake_repo / "data/run" / f"{n}.pid").read_text() for n in pids} == pids

    assert "latest version" in r2.stdout.lower()
    r3 = _run(fake_repo, "alphawave_stop.sh", port)
    assert r3.returncode == 0 and "Stopped" in r3.stdout
    time.sleep(0.5)
    for pid in pids.values():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid), 0)


def test_new_code_restarts_both_processes(fake_repo):
    port = _free_port()
    assert _run(fake_repo, "alphawave_launcher.sh", port).returncode == 0
    old = {n: (fake_repo / "data/run" / f"{n}.pid").read_text() for n in ("scheduler", "dashboard")}
    (fake_repo / "new_feature.txt").write_text("x")  # simulate a merge landing
    subprocess.run(["git", "-C", str(fake_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(fake_repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "two"], check=True)
    r = _run(fake_repo, "alphawave_launcher.sh", port)
    assert r.returncode == 0 and "restarted" in r.stdout.lower(), r.stdout + r.stderr
    new = {n: (fake_repo / "data/run" / f"{n}.pid").read_text() for n in old}
    assert all(new[n] != old[n] for n in old)
    for pid in new.values():
        os.kill(int(pid), 0)
    for pid in old.values():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid), 0)


def test_running_app_actions_open_restart_stop(fake_repo):
    port = _free_port()
    assert _run(fake_repo, "alphawave_launcher.sh", port).returncode == 0
    pidf = lambda n: (fake_repo / "data/run" / f"{n}.pid").read_text()
    first = {n: pidf(n) for n in ("scheduler", "dashboard")}

    def act(a):
        env = {**os.environ, "ALPHAWAVE_REPO": str(fake_repo), "ALPHAWAVE_PORT": str(port), "ALPHAWAVE_NO_PULL": "1", "ALPHAWAVE_ACTION": a}
        return subprocess.run(["bash", str(fake_repo / "scripts/alphawave_launcher.sh")], env=env, capture_output=True, text=True, timeout=60)

    assert act("open").returncode == 0 and {n: pidf(n) for n in first} == first
    r = act("restart")
    assert r.returncode == 0 and "restarted" in r.stdout.lower() and pidf("scheduler") != first["scheduler"]
    last = {n: pidf(n) for n in first}
    r = act("stop")
    assert r.returncode == 0 and "Stopped" in r.stdout
    assert not (fake_repo / "data/run/scheduler.pid").exists()
    time.sleep(0.5)
    for pid in last.values():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid), 0)


def test_app_mode_defers_dialog_to_the_app(fake_repo):
    port = _free_port()
    assert _run(fake_repo, "alphawave_launcher.sh", port).returncode == 0
    env = {**os.environ, "ALPHAWAVE_REPO": str(fake_repo), "ALPHAWAVE_PORT": str(port), "ALPHAWAVE_NO_PULL": "1", "ALPHAWAVE_APPLET": "1"}
    r = subprocess.run(["bash", str(fake_repo / "scripts/alphawave_launcher.sh")], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1] == "ALPHAWAVE_ASK"
    r = subprocess.run(["bash", str(fake_repo / "scripts/alphawave_launcher.sh")], env={**env, "ALPHAWAVE_ACTION": "stop"}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1].startswith("Stopped")


def test_launcher_reports_missing_python_env(fake_repo):
    (fake_repo / "venv/bin/python").write_text("#!/bin/bash\nexit 1\n")
    r = _run(fake_repo, "alphawave_launcher.sh", _free_port())
    assert r.returncode == 1 and "not ready" in r.stdout


def test_installer_builds_both_apps(fake_repo, tmp_path):
    dest = tmp_path / "Desktop"
    r = subprocess.run(["bash", str(fake_repo / "scripts/install_desktop_app.sh"), str(dest)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    (dest / "Stop AlphaWave.app").mkdir(parents=True)  # leftover from the first version
    r = subprocess.run(["bash", str(fake_repo / "scripts/install_desktop_app.sh"), str(dest)], capture_output=True, text=True)
    assert not (dest / "Stop AlphaWave.app").exists()
    for app, script in (("AlphaWave", "alphawave_launcher.sh"),):
        run = dest / f"{app}.app/Contents/MacOS/run"
        assert os.access(run, os.X_OK) and script in run.read_text()
        assert (dest / f"{app}.app/Contents/Resources/repo_path").read_text() == str(fake_repo)
        assert (dest / f"{app}.app/Contents/Info.plist").exists()
