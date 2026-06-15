"""Smoke tests for merchant API — OSS CI gate."""

import pytest


def test_root(client):
    r = client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["runtime"] == "python"


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ucp_discovery(client):
    r = client.get("/.well-known/ucp")
    assert r.status_code == 200
    assert r.json()["ucp"]["version"] == "2026-01-11"


def test_merchant_discovery(client):
    r = client.get("/.well-known/merchant")
    assert r.status_code == 200
    data = r.json()
    assert "protocols" in data
    assert "ucp" in data["protocols"]
    assert "mcp" in data["protocols"]


def test_oauth_discovery(client):
    r = client.get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    assert "authorization_endpoint" in r.json()


def test_mcp_tools_list(client):
    r = client.post(
        "/api/merchant/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert r.status_code == 200
    tools = r.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "search_products" in names
    assert "initiate_checkout" in names


def test_products_require_auth(client):
    r = client.get("/v1/products")
    assert r.status_code == 401


def test_products_list_empty(client, public_headers):
    r = client.get("/v1/products", headers=public_headers)
    assert r.status_code == 200
    assert "items" in r.json()


def test_create_product_admin(client, admin_headers):
    r = client.post(
        "/v1/products",
        headers=admin_headers,
        json={"title": "Test Tee", "description": "CI product"},
    )
    assert r.status_code == 201
    product = r.json()
    assert product["title"] == "Test Tee"
    assert product["variants"] == []


def test_create_cart(client, public_headers):
    r = client.post(
        "/v1/carts",
        headers=public_headers,
        json={"customer_email": "buyer@example.com"},
    )
    assert r.status_code == 200
    cart = r.json()
    assert cart["status"] == "open"
    assert cart["customer_email"] == "buyer@example.com"


def test_public_cannot_create_product(client, public_headers):
    r = client.post(
        "/v1/products",
        headers=public_headers,
        json={"title": "Blocked"},
    )
    assert r.status_code == 403


def test_rate_limit_headers(client, public_headers):
    r = client.get("/v1/products", headers=public_headers)
    assert r.status_code == 200
    assert "x-ratelimit-limit" in r.headers
