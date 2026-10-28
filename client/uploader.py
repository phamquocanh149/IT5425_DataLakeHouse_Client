import json
import os
import re
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from .checksum import sha256_file
from .config import ROLES
from .metadata import save_json
from .locking import job_lock, JobLocked
from .retry import RETRYABLE, RETRY_STATUSES, backoff


class UploadError(RuntimeError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def enqueue_file(config, file, source, source_date, dataset_key=None):
    original = Path(file).resolve()
    if not original.is_file():
        raise ValueError("File does not exist or is not a regular file")
    if not config.permits(source):
        raise ValueError("Invalid source for CLIENT_ROLE")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date):
        raise ValueError("source_date must be YYYY-MM-DD")
    date.fromisoformat(source_date)
    if not 0 < original.stat().st_size <= config.max_bytes:
        raise ValueError("File must be nonempty and within the configured server size limit")
    if dataset_key is not None and (not dataset_key.strip() or len(dataset_key) > 512 or any(ord(c) < 32 for c in dataset_key)):
        raise ValueError("Invalid dataset_key")
    # Snapshot in a unique job directory; never delete or overwrite the caller's raw file.
    job = uuid.uuid4().hex
    pending = config.root / "data" / "pending" / job
    pending.mkdir(parents=True)
    snapshot = pending / "payload"
    try:
        # Bounded copy also protects against a source growing during preparation.
        with original.open("rb") as src, snapshot.open("xb") as dst:
            total = 0
            while chunk := src.read(1024 * 1024):
                total += len(chunk)
                if total > config.max_bytes:
                    raise ValueError("File grew beyond server size limit")
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if not total:
            raise ValueError("File became empty during preparation")
    except Exception:
        snapshot.unlink(missing_ok=True)
        raise
    checksum = sha256_file(snapshot)
    record = dict(client_id=config.client_id, source=source, source_date=source_date,
                  filename=original.name, checksum_sha256=checksum, size_bytes=total,
                  server_status=None, uploaded_at=None, retry_count=0, http_status=None,
                  local_status="PENDING", job_id=job, dataset_key=dataset_key or original.name,
                  attempts=0, next_attempt_at=0, retryable=True)
    save_json(pending / "metadata.json", record)
    return job


def upload_file(api, file, source, source_date, *, sleep=time.sleep, dataset_key=None):
    job = enqueue_file(api.config, file, source, source_date, dataset_key)
    return send_job(api, job, sleep=sleep)


