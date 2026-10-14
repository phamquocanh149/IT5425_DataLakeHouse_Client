import hashlib
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from client.api_client import APIClient
from client.checksum import sha256_file
from client.config import Config
from client.uploader import UploadError, upload_file
from client.retry import backoff


@pytest.fixture
def cfg(tmp_path):
    return Config("client01", "all", "https://server.test", "test-secret-key", retries=2, root=tmp_path)


@pytest.fixture
def payload(tmp_path):
    path = tmp_path / "sample.csv"
    path.write_bytes(b"time,lat,lon,drought\n2021-06-22,39.25,-95.25,D1\n")
    return path


def receipt(payload, status="VERIFIED", **changes):
    metadata = dict(file_id="file_1", sha256=sha256_file(payload), size_bytes=payload.stat().st_size,
                    source="usdm", source_date="2021-06-22", status="VERIFIED", client_id="client01")
    metadata.update(changes)
    return {"status": status, "metadata": metadata}


def run(cfg, payload, handler, **kwargs):
    api = APIClient(cfg, httpx.MockTransport(handler))
    try:
        return upload_file(api, payload, kwargs.pop("source", "usdm"), kwargs.pop("source_date", "2021-06-22"), sleep=lambda _: None)
    finally:
        api.close()


def test_config_env(tmp_path, monkeypatch):
    for name in ["CLIENT_ID", "CLIENT_ROLE", "SERVER_BASE_URL", "SERVER_API_KEY", "REQUEST_TIMEOUT", "MAX_FILE_SIZE_MB", "UPLOAD_RETRY_COUNT"]:
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text("CLIENT_ID=client02\nCLIENT_ROLE=nldas_usgs\nSERVER_BASE_URL=https://server.test\nSERVER_API_KEY=test-secret\n")
    config = Config.load(env)
    assert config.client_id == "client02" and config.role == "nldas_usgs"
    assert config.api_key == "test-secret" and "test-secret" not in repr(config)
    monkeypatch.setenv("SERVER_API_KEY", "")
    with pytest.raises(ValueError, match="API_KEY"):
        Config.load(env)


@pytest.mark.parametrize("url", ["", "ftp://server", "https://", "https://user:secret@server", "https://server:wrong", "https://bad host", "https://server?secret=1"])
def test_bad_url(tmp_path, monkeypatch, url):
    monkeypatch.setenv("SERVER_BASE_URL", url)
    with pytest.raises(ValueError, match="SERVER_BASE_URL"):
        Config.load(tmp_path / "missing")


def test_checksum(payload):
    assert sha256_file(payload) == hashlib.sha256(payload.read_bytes()).hexdigest()


@pytest.mark.parametrize("kind", ["missing", "oversize", "empty", "source", "date", "role"])
def test_rejected_before_network(cfg, payload, kind):
    kwargs = {}
    if kind == "missing":
        payload = payload.with_name("absent")
    if kind == "oversize":
        with payload.open("wb") as stream:
            stream.truncate(20 * 1024 * 1024 + 1)
    if kind == "empty":
        payload.write_bytes(b"")
    if kind == "source":
        kwargs["source"] = "invalid"
    if kind == "date":
        kwargs["source_date"] = "2021-02-30"
    if kind == "role":
        cfg = replace(cfg, role="nldas_usgs")
    def unexpected(request):
        pytest.fail("Validation must happen before network access")
    with pytest.raises(ValueError):
        run(cfg, payload, unexpected, **kwargs)


@pytest.mark.parametrize("status", ["VERIFIED", "DUPLICATE"])
def test_success_and_wire_contract(cfg, payload, status):
    calls = []
    body = receipt(payload, status)
    def handler(request):
        calls.append(request.url.path)
        assert request.headers["X-API-Key"] == cfg.api_key
        if request.url.path == "/uploads":
            return httpx.Response(200, json=[body["metadata"]])
        assert request.method == "POST" and request.url.path == "/upload"
        assert request.headers["content-type"].startswith("multipart/form-data;")
        data = request.read()
        for field in [b'name="file"', b'name="source"', b'name="source_date"', b"usdm", b"2021-06-22", payload.read_bytes()]:
            assert field in data
        assert b'name="client_id"' not in data
        return httpx.Response(200, json=body)
    result = run(cfg, payload, handler)
    assert result["server_status"] == status and result["listing_check"] == "matched"
    assert payload.exists() and calls == ["/upload", "/uploads"]
    assert (cfg.root / "data/uploaded" / result["job_id"] / "payload").read_bytes() == payload.read_bytes()


