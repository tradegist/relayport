"""Smoke tests — verify the stack is up and auth is enforced."""

import httpx


def test_health_connected(api: httpx.Client) -> None:
    resp = api.get("/health")
    assert resp.status_code == 200
    assert resp.json()["connected"] is True


def test_order_requires_auth(anon_api: httpx.Client) -> None:
    resp = anon_api.post("/ibkr/order")
    assert resp.status_code == 401
