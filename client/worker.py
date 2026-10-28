import json
import time

import httpx

from .locking import JobLocked
from .uploader import UploadError, send_job
from .metadata import save_json


def run_once(api, stop=None):
    result = {"uploaded": [], "skipped": [], "errors": [], "sources": {s: {"uploaded": 0, "pending": 0} for s in api.config.source_enabled}}
    try:
        health = api.health()
        if health.status_code != 200 or health.json().get("status") != "ok":
            return {**result, "server": "unavailable"}
    except (httpx.HTTPError, ValueError, AttributeError):
        return {**result, "server": "unavailable"}
    for directory in sorted((api.config.root / "data" / "pending").glob("*")):
        if stop and stop.is_set():
            break
        if not directory.is_dir():
            continue
        try:
            record = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
            source = record["source"]
            if source in result["sources"]:
                result["sources"][source]["pending"] += 1
            if record.get("local_status") == "NEEDS_ATTENTION" or record.get("next_attempt_at", 0) > time.time():
                result["skipped"].append(directory.name); continue
            send_job(api, directory.name, immediate_retries=0, respect_schedule=True)
            result["uploaded"].append(directory.name)
            if source in result["sources"]:
                result["sources"][source]["uploaded"] += 1
                result["sources"][source]["pending"] -= 1
        except JobLocked:
            result["skipped"].append(directory.name)
        except (UploadError, OSError, ValueError, KeyError, TypeError) as exc:
            result["errors"].append({"job_id": directory.name, "error": type(exc).__name__})
    save_json(api.config.root / "data" / "source_status.json", {"checked_at": time.time(), "sources": result["sources"]})
    return result
