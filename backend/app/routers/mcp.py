"""MCP JSON-RPC commerce tools for RAILS AI marketplace integration."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import stripe
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.config import APP_VERSION, get_settings
from app.db.models import Cart, CartItem, Inventory, Product, Variant
from app.db.session import get_db, get_stripe_config
from app.domain.errors import ApiError
from app.domain.utils import is_valid_email, now_iso, uuid4

logger = logging.getLogger(__name__)

UCP_VERSION = "2026-01-11"

router = APIRouter(prefix="/api/merchant", tags=["MCP"])
root_router = APIRouter(tags=["MCP"])


# ---------------------------------------------------------------------------
# JSON-RPC schemas
# ---------------------------------------------------------------------------


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: str | int | None = None
    method: str
    params: dict[str, Any] | list[Any] | None = None


MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_products",
        "description": "Search the product catalog by title or description.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "status": {"type": "string", "enum": ["active", "draft"]},
            },
        },
    },
    {
        "name": "get_product",
        "description": "Get a single product with its variants by product ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "description": "Product ID"},
            },
            "required": ["product_id"],
        },
    },
    {
        "name": "get_or_create_cart",
        "description": "Get an existing open cart for a customer email or create a new one.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_email": {"type": "string", "format": "email"},
            },
            "required": ["customer_email"],
        },
    },
    {
        "name": "view_cart",
        "description": "View cart contents, totals, and status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cart_id": {"type": "string"},
            },
            "required": ["cart_id"],
        },
    },
    {
        "name": "add_to_cart",
        "description": "Add items to a cart (merges quantities for existing SKUs).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cart_id": {"type": "string"},
                "sku": {"type": "string"},
                "qty": {"type": "integer", "minimum": 1},
            },
            "required": ["cart_id", "sku", "qty"],
        },
    },
    {
        "name": "update_cart_item",
        "description": "Update quantity for a cart line item. Set qty to 0 to remove.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cart_id": {"type": "string"},
                "sku": {"type": "string"},
                "qty": {"type": "integer", "minimum": 0},
            },
            "required": ["cart_id", "sku", "qty"],
        },
    },
    {
        "name": "remove_cart_item",
        "description": "Remove a line item from the cart by SKU.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cart_id": {"type": "string"},
                "sku": {"type": "string"},
            },
            "required": ["cart_id", "sku"],
        },
    },
    {
        "name": "initiate_checkout",
        "description": "Start Stripe checkout for an open cart and return the payment URL.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cart_id": {"type": "string"},
                "success_url": {"type": "string", "format": "uri"},
                "cancel_url": {"type": "string", "format": "uri"},
                "collect_shipping": {"type": "boolean", "default": False},
            },
            "required": ["cart_id", "success_url", "cancel_url"],
        },
    },
]


# ---------------------------------------------------------------------------
# Cart / catalog helpers
# ---------------------------------------------------------------------------


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _variant_dict(variant: Variant) -> dict:
    return {
        "id": variant.id,
        "sku": variant.sku,
        "title": variant.title,
        "price_cents": variant.price_cents,
        "image_url": variant.image_url,
    }


def _product_dict(product: Product, variants: list[Variant]) -> dict:
    return {
        "id": product.id,
        "title": product.title,
        "description": product.description,
        "status": product.status,
        "created_at": product.created_at,
        "variants": [_variant_dict(v) for v in variants],
    }


def _cart_items_payload(items: list[CartItem]) -> list[dict]:
    return [
        {
            "sku": item.sku,
            "title": item.title,
            "qty": item.qty,
            "unit_price_cents": item.unit_price_cents,
        }
        for item in items
    ]


def _subtotal_cents(items: list[CartItem]) -> int:
    return sum(item.unit_price_cents * item.qty for item in items)


def _totals(subtotal_cents: int) -> dict:
    return {
        "subtotal_cents": subtotal_cents,
        "discount_cents": 0,
        "shipping_cents": 0,
        "tax_cents": 0,
        "total_cents": subtotal_cents,
    }


def _get_cart_or_404(db: Session, cart_id: str) -> Cart:
    cart = db.get(Cart, cart_id)
    if not cart:
        raise ApiError.not_found("Cart not found")
    return cart


def _require_open_cart(cart: Cart) -> None:
    if cart.status != "open":
        raise ApiError.conflict("Cart is not open")


def _cart_response(db: Session, cart: Cart) -> dict:
    items = db.query(CartItem).filter(CartItem.cart_id == cart.id).all()
    subtotal = _subtotal_cents(items)
    return {
        "id": cart.id,
        "status": cart.status,
        "currency": cart.currency,
        "customer_email": cart.customer_email,
        "items": _cart_items_payload(items),
        "totals": _totals(subtotal),
        "expires_at": cart.expires_at,
        "stripe_checkout_session_id": cart.stripe_checkout_session_id,
    }


def _validate_variant_for_cart(db: Session, sku: str, qty: int) -> dict:
    variant = db.query(Variant).filter(Variant.sku == sku).first()
    if not variant:
        raise ApiError.not_found(f"SKU not found: {sku}")
    if variant.status != "active":
        raise ApiError.invalid_request(f"SKU not active: {sku}")

    inv = db.query(Inventory).filter(Inventory.sku == sku).first()
    on_hand = inv.on_hand if inv else 0
    reserved = inv.reserved if inv else 0
    available = on_hand - reserved
    if available < qty:
        raise ApiError.insufficient_inventory(sku)

    return {
        "sku": sku,
        "title": variant.title,
        "qty": qty,
        "unit_price_cents": variant.price_cents,
    }


def _mcp_text_result(data: Any, *, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(data, indent=2)}],
        "isError": is_error,
    }


def _mcp_error_result(message: str) -> dict:
    return _mcp_text_result({"error": message}, is_error=True)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _tool_search_products(db: Session, args: dict[str, Any]) -> dict:
    query = (args.get("query") or "").strip()
    limit = min(int(args.get("limit") or 10), 50)
    status = args.get("status")

    q = db.query(Product)
    if status:
        q = q.filter(Product.status == status)
    else:
        q = q.filter(Product.status == "active")

    if query:
        pattern = f"%{query}%"
        q = q.filter(
            or_(Product.title.ilike(pattern), Product.description.ilike(pattern))
        )

    products = q.order_by(Product.created_at.desc()).limit(limit).all()
    product_ids = [p.id for p in products]
    variants_by_product: dict[str, list[Variant]] = {}
    if product_ids:
        variants = (
            db.query(Variant)
            .filter(Variant.product_id.in_(product_ids))
            .order_by(Variant.created_at.asc())
            .all()
        )
        for variant in variants:
            variants_by_product.setdefault(variant.product_id, []).append(variant)

    items = [_product_dict(p, variants_by_product.get(p.id, [])) for p in products]
    return _mcp_text_result({"items": items, "count": len(items)})


def _tool_get_product(db: Session, args: dict[str, Any]) -> dict:
    product_id = args.get("product_id")
    if not product_id:
        raise ApiError.invalid_request("product_id is required")

    product = db.get(Product, product_id)
    if not product:
        raise ApiError.not_found("Product not found")

    variants = (
        db.query(Variant)
        .filter(Variant.product_id == product_id)
        .order_by(Variant.created_at.asc())
        .all()
    )
    return _mcp_text_result(_product_dict(product, variants))


def _tool_get_or_create_cart(db: Session, args: dict[str, Any]) -> dict:
    email = (args.get("customer_email") or "").lower().strip()
    if not is_valid_email(email):
        raise ApiError.invalid_request("A valid customer_email is required")

    cart = (
        db.query(Cart)
        .filter(Cart.customer_email == email, Cart.status == "open")
        .order_by(Cart.created_at.desc())
        .first()
    )
    if cart:
        return _mcp_text_result(_cart_response(db, cart))

    cart_id = uuid4()
    now = now_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()

    cart = Cart(
        id=cart_id,
        status="open",
        customer_email=email,
        currency="USD",
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )
    db.add(cart)
    db.commit()

    return _mcp_text_result(_cart_response(db, cart))


def _tool_view_cart(db: Session, args: dict[str, Any]) -> dict:
    cart_id = args.get("cart_id")
    if not cart_id:
        raise ApiError.invalid_request("cart_id is required")

    cart = _get_cart_or_404(db, cart_id)
    return _mcp_text_result(_cart_response(db, cart))


def _tool_add_to_cart(db: Session, args: dict[str, Any]) -> dict:
    cart_id = args.get("cart_id")
    sku = args.get("sku")
    qty = int(args.get("qty") or 0)

    if not cart_id or not sku or qty < 1:
        raise ApiError.invalid_request("cart_id, sku, and qty (>=1) are required")

    cart = _get_cart_or_404(db, cart_id)
    _require_open_cart(cart)

    validated = _validate_variant_for_cart(db, sku, qty)
    existing = (
        db.query(CartItem)
        .filter(CartItem.cart_id == cart_id, CartItem.sku == sku)
        .first()
    )

    if existing:
        new_qty = existing.qty + qty
        _validate_variant_for_cart(db, sku, new_qty)
        existing.qty = new_qty
    else:
        db.add(
            CartItem(
                id=uuid4(),
                cart_id=cart_id,
                sku=validated["sku"],
                title=validated["title"],
                qty=validated["qty"],
                unit_price_cents=validated["unit_price_cents"],
            )
        )

    cart.updated_at = now_iso()
    db.commit()
    return _mcp_text_result(_cart_response(db, cart))


def _tool_update_cart_item(db: Session, args: dict[str, Any]) -> dict:
    cart_id = args.get("cart_id")
    sku = args.get("sku")
    qty = int(args.get("qty") if args.get("qty") is not None else -1)

    if not cart_id or not sku or qty < 0:
        raise ApiError.invalid_request("cart_id, sku, and qty (>=0) are required")

    cart = _get_cart_or_404(db, cart_id)
    _require_open_cart(cart)

    item = db.query(CartItem).filter(CartItem.cart_id == cart_id, CartItem.sku == sku).first()
    if not item:
        raise ApiError.not_found(f"SKU not in cart: {sku}")

    if qty == 0:
        db.delete(item)
    else:
        _validate_variant_for_cart(db, sku, qty)
        item.qty = qty

    cart.updated_at = now_iso()
    db.commit()
    return _mcp_text_result(_cart_response(db, cart))


def _tool_remove_cart_item(db: Session, args: dict[str, Any]) -> dict:
    cart_id = args.get("cart_id")
    sku = args.get("sku")
    if not cart_id or not sku:
        raise ApiError.invalid_request("cart_id and sku are required")

    cart = _get_cart_or_404(db, cart_id)
    _require_open_cart(cart)

    deleted = (
        db.query(CartItem)
        .filter(CartItem.cart_id == cart_id, CartItem.sku == sku)
        .delete()
    )
    if not deleted:
        raise ApiError.not_found(f"SKU not in cart: {sku}")

    cart.updated_at = now_iso()
    db.commit()
    return _mcp_text_result(_cart_response(db, cart))


def _tool_initiate_checkout(db: Session, args: dict[str, Any]) -> dict:
    cart_id = args.get("cart_id")
    success_url = args.get("success_url")
    cancel_url = args.get("cancel_url")
    collect_shipping = bool(args.get("collect_shipping", False))

    if not cart_id or not success_url or not cancel_url:
        raise ApiError.invalid_request("cart_id, success_url, and cancel_url are required")

    stripe_cfg = get_stripe_config(db)
    if not stripe_cfg.get("secret_key"):
        raise ApiError.invalid_request(
            "Stripe not connected. POST /v1/setup/stripe first."
        )

    now = now_iso()
    updated = (
        db.execute(
            update(Cart)
            .where(Cart.id == cart_id, Cart.status == "open")
            .values(status="checked_out", updated_at=now)
        ).rowcount
        or 0
    )

    if updated == 0:
        cart = db.get(Cart, cart_id)
        if not cart:
            raise ApiError.not_found("Cart not found")
        if cart.status != "open":
            raise ApiError.conflict("Cart is not open")
        raise ApiError.invalid_request("Failed to initiate checkout. Please try again.")

    cart = _get_cart_or_404(db, cart_id)
    items = db.query(CartItem).filter(CartItem.cart_id == cart_id).all()
    if not items:
        cart.status = "open"
        cart.updated_at = now_iso()
        db.commit()
        raise ApiError.invalid_request("Cart is empty")

    reserved_items: list[dict[str, Any]] = []

    def release_reserved_inventory() -> None:
        for reserved in reserved_items:
            inv = db.query(Inventory).filter(Inventory.sku == reserved["sku"]).first()
            if inv:
                inv.reserved = max(0, inv.reserved - reserved["qty"])
                inv.updated_at = now_iso()
        reserved_items.clear()

    try:
        for item in items:
            result = db.execute(
                update(Inventory)
                .where(
                    Inventory.sku == item.sku,
                    Inventory.on_hand - Inventory.reserved >= item.qty,
                )
                .values(
                    reserved=Inventory.reserved + item.qty,
                    updated_at=now_iso(),
                )
            )
            if result.rowcount == 0:
                release_reserved_inventory()
                cart.status = "open"
                cart.updated_at = now_iso()
                db.commit()
                raise ApiError.insufficient_inventory(item.sku)
            reserved_items.append({"sku": item.sku, "qty": item.qty})
    except ApiError:
        raise
    except Exception:
        release_reserved_inventory()
        cart.status = "open"
        cart.updated_at = now_iso()
        db.commit()
        raise

    stripe.api_key = stripe_cfg["secret_key"]
    line_items = [
        {
            "price_data": {
                "currency": "usd",
                "product_data": {"name": item.title},
                "unit_amount": item.unit_price_cents,
            },
            "quantity": item.qty,
        }
        for item in items
    ]

    session_params: dict[str, Any] = {
        "mode": "payment",
        "customer_email": cart.customer_email,
        "automatic_tax": {"enabled": True},
        "line_items": line_items,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {"cart_id": cart_id},
    }

    if collect_shipping:
        session_params["shipping_address_collection"] = {"allowed_countries": ["US"]}
        session_params["shipping_options"] = [
            {
                "shipping_rate_data": {
                    "type": "fixed_amount",
                    "fixed_amount": {"amount": 0, "currency": "usd"},
                    "display_name": "Standard Shipping",
                }
            }
        ]

    try:
        session = stripe.checkout.Session.create(**session_params)
    except stripe.StripeError as err:
        release_reserved_inventory()
        cart.status = "open"
        cart.updated_at = now_iso()
        db.commit()
        raise ApiError.invalid_request("Payment processing error. Please try again.") from err

    cart.stripe_checkout_session_id = session.id
    cart.updated_at = now_iso()
    db.commit()

    return _mcp_text_result(
        {
            "checkout_url": session.url,
            "stripe_checkout_session_id": session.id,
            "cart_id": cart_id,
        }
    )


TOOL_HANDLERS: dict[str, Callable[[Session, dict[str, Any]], dict]] = {
    "search_products": _tool_search_products,
    "get_product": _tool_get_product,
    "get_or_create_cart": _tool_get_or_create_cart,
    "view_cart": _tool_view_cart,
    "add_to_cart": _tool_add_to_cart,
    "update_cart_item": _tool_update_cart_item,
    "remove_cart_item": _tool_remove_cart_item,
    "initiate_checkout": _tool_initiate_checkout,
}


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------


def _jsonrpc_success(req_id: str | int | None, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(
    req_id: str | int | None,
    code: int,
    message: str,
    data: Any = None,
) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


def _normalize_params(params: dict[str, Any] | list[Any] | None) -> dict[str, Any]:
    if params is None:
        return {}
    if isinstance(params, dict):
        return params
    return {}


def _handle_mcp_method(
    db: Session,
    method: str,
    params: dict[str, Any] | list[Any] | None,
) -> Any:
    normalized = _normalize_params(params)

    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {
                "name": "merchant",
                "version": APP_VERSION,
            },
        }

    if method == "tools/list":
        return {"tools": MCP_TOOLS}

    if method == "tools/call":
        tool_name = normalized.get("name")
        arguments = normalized.get("arguments") or {}
        if not tool_name:
            raise ValueError("tools/call requires params.name")

        handler = TOOL_HANDLERS.get(tool_name)
        if not handler:
            raise ValueError(f"Unknown tool: {tool_name}")

        return handler(db, arguments)

    if method == "ping":
        return {}

    raise ValueError(f"Unknown method: {method}")


@router.post("/mcp")
def mcp_endpoint(
    body: JsonRpcRequest,
    db: Session = Depends(get_db),
) -> dict:
    if body.jsonrpc != "2.0":
        return _jsonrpc_error(body.id, -32600, "Invalid Request: jsonrpc must be '2.0'")

    if not body.method:
        return _jsonrpc_error(body.id, -32600, "Invalid Request: method is required")

    try:
        result = _handle_mcp_method(db, body.method, body.params)
        return _jsonrpc_success(body.id, result)
    except ApiError as err:
        detail = err.detail if isinstance(err.detail, dict) else {"message": str(err.detail)}
        error_body = detail.get("error", detail)
        message = error_body.get("message", "Request failed")
        return _jsonrpc_success(
            body.id,
            _mcp_error_result(message),
        )
    except ValueError as err:
        return _jsonrpc_error(body.id, -32602, str(err))
    except Exception as err:
        logger.exception("MCP handler error")
        return _jsonrpc_error(body.id, -32603, "Internal error", str(err))


@root_router.get("/.well-known/merchant")
def merchant_discovery(request: Request) -> dict:
    settings = get_settings()
    base = _base_url(request)

    return {
        "name": settings.store_name,
        "description": "Open-source commerce backend with UCP and MCP support",
        "version": APP_VERSION,
        "url": settings.merchant_url or base,
        "protocols": {
            "ucp": {
                "version": UCP_VERSION,
                "profile": f"{base}/.well-known/ucp",
                "checkout_endpoint": f"{base}/ucp/v1",
                "oauth_discovery": f"{base}/.well-known/oauth-authorization-server",
            },
            "mcp": {
                "version": "1.0",
                "transport": "http+json-rpc",
                "endpoint": f"{base}/api/merchant/mcp",
                "tools": [tool["name"] for tool in MCP_TOOLS],
            },
        },
        "capabilities": [
            "catalog",
            "cart",
            "checkout",
            "ucp",
            "oauth",
            "mcp",
        ],
        "rails": {
            "marketplace": True,
            "frontend_url": settings.rails_frontend_url,
        },
    }
