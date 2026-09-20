"""Compression is a capacity feature, not a cosmetic one.

Several pages fetch the whole employee roster on load; at ~1000 employees that
response is ~500KB of JSON, and bandwidth plus serialization dominate its cost.
GZipMiddleware cuts the wire size by 3-50x depending on how repetitive the data
is. These tests pin the behaviour that matters: it compresses for clients that
ask, it is transparent to clients that do not, and it leaves small responses
alone.
"""
import gzip
import json

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.testclient import TestClient

import app.main as app_main


def test_app_registers_gzip_middleware():
    """The middleware is wired into the real app, not just available to import."""
    assert any(
        mw.cls is GZipMiddleware for mw in app_main.app.user_middleware
    ), "GZipMiddleware is missing from app.main"


def _payload_app() -> FastAPI:
    """A stand-in app with the same middleware, so the test needs no database."""
    api = FastAPI()
    api.add_middleware(GZipMiddleware, minimum_size=1000)

    @api.get("/big")
    def big():
        return [{"id": i, "name": f"Employee {i}", "status": "active"} for i in range(500)]

    @api.get("/small")
    def small():
        return {"status": "ok"}

    return api


def test_large_response_is_compressed_and_decodes_identically():
    client = TestClient(_payload_app())

    gz = client.get("/big", headers={"Accept-Encoding": "gzip"})
    plain = client.get("/big", headers={"Accept-Encoding": "identity"})

    assert gz.headers.get("content-encoding") == "gzip"
    assert plain.headers.get("content-encoding") is None

    # The bytes on the wire differ; what the client ends up with must not.
    assert gz.json() == plain.json()
    assert len(gz.json()) == 500

    wire = int(gz.headers["content-length"])
    assert wire < len(plain.content), "compressed response is not smaller"


def test_client_that_cannot_gzip_still_gets_valid_json():
    """No API contract change: a client without Accept-Encoding is unaffected."""
    client = TestClient(_payload_app())
    r = client.get("/big", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") is None
    json.loads(r.content)  # raises if the body is not plain JSON


def test_small_responses_are_left_uncompressed():
    """Below the threshold, compression costs CPU and saves nothing."""
    client = TestClient(_payload_app())
    r = client.get("/small", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") is None
    assert r.json() == {"status": "ok"}
