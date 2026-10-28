import json
import threading
import os
import subprocess
import sys
from pathlib import Path
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from client.api_client import APIClient
from client.config import Config
from client.metadata import save_json
from client.locking import job_lock, JobLocked
from client.uploader import enqueue_file, send_job, upload_file, UploadError
from client.worker import run_once


@pytest.fixture
def config(tmp_path):
    return Config("collector01", "all", "http://127.0.0.1:8000", "unit-only-secret", retries=0, root=tmp_path)


def queued(config, source="usdm"):
    original = config.root / (source + ".csv")
    original.write_bytes(b"small mock")
    job = enqueue_file(config, original, source, "2021-06-22", "station/mock")
    return job, original


def record(config, job):
    return json.loads((config.root / "data/pending" / job / "metadata.json").read_text())


def ready(config, job):
    data = record(config, job); data["next_attempt_at"] = 0
    save_json(config.root / "data/pending" / job / "metadata.json", data)


def transport(config, handler):
    def call(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/uploads":
            return httpx.Response(403)
        return handler(request)
    return APIClient(config, httpx.MockTransport(call))


def confirmation(data):
    return {"status": "VERIFIED", "metadata": dict(status="VERIFIED", file_id="unit-file",
        sha256=data["checksum_sha256"], size_bytes=data["size_bytes"], source=data["source"],
        source_date=data["source_date"], dataset_key=data["dataset_key"])}


def test_restart_reuses_job_and_original_filename(config):
    job, original = queued(config)
    api = transport(config, lambda _: (_ for _ in ()).throw(httpx.ConnectError(config.api_key)))
    with pytest.raises(UploadError):
        send_job(api, job)
    api.close()
    before = record(config, job)
    assert before["local_status"] == "RETRY_PENDING" and before["attempts"] == 1
    ready(config, job)
    def send(request):
        assert b'filename="usdm.csv"' in request.read()
        assert b"station/mock" in request.read()
        return httpx.Response(200, json=confirmation(before))
    api = transport(config, send)
    result = run_once(api); api.close()
    assert result["uploaded"] == [job] and original.exists()
    assert list((config.root / "data/uploaded").iterdir())[0].name == job
    assert not list((config.root / "data/pending").iterdir())
    status = json.loads((config.root / "data/source_status.json").read_text())
    assert status["sources"]["usdm"]["uploaded"] == 1


@pytest.mark.parametrize("code", [400, 401, 403, 413, 422])
def test_terminal_errors_not_retried(config, code):
    job, _ = queued(config); calls = []
    def send(request):
        calls.append(1); return httpx.Response(code)
    api = transport(config, send)
    run_once(api); run_once(api); api.close()
    assert len(calls) == 1 and record(config, job)["local_status"] == "NEEDS_ATTENTION"


def test_budget_survives_restart(config):
    config = replace(config, worker_max_attempts=2)
    job, _ = queued(config)
    for _ in range(3):
        ready(config, job)
        api = transport(config, lambda _: httpx.Response(503))
        run_once(api); api.close()
    assert record(config, job)["attempts"] == 2
    assert record(config, job)["local_status"] == "NEEDS_ATTENTION"


def test_two_workers_cannot_send_same_job(config):
    job, _ = queued(config); started = threading.Event(); finish = threading.Event(); calls = []
    body = confirmation(record(config, job))
    def send(request):
        calls.append(1); started.set(); assert finish.wait(5)
        return httpx.Response(200, json=body)
    one = transport(config, send); two = transport(config, send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_once, one)
        assert started.wait(5)
        second = run_once(two)
        finish.set()
        assert first.result()["uploaded"] == [job]
        assert second["skipped"] == [job]
    one.close(); two.close()
    assert len(calls) == 1


def test_corrupt_pending_retained(config):
    job, original = queued(config)
    snapshot = config.root / "data/pending" / job / "payload"
    snapshot.write_bytes(b"damaged")
    api = transport(config, lambda _: pytest.fail("Must not upload corrupt content"))
    run_once(api); api.close()
    assert snapshot.exists() and original.exists()
    assert record(config, job)["local_status"] == "NEEDS_ATTENTION"


def test_legacy_pending_upgrade(config):
    job, _ = queued(config)
    data = record(config, job)
    for name in ["attempts", "dataset_key", "next_attempt_at"]:
        data.pop(name)
    data["local_status"] = "pending"
    save_json(config.root / "data/pending" / job / "metadata.json", data)
    data["dataset_key"] = data["filename"]
    api = transport(config, lambda _: httpx.Response(200, json=confirmation(data)))
    assert run_once(api)["uploaded"] == [job]
    api.close()


def test_single_client_source_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("SERVER_API_KEY", "unit-only-collector")
    monkeypatch.setenv("CLIENT_ID", "collector01")
    monkeypatch.setenv("CLIENT_ROLE", "all")
    monkeypatch.setenv("SOURCE_NLDAS_ENABLED", "false")
    conf = Config.load(tmp_path / "absent")
    assert conf.client_id == "collector01" and conf.permits("noaa") and not conf.permits("nldas")


def test_recover_acknowledged_job_without_network(config):
    job, _ = queued(config); data = record(config, job); remote = confirmation(data)["metadata"]
    data.update(local_status="UPLOADED", server_status="VERIFIED", server_metadata=remote,
                server_file_id=remote["file_id"], attempts=config.worker_max_attempts)
    save_json(config.root / "data/pending" / job / "metadata.json", data)
    api = transport(config, lambda _: pytest.fail("Persisted acknowledgement needs no resend"))
    assert run_once(api)["uploaded"] == [job]
    api.close()


def test_process_crash_releases_job_lock(config):
    job, _ = queued(config)
    code = '''
import sys, time
from pathlib import Path
from client.locking import job_lock
with job_lock(Path(sys.argv[1]), sys.argv[2]):
    print("LOCKED", flush=True)
    time.sleep(30)
'''
    process = subprocess.Popen([sys.executable, "-c", code, str(config.root), job],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        # Bound readiness via a helper thread rather than blocking indefinitely on stdout.
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(process.stdout.readline).result(timeout=5).strip() == "LOCKED"
        with pytest.raises(JobLocked):
            with job_lock(config.root, job):
                pytest.fail("Second process acquired the same job")
    finally:
        process.kill(); process.communicate(timeout=5)
    with job_lock(config.root, job):
        assert (config.root / "data/pending" / job / "payload").exists()
