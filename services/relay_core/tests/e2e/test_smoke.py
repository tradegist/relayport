"""Smoke tests — verify the relay stack is up and auth is enforced."""

import httpx


def test_health_ok(api: httpx.Client) -> None:
    resp = api.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_auth_required(anon_api: httpx.Client) -> None:
    resp = anon_api.post("/relays/ibkr/poll/0")
    assert resp.status_code == 401