def send_job(api, job, *, sleep=time.sleep, immediate_retries=None, respect_schedule=False):
    config = api.config
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", job):
        raise ValueError("Invalid job_id")
    with job_lock(config.root, job):
        pending = config.root / "data" / "pending" / job
        if not pending.is_dir():
            raise UploadError("Pending job no longer exists")
        snapshot = pending / "payload"
        record = json.loads((pending / "metadata.json").read_text(encoding="utf-8"))
        record.setdefault("attempts", record.get("retry_count", 0))
        record.setdefault("dataset_key", record["filename"])
        record["job_id"] = job
        if respect_schedule and record.get("next_attempt_at", 0) > time.time():
            raise JobLocked("Job backoff has not elapsed")

        def sanitized():
            def clean(value):
                if isinstance(value, str):
                    return value.replace(config.api_key, "[REDACTED]") if config.api_key else value
                if isinstance(value, dict):
                    return {k: clean(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [clean(v) for v in value]
                return value
            return clean(record)

        def persist():
            save_json(pending / "metadata.json", sanitized())

        def fail(message, retryable=False):
            record.update(local_status="RETRY_PENDING" if retryable and record["attempts"] < config.worker_max_attempts else "NEEDS_ATTENTION",
                          retryable=retryable, last_error=message,
                          next_attempt_at=time.time() + min(2 ** min(record["attempts"], 10), 3600))
            persist()
            save_json(config.root / "data" / "failed" / (job + ".json"),
                {"job_id": job, "local_status": record["local_status"], "pending_file": str(snapshot), "last_error": message})
            raise UploadError(message, retryable)

        # Never automatically resume terminal errors, even after restarting the worker.
        if record.get("local_status") == "NEEDS_ATTENTION":
            raise UploadError("Job requires operator attention")
        if record.get("http_status") in {400, 401, 403, 404, 409, 413, 422}:
            fail("Previous request was rejected; operator attention required")
        try:
            if not config.permits(record["source"]):
                fail("Source is disabled or unauthorized locally")
            if (not 0 < snapshot.stat().st_size <= config.max_bytes
                    or snapshot.stat().st_size != record["size_bytes"]
                    or sha256_file(snapshot) != record["checksum_sha256"]):
                fail("Pending payload integrity check failed")
        except OSError:
            fail("Pending payload missing or unreadable")
        # A crash between acknowledgement persistence and the directory rename requires
        # only finishing the local move, not another request or a new attempt budget.
        if record.get("local_status") == "UPLOADED" and record.get("server_status") in {"VERIFIED", "DUPLICATE"}:
            remote = record.get("server_metadata", {})
            if (remote.get("sha256") == record["checksum_sha256"] and remote.get("size_bytes") == record["size_bytes"]
                    and remote.get("source") == record["source"] and remote.get("source_date") == record["source_date"]
                    and remote.get("status") == "VERIFIED" and remote.get("file_id") == record.get("server_file_id")
                    and remote.get("dataset_key", remote.get("original_filename", record["filename"])) == record["dataset_key"]):
                destination = config.root / "data" / "uploaded" / job
                destination.parent.mkdir(parents=True, exist_ok=True)
                pending.rename(destination)
                return sanitized()
            fail("Persisted acknowledgement is invalid")
        if record["attempts"] >= config.worker_max_attempts:
            fail("Automatic attempt budget exhausted")
        start = time.monotonic()
        retries = config.retries if immediate_retries is None else immediate_retries
        for attempt in range(retries + 1):
            if record["attempts"] >= config.worker_max_attempts:
                fail("Automatic attempt budget exhausted")
            record.update(local_status="UPLOADING", retry_count=attempt, attempts=record["attempts"] + 1,
                          next_attempt_at=time.time() + min(2 ** min(record["attempts"] + 1, 10), 3600))
            persist()  # Reserve attempt before network I/O, including after a hard crash.
            try:
                response = api.upload(snapshot, record["filename"], record["source"], record["source_date"], record["dataset_key"])
            except RETRYABLE as exc:
                if attempt < retries and record["attempts"] < config.worker_max_attempts:
                    backoff(attempt, sleep); continue
                fail("Network failure after bounded retries: " + type(exc).__name__, True)
            except httpx.HTTPError:
                fail("Non-retryable HTTP transport error")
            record["http_status"] = response.status_code
            if response.status_code in RETRY_STATUSES:
                if attempt < retries and record["attempts"] < config.worker_max_attempts:
                    backoff(attempt, sleep); continue
                fail(f"Temporary server error: HTTP {response.status_code}", True)
            if response.status_code != 200:
                fail(f"Server rejected upload: HTTP {response.status_code}")
            try:
                body = response.json(); remote = body["metadata"]; state = body["status"]
                if state not in {"VERIFIED", "DUPLICATE"} or remote["status"] != "VERIFIED":
                    raise ValueError()
                if remote["sha256"] != record["checksum_sha256"] or remote["size_bytes"] != record["size_bytes"] or not remote["file_id"]:
                    raise ValueError()
                if remote["source"] != record["source"] or remote["source_date"] != record["source_date"]:
                    raise ValueError()
                # Old responses may lack dataset_key. Explicit non-filename keys require Stage 1.5.
                if remote.get("dataset_key", remote.get("original_filename", record["filename"])) != record["dataset_key"]:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                fail("Server response does not confirm matching logical observation and integrity")
            record.update(server_status=state, server_file_id=remote["file_id"], server_metadata=remote,
                          uploaded_at=datetime.now(timezone.utc).isoformat(), local_status="UPLOADED",
                          retryable=False, last_error=None, listing_check="unavailable")
            try:
                listing = api.uploads()
                if listing.status_code == 200:
                    entries = listing.json()
                    record["listing_check"] = "matched" if isinstance(entries, list) and any(
                        isinstance(item, dict) and item.get("file_id") == remote["file_id"]
                        and item.get("sha256") == record["checksum_sha256"] and item.get("size_bytes") == record["size_bytes"]
                        for item in entries) else "not_found"
            except (httpx.HTTPError, ValueError):
                pass
            record["elapsed_seconds"] = round(time.monotonic() - start, 3)
            persist()
            safe = sanitized()
            save_json(config.root / "logs" / (job + ".json"), {k: v for k, v in safe.items() if k != "server_metadata"})
            destination = config.root / "data" / "uploaded" / job
            destination.parent.mkdir(parents=True, exist_ok=True)
            pending.rename(destination)
            return safe
