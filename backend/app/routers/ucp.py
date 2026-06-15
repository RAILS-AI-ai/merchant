"""Universal Commerce Protocol (UCP) routes."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import stripe
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models import Inventory, Product, UcpCheckoutSession, Variant
from app.db.session import get_db, get_stripe_config
from app.domain.errors import ApiError
from app.domain.utils import now_iso, uuid4

UCP_VERSION = "2026-01-11"

router = APIRouter(tags=["UCP"])


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class UCPCheckoutCreateBody(BaseModel):
    line_items: list[dict[str, Any]] = Field(..., min_length=1)
    buyer: dict[str, Any] | None = None
    currency: str
    payment: dict[str, Any] | None = None


class UCPCheckoutUpdateBody(BaseModel):
    line_items: list[dict[str, Any]] | None = None
    buyer: dict[str, Any] | None = None
    currency: str | None = None
    payment: dict[str, Any] | None = None


class UCPCompleteBody(BaseModel):
    payment_data: dict[str, Any] | None = None
    risk_signals: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _ucp_envelope(capabilities: list[dict[str, str]]) -> dict:
    return {"version": UCP_VERSION, "capabilities": capabilities}


def _active_capabilities() -> list[dict[str, str]]:
    return [
        {"name": "dev.ucp.shopping.checkout", "version": UCP_VERSION},
        {"name": "dev.ucp.common.identity_linking", "version": UCP_VERSION},
        {"name": "dev.ucp.shopping.order", "version": UCP_VERSION},
    ]


def _parse_ucp_agent_header(header: str | None) -> dict[str, str | None]:
    if not header:
        return {}
    match = re.search(r'profile="([^"]+)"', header)
    return {"profile": match.group(1)} if match else {}


def _stripe_payment_handlers() -> list[dict[str, Any]]:
    return [
        {
            "id": "stripe_checkout",
            "name": "com.stripe.checkout",
            "version": UCP_VERSION,
            "spec": "https://stripe.com/docs/payments/checkout",
            "instrument_schemas": [
                "https://ucp.dev/schemas/shopping/types/card_payment_instrument.json",
            ],
            "config": {
                "type": "REDIRECT",
                "description": "Secure checkout via Stripe",
            },
        }
    ]


def _stripe_payment_handlers_minimal() -> list[dict[str, Any]]:
    return [
        {
            "id": "stripe_checkout",
            "name": "com.stripe.checkout",
            "version": UCP_VERSION,
            "spec": "https://stripe.com/docs/payments/checkout",
            "instrument_schemas": [
                "https://ucp.dev/schemas/shopping/types/card_payment_instrument.json",
            ],
            "config": {"type": "REDIRECT"},
        }
    ]


def _policy_links(base_url: str) -> list[dict[str, str]]:
    return [
        {"rel": "privacy_policy", "href": f"{base_url}/privacy", "title": "Privacy Policy"},
        {"rel": "terms_of_service", "href": f"{base_url}/terms", "title": "Terms of Service"},
    ]


def _lookup_variant(db: Session, item_id: str) -> tuple[Variant, Product] | None:
    row = (
        db.query(Variant, Product)
        .join(Product, Variant.product_id == Product.id)
        .filter((Variant.id == item_id) | (Variant.sku == item_id))
        .first()
    )
    return row if row else None


def _resolve_line_items(
    db: Session,
    line_items: list[dict[str, Any]],
    currency: str,
    *,
    preserve_ids: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    resolved: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    subtotal = 0
    currency_upper = currency.upper()

    for item in line_items:
        item_id = (item.get("item") or {}).get("id")
        quantity = item.get("quantity") or 1

        if not item_id:
            messages.append(
                {
                    "type": "error",
                    "code": "invalid_item",
                    "content": "Line item missing item.id",
                    "severity": "recoverable",
                }
            )
            continue

        row = _lookup_variant(db, item_id)
        if not row:
            messages.append(
                {
                    "type": "error",
                    "code": "item_not_found",
                    "content": f"Item {item_id} not found",
                    "severity": "recoverable",
                }
            )
            continue

        variant, product = row
        inv = db.query(Inventory).filter(Inventory.sku == variant.sku).first()
        available = (inv.on_hand - inv.reserved) if inv else 0

        if available < quantity:
            messages.append(
                {
                    "type": "error",
                    "code": "insufficient_inventory",
                    "content": f"Only {available} of {variant.sku} available",
                    "severity": "recoverable",
                }
            )

        unit_price = variant.price_cents
        total_price = unit_price * quantity
        subtotal += total_price

        resolved.append(
            {
                "id": item.get("id") if preserve_ids and item.get("id") else uuid4(),
                "item": {
                    "id": variant.sku,
                    "title": variant.title or product.title,
                    "description": product.description,
                    "image_url": variant.image_url,
                },
                "quantity": quantity,
                "unit_price": {"amount": unit_price, "currency": currency_upper},
                "total_price": {"amount": total_price, "currency": currency_upper},
            }
        )

    return resolved, messages, subtotal


def _derive_status(
    messages: list[dict[str, Any]],
    resolved_items: list[dict[str, Any]],
) -> Literal[
    "incomplete",
    "requires_escalation",
    "ready_for_complete",
    "complete_in_progress",
    "completed",
    "canceled",
]:
    has_errors = any(m.get("type") == "error" for m in messages)
    has_buyer_required = any(
        m.get("severity") in ("requires_buyer_input", "requires_buyer_review")
        for m in messages
    )

    if has_buyer_required:
        return "requires_escalation"
    if not has_errors and resolved_items:
        return "ready_for_complete"
    return "incomplete"


def _build_totals(subtotal: int, currency: str) -> list[dict[str, Any]]:
    currency_upper = currency.upper()
    return [
        {"type": "subtotal", "amount": subtotal, "currency": currency_upper},
        {"type": "grand_total", "amount": subtotal, "currency": currency_upper},
    ]


def _session_response(
    request: Request,
    db: Session,
    session: UcpCheckoutSession,
    *,
    payment_handlers: list[dict[str, Any]] | None = None,
    continue_url: str | None = None,
    extra_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    base = _base_url(request)
    line_items = json.loads(session.line_items or "[]")
    buyer_raw = json.loads(session.buyer or "null")
    totals = json.loads(session.totals or "[]")
    messages = json.loads(session.messages or "[]")
    if extra_messages:
        messages = extra_messages

    instruments = json.loads(session.payment_instruments or "null")

    if payment_handlers is None:
        stripe_cfg = get_stripe_config(db)
        payment_handlers = (
            _stripe_payment_handlers_minimal() if stripe_cfg.get("secret_key") else []
        )

    response: dict[str, Any] = {
        "ucp": _ucp_envelope(_active_capabilities()),
        "id": session.id,
        "status": session.status,
        "currency": session.currency,
        "line_items": line_items,
        "buyer": buyer_raw or None,
        "totals": totals,
        "messages": messages,
        "links": _policy_links(base),
        "payment": {
            "handlers": payment_handlers,
            "instruments": instruments or None,
        },
        "expires_at": session.expires_at,
    }

    escalation_url = continue_url
    if escalation_url is None and session.status == "requires_escalation":
        escalation_url = f"{base}/checkout/{session.id}"
    if escalation_url:
        response["continue_url"] = escalation_url

    if session.order_id:
        response["order"] = {
            "id": session.order_id,
            "number": session.order_number,
            "permalink_url": f"{base}/orders/{session.order_id}",
        }

    return response


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@router.get("/.well-known/ucp")
def ucp_profile(request: Request, db: Session = Depends(get_db)) -> dict:
    base = _base_url(request)
    stripe_cfg = get_stripe_config(db)

    payment_handlers: list[dict[str, Any]] = []
    if stripe_cfg.get("secret_key"):
        payment_handlers = _stripe_payment_handlers()

    return {
        "ucp": {
            "version": UCP_VERSION,
            "services": {
                "dev.ucp.shopping": {
                    "version": UCP_VERSION,
                    "spec": "https://ucp.dev/specification/checkout",
                    "rest": {
                        "schema": "https://ucp.dev/services/shopping/rest.openapi.json",
                        "endpoint": f"{base}/ucp/v1",
                    },
                },
                "dev.ucp.common": {
                    "version": UCP_VERSION,
                    "spec": "https://ucp.dev/specification/identity-linking",
                    "rest": {
                        "schema": "https://ucp.dev/services/common/rest.openapi.json",
                        "endpoint": base,
                    },
                },
            },
            "capabilities": [
                {
                    "name": "dev.ucp.shopping.checkout",
                    "version": UCP_VERSION,
                    "spec": "https://ucp.dev/specification/checkout",
                    "schema": "https://ucp.dev/schemas/shopping/checkout.json",
                },
                {
                    "name": "dev.ucp.common.identity_linking",
                    "version": UCP_VERSION,
                    "spec": "https://ucp.dev/specification/identity-linking",
                    "schema": "https://ucp.dev/schemas/common/identity_linking.json",
                },
                {
                    "name": "dev.ucp.shopping.order",
                    "version": UCP_VERSION,
                    "spec": "https://ucp.dev/specification/order",
                    "schema": "https://ucp.dev/schemas/shopping/order.json",
                },
            ],
        },
        "payment": {"handlers": payment_handlers},
    }


# ---------------------------------------------------------------------------
# Checkout sessions
# ---------------------------------------------------------------------------


@router.post("/ucp/v1/checkout-sessions", status_code=201)
def create_checkout_session(
    request: Request,
    body: UCPCheckoutCreateBody,
    db: Session = Depends(get_db),
) -> dict:
    _parse_ucp_agent_header(request.headers.get("UCP-Agent"))

    if not body.line_items:
        raise ApiError.invalid_request("line_items is required and must not be empty")
    if not body.currency:
        raise ApiError.invalid_request("currency is required")

    resolved_items, messages, subtotal = _resolve_line_items(
        db, body.line_items, body.currency
    )
    status = _derive_status(messages, resolved_items)
    totals = _build_totals(subtotal, body.currency)

    session_id = uuid4()
    now = now_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()

    db.add(
        UcpCheckoutSession(
            id=session_id,
            status=status,
            currency=body.currency.upper(),
            line_items=json.dumps(resolved_items),
            buyer=json.dumps(body.buyer) if body.buyer else None,
            totals=json.dumps(totals),
            messages=json.dumps(messages),
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()

    stripe_cfg = get_stripe_config(db)
    payment_handlers = (
        _stripe_payment_handlers_minimal() if stripe_cfg.get("secret_key") else []
    )

    base = _base_url(request)
    return {
        "ucp": _ucp_envelope(_active_capabilities()),
        "id": session_id,
        "status": status,
        "currency": body.currency.upper(),
        "line_items": resolved_items,
        "buyer": body.buyer,
        "totals": totals,
        "messages": messages,
        "links": _policy_links(base),
        "payment": {"handlers": payment_handlers},
        "continue_url": f"{base}/checkout/{session_id}" if status == "requires_escalation" else None,
        "expires_at": expires_at,
    }


@router.get("/ucp/v1/checkout-sessions/{session_id}")
def get_checkout_session(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    session = db.get(UcpCheckoutSession, session_id)
    if not session:
        raise ApiError.not_found("Checkout session not found")

    if session.expires_at and session.expires_at < now_iso():
        session.status = "canceled"
        session.updated_at = now_iso()
        db.commit()

    return _session_response(request, db, session)


@router.put("/ucp/v1/checkout-sessions/{session_id}")
def update_checkout_session(
    session_id: str,
    request: Request,
    body: UCPCheckoutUpdateBody,
    db: Session = Depends(get_db),
) -> dict:
    session = db.get(UcpCheckoutSession, session_id)
    if not session:
        raise ApiError.not_found("Checkout session not found")

    if session.status in ("completed", "canceled"):
        raise ApiError.invalid_request(f"Cannot update {session.status} checkout session")

    currency = (body.currency or session.currency).upper()
    line_items_input = body.line_items or []

    resolved_items, messages, subtotal = _resolve_line_items(
        db,
        line_items_input,
        currency,
        preserve_ids=True,
    )
    status = _derive_status(messages, resolved_items)
    totals = _build_totals(subtotal, currency)
    now = now_iso()

    session.status = status
    session.currency = currency
    session.line_items = json.dumps(resolved_items)
    session.buyer = json.dumps(body.buyer) if body.buyer else None
    session.totals = json.dumps(totals)
    session.messages = json.dumps(messages)
    session.updated_at = now
    db.commit()

    stripe_cfg = get_stripe_config(db)
    payment_handlers = (
        _stripe_payment_handlers_minimal() if stripe_cfg.get("secret_key") else []
    )

    return {
        "ucp": _ucp_envelope(_active_capabilities()),
        "id": session_id,
        "status": status,
        "currency": currency,
        "line_items": resolved_items,
        "buyer": body.buyer,
        "totals": totals,
        "messages": messages,
        "links": _policy_links(_base_url(request)),
        "payment": {"handlers": payment_handlers},
        "expires_at": session.expires_at,
    }


@router.post("/ucp/v1/checkout-sessions/{session_id}/complete")
def complete_checkout_session(
    session_id: str,
    request: Request,
    body: UCPCompleteBody,
    db: Session = Depends(get_db),
) -> dict:
    session = db.get(UcpCheckoutSession, session_id)
    if not session:
        raise ApiError.not_found("Checkout session not found")

    if session.status == "completed":
        raise ApiError.invalid_request("Checkout already completed")
    if session.status == "canceled":
        raise ApiError.invalid_request("Checkout was canceled")
    if session.status != "ready_for_complete":
        raise ApiError.invalid_request(f"Cannot complete checkout in {session.status} state")

    session.status = "complete_in_progress"
    session.updated_at = now_iso()
    db.commit()

    line_items = json.loads(session.line_items or "[]")
    buyer = json.loads(session.buyer or "{}")
    totals = json.loads(session.totals or "[]")

    stripe_cfg = get_stripe_config(db)
    payment_data = body.payment_data or {}
    base = _base_url(request)

    if stripe_cfg.get("secret_key") and payment_data.get("handler_id") == "stripe_checkout":
        stripe.api_key = stripe_cfg["secret_key"]

        stripe_line_items = [
            {
                "price_data": {
                    "currency": session.currency.lower(),
                    "product_data": {
                        "name": li.get("item", {}).get("title") or li.get("item", {}).get("id"),
                        "description": li.get("item", {}).get("description"),
                        "images": [li["item"]["image_url"]]
                        if li.get("item", {}).get("image_url")
                        else None,
                    },
                    "unit_amount": li.get("unit_price", {}).get("amount"),
                },
                "quantity": li.get("quantity"),
            }
            for li in line_items
        ]

        success_url = payment_data.get("success_url") or (
            f"{base}/ucp/v1/checkout-sessions/{session_id}/success"
        )
        cancel_url = payment_data.get("cancel_url") or (
            f"{base}/ucp/v1/checkout-sessions/{session_id}/cancel"
        )

        stripe_session = stripe.checkout.Session.create(
            mode="payment",
            line_items=stripe_line_items,
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=buyer.get("email"),
            metadata={"ucp_checkout_session_id": session_id},
        )

        session.stripe_session_id = stripe_session.id
        session.updated_at = now_iso()
        db.commit()

        return {
            "ucp": _ucp_envelope(_active_capabilities()),
            "id": session_id,
            "status": "requires_escalation",
            "currency": session.currency,
            "line_items": line_items,
            "buyer": buyer or None,
            "totals": totals,
            "messages": [
                {
                    "type": "info",
                    "code": "payment_required",
                    "content": "Redirect to payment provider to complete purchase",
                }
            ],
            "links": _policy_links(base),
            "payment": {
                "handlers": [
                    {
                        "id": "stripe_checkout",
                        "name": "com.stripe.checkout",
                        "version": UCP_VERSION,
                        "spec": "https://stripe.com/docs/payments/checkout",
                        "instrument_schemas": [],
                    }
                ]
            },
            "continue_url": stripe_session.url,
            "expires_at": session.expires_at,
        }

    raise ApiError.invalid_request("No valid payment handler specified")


@router.delete("/ucp/v1/checkout-sessions/{session_id}")
def cancel_checkout_session(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    session = db.get(UcpCheckoutSession, session_id)
    if not session:
        raise ApiError.not_found("Checkout session not found")

    if session.status == "completed":
        raise ApiError.invalid_request("Cannot cancel completed checkout")

    session.status = "canceled"
    session.updated_at = now_iso()
    db.commit()

    return _session_response(
        request,
        db,
        session,
        payment_handlers=[],
        extra_messages=[
            {
                "type": "info",
                "code": "checkout_canceled",
                "content": "Checkout session has been canceled",
            }
        ],
    )
