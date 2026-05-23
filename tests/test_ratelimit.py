"""Tests for the in-memory rate limiter decorator."""
from __future__ import annotations

import pytest
from flask import Flask

from hellocrypto import ratelimit
from hellocrypto.ratelimit import rate_limit


@pytest.fixture(autouse=True)
def _reset_buckets():
    ratelimit._hits.clear()
    yield
    ratelimit._hits.clear()


@pytest.fixture
def app():
    app = Flask(__name__)

    @app.post("/limited")
    @rate_limit(max_calls=2, per_seconds=10)
    def limited():
        return {"ok": True}

    return app


def test_first_calls_pass(app):
    client = app.test_client()
    assert client.post("/limited").status_code == 200
    assert client.post("/limited").status_code == 200


def test_third_call_is_blocked(app):
    client = app.test_client()
    client.post("/limited")
    client.post("/limited")
    r = client.post("/limited")
    assert r.status_code == 429
    assert "Retry-After" in r.headers
