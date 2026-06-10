"""Tests for order-error classification in the Binance client."""
from __future__ import annotations

import json

import requests

from hellocrypto.api import _is_notional_failure


def _http_error(payload):
    resp = requests.Response()
    resp._content = json.dumps(payload).encode()
    return requests.exceptions.HTTPError(response=resp)


def test_detects_notional_filter_failure():
    err = _http_error({"code": -1013, "msg": "Filter failure: NOTIONAL"})
    assert _is_notional_failure(err) is True


def test_ignores_other_1013_filters():
    # -1013 also covers LOT_SIZE — not a dust condition, must propagate.
    err = _http_error({"code": -1013, "msg": "Filter failure: LOT_SIZE"})
    assert _is_notional_failure(err) is False


def test_ignores_unrelated_error_codes():
    err = _http_error({"code": -2010, "msg": "Account has insufficient balance"})
    assert _is_notional_failure(err) is False
