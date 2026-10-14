import re
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from .checksum import sha256_file
from .config import ROLES
from .metadata import save_json
from .retry import RETRYABLE, RETRY_STATUSES, backoff


class UploadError(RuntimeError):
    pass


def upload_file(api, file, source, source_date, *, sleep=time.sleep):
    config = api.config
    original = Path(file).resolve()
    if not original.is_file():
        raise ValueError("File does not exist or is not a regular file")
    if source not in ROLES[config.role]:
        raise ValueError("Invalid source for CLIENT_ROLE")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date):
        raise ValueError("source_date must be YYYY-MM-DD")
    date.fromisoformat(source_date)
    if not 0 < original.stat().st_size <= config.max_bytes:
        raise ValueError("File must be nonempty and within the configured server size limit")
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
        if not total:
            raise ValueError("File became empty during preparation")
    except Exception:
        snapshot.unlink(missing_ok=True)
        raise
    checksum = sha256_file(snapshot)
    record = dict(client_id=config.client_id, source=source, source_date=source_date,
                  filename=original.name, checksum_sha256=checksum, size_bytes=total,
                  server_status=None, uploaded_at=None, retry_count=0, http_status=None,
                  local_status="pending", job_id=job)
    start = time.monotonic()

    def sanitized_record():
        # Redact even if a remote response unexpectedly echoes a credential.
        import json
        safe = json.dumps(record)
        if config.api_key:
            safe = safe.replace(config.api_key, "[REDACTED]")
        return json.loads(safe)

    def persist():
        save_json(pending / "metadata.json", sanitized_record())

    persist()
    try:
        for attempt in range(config.retries + 1):
            record["retry_count"] = attempt
            try:
                response = api.upload(snapshot, original.name, source, source_date)
            except RETRYABLE as exc:
                if attempt < config.retries:
                    backoff(attempt, sleep)
                    continue
                raise UploadError("Network failure after bounded retries: " + type(exc).__name__) from None
            record["http_status"] = response.status_code
            if response.status_code in RETRY_STATUSES and attempt < config.retries:
                backoff(attempt, sleep)
                continue
            if response.status_code != 200:
                raise UploadError(f"Server rejected upload: HTTP {response.status_code}")
            try:
                body = response.json()
                remote = body["metadata"]
                state = body["status"]
                if state not in {"VERIFIED", "DUPLICATE"} or remote["status"] != "VERIFIED":
                    raise ValueError()
                if remote["sha256"] != checksum or remote["size_bytes"] != total or not remote["file_id"]:
                    raise ValueError()
                # Server deduplicates globally by hash, including across source/date/client.
                # Do not silently claim that a duplicate registered a new observation.
                if remote["source"] != source or remote["source_date"] != source_date:
                    raise UploadError("Server duplicate belongs to a different source/date; kept pending for review")
            except (ValueError, KeyError, TypeError):
                raise UploadError("Server response does not confirm matching file integrity") from None
            record.update(server_status=state, server_file_id=remote["file_id"], server_metadata=remote,
                          uploaded_at=datetime.now(timezone.utc).isoformat(), local_status="uploaded")
            record["listing_check"] = "unavailable"
            try:
                listing = api.uploads()
                if listing.status_code == 200:
                    entries = listing.json()
                    record["listing_check"] = "matched" if isinstance(entries, list) and any(
                        isinstance(item, dict) and item.get("file_id") == remote["file_id"]
                        and item.get("sha256") == checksum and item.get("size_bytes") == total
                        for item in entries) else "not_found"
            except (httpx.HTTPError, ValueError):
                pass
            record["elapsed_seconds"] = round(time.monotonic() - start, 3)
            persist()
            safe_record = sanitized_record()
            save_json(config.root / "logs" / (job + ".json"), {k: v for k, v in safe_record.items() if k != "server_metadata"})
            destination = config.root / "data" / "uploaded" / job
            destination.parent.mkdir(parents=True, exist_ok=True)
            pending.rename(destination)
            return safe_record
    except Exception as exc:
        record.update(local_status="pending", last_error=str(exc) if isinstance(exc, UploadError) else type(exc).__name__)
        persist()
        # Failure report links the pending snapshot; payload stays retryable in pending.
        save_json(config.root / "data" / "failed" / (job + ".json"),
                  dict(job_id=job, local_status="failed", pending_file=str(snapshot), last_error=record["last_error"]))
        raise UploadError(record["last_error"]) from None