@pytest.mark.parametrize("code", [400, 401, 403, 409, 413, 422])
def test_http_rejection_no_retry(cfg, payload, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(code, json={"detail": cfg.api_key})
    with pytest.raises(UploadError, match=f"HTTP {code}"):
        run(cfg, payload, handler)
    assert len(calls) == 1
    assert list((cfg.root / "data/pending").glob("*/payload"))
    assert not list((cfg.root / "data/uploaded").glob("*"))
    assert list((cfg.root / "data/failed").glob("*.json"))
    for path in cfg.root.rglob("*.json"):
        assert cfg.api_key not in path.read_text()


def test_network_retry_reopens_file(cfg, payload):
    calls = []
    def handler(request):
        if request.url.path == "/uploads":
            return httpx.Response(403)
        calls.append(request.read())
        assert payload.read_bytes() in calls[-1]
        if len(calls) == 1:
            raise httpx.ReadTimeout("lost response " + cfg.api_key)
        return httpx.Response(200, json=receipt(payload, "DUPLICATE"))
    result = run(cfg, payload, handler)
    assert result["retry_count"] == 1 and len(calls) == 2
    assert result["listing_check"] == "unavailable"


def test_network_exhausted(cfg, payload, caplog):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError(cfg.api_key)
    with pytest.raises(UploadError, match="Network failure") as error:
        run(cfg, payload, handler)
    assert len(calls) == 3
    assert cfg.api_key not in str(error.value) + caplog.text
    assert list((cfg.root / "data/pending").glob("*/payload"))


@pytest.mark.parametrize("bad", ["checksum", "status", "missing", "invalid_json", "cross_source"])
def test_unconfirmed_response_keeps_pending(cfg, payload, bad):
    body = receipt(payload)
    if bad == "checksum":
        body["metadata"]["sha256"] = "0" * 64
    elif bad == "status":
        body["status"] = "UPLOADED"
    elif bad == "missing":
        body = {}
    elif bad == "cross_source":
        body["status"] = "DUPLICATE"
        body["metadata"]["source"] = "usgs"
    def handler(request):
        return httpx.Response(200, text="not JSON") if bad == "invalid_json" else httpx.Response(200, json=body)
    with pytest.raises(UploadError):
        run(cfg, payload, handler)
    assert not list((cfg.root / "data/uploaded").glob("*"))
    assert list((cfg.root / "data/pending").glob("*/payload"))


def test_server_temporary_error_retry(cfg, payload):
    calls = []
    def handler(request):
        if request.url.path == "/uploads":
            return httpx.Response(200, json=[])
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=receipt(payload))
    result = run(cfg, payload, handler)
    assert result["retry_count"] == 1 and result["listing_check"] == "not_found"


def test_backoff_is_exponential_and_capped():
    delays = []
    for attempt in range(7):
        backoff(attempt, delays.append)
    assert delays == [1, 2, 4, 8, 16, 30, 30]


def test_success_response_cannot_leak_key_to_metadata_or_logs(cfg, payload):
    body = receipt(payload, file_id=cfg.api_key)
    def handler(request):
        if request.url.path == "/uploads":
            return httpx.Response(200, json=[body["metadata"]])
        return httpx.Response(200, json=body)
    result = run(cfg, payload, handler)
    assert result["server_file_id"] == "[REDACTED]"
    for path in cfg.root.rglob("*.json"):
        assert cfg.api_key not in path.read_text()


def test_checksum_reads_bounded_chunks(payload, monkeypatch):
    original_open = Path.open
    sizes = []
    class GuardedReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            sizes.append(size)
            return self.stream.read(size)
    monkeypatch.setattr(Path, "open", lambda self, *args, **kwargs: GuardedReader(original_open(self, *args, **kwargs)))
    assert len(sha256_file(payload)) == 64 and sizes
