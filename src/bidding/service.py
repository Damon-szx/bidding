"""Local single-process API lifecycle commands."""
import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from bidding.config import DATA, ROOT

PID_FILE = DATA / "api.pid"
LOG_FILE = DATA / "api.log"


def owned_pid():
    if not PID_FILE.exists():
        return None
    pid = int(PID_FILE.read_text().strip())
    command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout.strip()
    if str(ROOT / ".venv" / "bin" / "python") in command and "-m uvicorn bidding.api:app" in command:
        return pid
    PID_FILE.unlink(missing_ok=True)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    pid = owned_pid()
    if args.action == "status":
        print("Running, PID " + str(pid) if pid else "Stopped")
        return
    if args.action == "stop":
        if not pid:
            print("Already stopped")
            return
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if not owned_pid():
                print("Stopped")
                return
            time.sleep(0.2)
        print("Shutdown is still in progress; check status and api.log")
        return
    if pid:
        print("Already running, PID " + str(pid))
        return
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    try:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", args.port))
    except OSError as exc:
        raise SystemExit("Port unavailable: " + str(exc)) from exc
    with LOG_FILE.open("ab") as log:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "bidding.api:app", "--host", "127.0.0.1", "--port", str(args.port)], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    PID_FILE.write_text(str(process.pid))
    # Startup failures, including an occupied port, must not be reported as success.
    for _ in range(50):
        time.sleep(0.2)
        if process.poll() is not None:
            PID_FILE.unlink(missing_ok=True)
            raise SystemExit("Startup failed; see " + str(LOG_FILE))
        import urllib.request
        try:
            with urllib.request.urlopen("http://127.0.0.1:" + str(args.port) + "/health", timeout=0.5) as response:
                if response.status == 200:
                    print("Started, PID " + str(process.pid) + "; http://127.0.0.1:" + str(args.port) + "/docs")
                    return
        except OSError:
            pass
    print("Process started but health check not ready; see " + str(LOG_FILE))


if __name__ == "__main__":
    main()
