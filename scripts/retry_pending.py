import argparse
import json
import signal
import sys
import threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client.config import Config
from client.api_client import APIClient
from client.worker import run_once


def main():
    parser = argparse.ArgumentParser(description="Resume pending snapshots without making new jobs")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=10)
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("interval must be >= 1 second")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    api = None
    try:
        api = APIClient(Config.load())
        while not stop.is_set():
            report = run_once(api, stop)
            print(json.dumps(report), flush=True)
            if args.once:
                return 1 if report.get("server") == "unavailable" or report["errors"] else 0
            stop.wait(args.interval)
        return 0
    except (ValueError, OSError) as exc:
        print("Worker configuration/storage error:", type(exc).__name__)
        return 1
    finally:
        if api:
            api.close()


if __name__ == "__main__":
    sys.exit(main())
