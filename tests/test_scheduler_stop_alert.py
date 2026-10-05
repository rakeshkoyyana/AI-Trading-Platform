"""Discord must hear about a scheduler that stops: Ctrl+C, kill/Stop button (SIGTERM), or a crash."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from src.scheduler.run_loop import run_until_stopped

ROOT = Path(__file__).resolve().parents[1]


class _Cycle:
    engine = None

    def __init__(self):
        self.msgs = []

    def notify(self, msg, level="info"):
        self.msgs.append((level, msg))


class _Sched:
    def __init__(self, exc):
        self.exc = exc

    def start(self):
        raise self.exc


def test_ctrl_c_and_systemexit_send_stopped():
    for exc, word in ((KeyboardInterrupt(), "Ctrl+C"), (SystemExit(0), "stopped")):
        c = _Cycle()
        run_until_stopped(_Sched(exc), c)
        assert c.msgs and "Scheduler stopped" in c.msgs[-1][1] and word in c.msgs[-1][1]


def test_crash_is_reported_then_raised():
    c = _Cycle()
    try:
        run_until_stopped(_Sched(RuntimeError("boom")), c)
        raise AssertionError("should re-raise")
    except RuntimeError:
        pass
    assert c.msgs[0][0] == "error" and "crashed" in c.msgs[0][1]


def test_sigterm_posts_stopped_to_discord(tmp_path):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))["content"])
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path}/t.db", "BROKER": "sim", "PYTHONPATH": str(ROOT),
           "DISCORD_WEBHOOK_URL": f"http://127.0.0.1:{srv.server_port}/hook"}
    p = subprocess.Popen([sys.executable, "-m", "src.scheduler.run_loop", "--sim"], cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if any("Scheduler started" in m for m in got):
                break
            time.sleep(0.2)
        assert any("Scheduler started" in m for m in got), got
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=20)
    finally:
        if p.poll() is None:
            p.kill()
        srv.shutdown()
    assert any("Scheduler stopped (SIGTERM)" in m for m in got), got
