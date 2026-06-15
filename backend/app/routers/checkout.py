from datetime import datetime, timedelta, timezone
from typing import Any

import stripe
from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, update
from sqlalchemy.orm import Session

from app.db.models import Cart, CartItem, Discount, DiscountUsage, Inventory, Variant
from app.db.session import get_db
from app.deps.auth import AuthContext, get_auth_context
from app.domain.errors import ApiError
from app.domain.utils import is_valid_email, now_iso, uuid4

router = APIRouter(tags=["Checkout"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class CartItemInput(BaseModel):
    sku: str = Field(..., min_length=1)
    qty: int = Field(..., gt=0)


class CreateCartBody(BaseModel):
    customer_email: EmailStr


class AddCartItemsBody(BaseModel):
    items: list[CartItemInput] = Field(..., min_length=1)


class CheckoutBody(BaseModel):
    success_url: str
    cancel_url: str
    collect_shipping: bool = False
    shipping_countries: list[str] = Field(default_factory=lambda: ["US"])
    shipping_options: list[Any] | None = None


class ApplyDiscountBody(BaseModel):
    code: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Discount helpers (mirrors src/routes/discounts.ts)
# ---------------------------------------------------------------------------


def validate_discount(
    db: Session,
    discount: Discount,
    subtotal_cents: int,
    customer_email: str | None = None,
) -> None:
    if discount.status != "active":
        raise ApiError.invalid_request("Discount is not active")

    current_time = now_iso()
    if discount.starts_at and current_time < discount.starts_at:
        raise ApiError.invalid_request("Discount has not started yet")
    if discount.expires_at and current_time > discount.expires_at:
        raise ApiError.invalid_request("Discount has expired")

    if discount.min_purchase_cents and subtotal_cents < discount.min_purchase_cents:
        min_dollars = discount.min_purchase_cents / 100
        raise ApiError.invalid_request(
            f"Minimum purchase of ${min_dollars:.2f} required"
        )

    if discount.usage_limit is not None and discount.usage_count >= discount.usage_limit:
        raise ApiError.invalid_request("Discount usage limit reached")

    if customer_email and discount.usage_limit_per_customer is not None:
        usage_count = (
            db.query(func.count(DiscountUsage.id))
            .filter(
                DiscountUsage.discount_id == discount.id,
                DiscountUsage.customer_email == customer_email.lower(),
            )
            .scalar()
            or 0
        )
        if usage_count >= discount.usage_limit_per_customer:
            raise ApiError.invalid_request("You have already used this discount")


def calculate_discount(discount: Discount, subtotal_cents: int) -> int:
    if discount.type == "percentage":
        amount = (subtotal_cents * discount.value) // 100
        if discount.max_discount_cents is not None and amount > discount.max_discount_cents:
            amount = discount.max_discount_cents
        return amount
    if discount.type == "fixed_amount":
        return min(discount.value, subtotal_cents)
    return 0


# ---------------------------------------------------------------------------
# Cart helpers
# ---------------------------------------------------------------------------


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


def _totals(subtotal_cents: int, discount_cents: int = 0) -> dict:
    return {
        "subtotal_cents": subtotal_cents,
        "discount_cents": discount_cents,
        "shipping_cents": 0,
        "tax_cents": 0,
        "total_cents": subtotal_cents - discount_cents,
    }


def _clear_cart_discount(db: Session, cart: Cart) -> None:
    cart.discount_code = None
    cart.discount_id = None
    cart.discount_amount_cents = 0


def _resolve_cart_discount(
    db: Session,
    cart: Cart,
    items: list[CartItem],
    *,
    clear_on_invalid: bool = False,
) -> tuple[dict | None, int]:
    if not cart.discount_id:
        return None, 0

    discount = db.get(Discount, cart.discount_id)
    if not discount:
        if clear_on_invalid:
            _clear_cart_discount(db, cart)
        return None, 0

    subtotal = _subtotal_cents(items)
    try:
        validate_discount(db, discount, subtotal, cart.customer_email)
        amount = calculate_discount(discount, subtotal)
        cart.discount_amount_cents = amount
        return (
            {
                "code": discount.code,
                "type": discount.type,
                "amount_cents": amount,
            },
            amount,
        )
    except ApiError:
        if clear_on_invalid:
            _clear_cart_discount(db, cart)
        return None, 0


def _get_cart_or_404(db: Session, cart_id: str) -> Cart:
    cart = db.get(Cart, cart_id)
    if not cart:
        raise ApiError.not_found("Cart not found")
    return cart


def _require_open_cart(cart: Cart) -> None:
    if cart.status != "open":
        raise ApiError.conflict("Cart is not open")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/{cart_id}")
def get_cart(
    cart_id: str,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(get_auth_context),
) -> dict:
    cart = _get_cart_or_404(db, cart_id)
    items = db.query(CartItem).filter(CartItem.cart_id == cart_id).all()
    return {
        "id": cart.id,
        "status": cart.status,
        "currency": cart.currency,
        "customer_email": cart.customer_email,
        "items": _cart_items_payload(items),
        "expires_at": cart.expires_at,
        "stripe_checkout_session_id": cart.stripe_checkout_session_id,
    }


@router.post("/")
def create_cart(
    body: CreateCartBody,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(get_auth_context),
) -> dict:
    if not is_valid_email(body.customer_email):
        raise ApiError.invalid_request("A valid customer_email is required")

    cart_id = uuid4()
    now = now_iso()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=30)
    ).isoformat()

    db.add(
        Cart(
            id=cart_id,
            status="open",
            customer_email=body.customer_email,
            currency="USD",
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()

    return {
        "id": cart_id,
        "status": "open",
        "currency": "USD",
        "customer_email": body.customer_email,
        "items": [],
        "discount": None,
        "totals": _totals(0),
        "expires_at": expires_at,
    }


@router.post("/{cart_id}/items")
def replace_cart_items(
    cart_id: str,
    body: AddCartItemsBody,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(get_auth_context),
) -> dict:
    cart = _get_cart_or_404(db, cart_id)
    _require_open_cart(cart)

    validated_items: list[dict] = []
    for item in body.items:
        variant = db.query(Variant).filter(Variant.sku == item.sku).first()
        if not variant:
            raise ApiError.not_found(f"SKU not found: {item.sku}")
        if variant.status != "active":
            raise ApiError.invalid_request(f"SKU not active: {item.sku}")

        inv = db.query(Inventory).filter(Inventory.sku == item.sku).first()
        on_hand = inv.on_hand if inv else 0
        reserved = inv.reserved if inv else 0
        available = on_hand - reserved
        if available < item.qty:
            raise ApiError.insufficient_inventory(item.sku)

        validated_items.append(
            {
                "sku": item.sku,
                "title": variant.title,
                "qty": item.qty,
                "unit_price_cents": variant.price_cents,
            }
        )

    db.query(CartItem).filter(CartItem.cart_id == cart_id).delete()
    for item in validated_items:
        db.add(
            CartItem(
                id=uuid4(),
                cart_id=cart_id,
                sku=item["sku"],
                title=item["title"],
                qty=item["qty"],
                unit_price_cents=item["unit_price_cents"],
            )
        )

    all_items = db.query(CartItem).filter(CartItem.cart_id == cart_id).all()
    subtotal = _subtotal_cents(all_items)
    discount_info, discount_amount = _resolve_cart_discount(
        db, cart, all_items, clear_on_invalid=True
    )
    db.commit()

    return {
        "id": cart.id,
        "status": cart.status,
        "currency": cart.currency,
        "customer_email": cart.customer_email,
        "items": _cart_items_payload(all_items),
        "discount": discount_info,
        "totals": _totals(subtotal, discount_amount),
        "expires_at": cart.expires_at,
    }


@router.post("/{cart_id}/checkout")
def checkout_cart(
    cart_id: str,
    body: CheckoutBody,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    if not auth.stripe_secret_key:
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

    subtotal = _subtotal_cents(items)
    discount: Discount | None = None
    discount_amount_cents = 0
    discount_reserved = False
    reserved_items: list[dict[str, Any]] = []

    def revert_cart_status() -> None:
        cart.status = "open"
        cart.updated_at = now_iso()

    def release_reserved_discount() -> None:
        if discount_reserved and discount:
            row = db.get(Discount, discount.id)
            if row:
                row.usage_count = max(0, row.usage_count - 1)
                row.updated_at = now_iso()

    def release_reserved_inventory() -> None:
        for reserved in reserved_items:
            inv = db.query(Inventory).filter(Inventory.sku == reserved["sku"]).first()
            if inv:
                inv.reserved = max(0, inv.reserved - reserved["qty"])
                inv.updated_at = now_iso()
        reserved_items.clear()

    if cart.discount_id:
        discount = db.get(Discount, cart.discount_id)
        if discount:
            try:
                validate_discount(db, discount, subtotal, cart.customer_email)
            except ApiError as err:
                _clear_cart_discount(db, cart)
                revert_cart_status()
                db.commit()
                raise err

            current_time = now_iso()

            if discount.usage_limit_per_customer is not None:
                usage_count = (
                    db.query(func.count(DiscountUsage.id))
                    .filter(
                        DiscountUsage.discount_id == discount.id,
                        DiscountUsage.customer_email == cart.customer_email.lower(),
                    )
                    .scalar()
                    or 0
                )
                if usage_count >= discount.usage_limit_per_customer:
                    _clear_cart_discount(db, cart)
                    revert_cart_status()
                    db.commit()
                    raise ApiError.invalid_request("You have already used this discount")

            if discount.usage_limit is not None:
                result = db.execute(
                    update(Discount)
                    .where(
                        Discount.id == discount.id,
                        Discount.status == "active",
                        or_(Discount.starts_at.is_(None), Discount.starts_at <= current_time),
                        or_(Discount.expires_at.is_(None), Discount.expires_at >= current_time),
                        Discount.usage_count < discount.usage_limit,
                    )
                    .values(usage_count=Discount.usage_count + 1, updated_at=current_time)
                )
                if result.rowcount == 0:
                    _clear_cart_discount(db, cart)
                    revert_cart_status()
                    db.commit()
                    raise ApiError.invalid_request("Discount usage limit reached")
                discount_reserved = True
            else:
                result = db.execute(
                    update(Discount)
                    .where(
                        Discount.id == discount.id,
                        Discount.status == "active",
                        or_(Discount.starts_at.is_(None), Discount.starts_at <= current_time),
                        or_(Discount.expires_at.is_(None), Discount.expires_at >= current_time),
                    )
                    .values(updated_at=current_time)
                )
                if result.rowcount == 0:
                    _clear_cart_discount(db, cart)
                    revert_cart_status()
                    db.commit()
                    raise ApiError.invalid_request("Discount is no longer valid")

            discount_amount_cents = calculate_discount(discount, subtotal)
        else:
            _clear_cart_discount(db, cart)

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
                raise ApiError.insufficient_inventory(item.sku)
            reserved_items.append({"sku": item.sku, "qty": item.qty})
    except ApiError:
        release_reserved_discount()
        release_reserved_inventory()
        revert_cart_status()
        db.commit()
        raise

    stripe.api_key = auth.stripe_secret_key

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

    stripe_coupon_id: str | None = None
    if discount and discount_amount_cents > 0:
        needs_on_the_fly_coupon = (
            discount.type == "percentage" and discount.max_discount_cents is not None
        )
        if discount.stripe_coupon_id and not needs_on_the_fly_coupon:
            stripe_coupon_id = discount.stripe_coupon_id
        else:
            try:
                coupon_params: dict[str, Any] = {
                    "duration": "once",
                    "metadata": {"merchant_discount_id": discount.id},
                }
                if discount.type == "percentage" and discount.max_discount_cents:
                    coupon_params["amount_off"] = discount_amount_cents
                    coupon_params["currency"] = "usd"
                elif discount.type == "percentage":
                    coupon_params["percent_off"] = discount.value
                else:
                    coupon_params["amount_off"] = discount.value
                    coupon_params["currency"] = "usd"

                coupon = stripe.Coupon.create(**coupon_params)
                stripe_coupon_id = coupon.id
            except stripe.StripeError as err:
                release_reserved_discount()
                release_reserved_inventory()
                revert_cart_status()
                db.commit()
                raise ApiError.invalid_request(
                    "Failed to apply discount. Please try again or remove the discount and proceed."
                ) from err

    default_shipping_options = [
        {
            "shipping_rate_data": {
                "type": "fixed_amount",
                "fixed_amount": {"amount": 0, "currency": "usd"},
                "display_name": "Standard Shipping",
                "delivery_estimate": {
                    "minimum": {"unit": "business_day", "value": 5},
                    "maximum": {"unit": "business_day", "value": 7},
                },
            }
        }
    ]

    session_params: dict[str, Any] = {
        "mode": "payment",
        "customer_email": cart.customer_email,
        "automatic_tax": {"enabled": True},
        "line_items": line_items,
        "success_url": body.success_url,
        "cancel_url": body.cancel_url,
        "metadata": {"cart_id": cart_id},
    }

    if body.collect_shipping:
        session_params["shipping_address_collection"] = {
            "allowed_countries": body.shipping_countries,
        }
        session_params["shipping_options"] = (
            body.shipping_options or default_shipping_options
        )

    if stripe_coupon_id:
        session_params["discounts"] = [{"coupon": stripe_coupon_id}]

    if discount:
        session_params["metadata"].update(
            {
                "discount_id": discount.id,
                "discount_code": discount.code or "",
                "discount_type": discount.type,
            }
        )

    try:
        session = stripe.checkout.Session.create(**session_params)
    except stripe.StripeError:
        release_reserved_discount()
        release_reserved_inventory()
        revert_cart_status()
        db.commit()
        raise ApiError.invalid_request("Payment processing error. Please try again.")

    cart.stripe_checkout_session_id = session.id
    cart.discount_amount_cents = discount_amount_cents
    cart.updated_at = now_iso()
    db.commit()

    return {
        "checkout_url": session.url,
        "stripe_checkout_session_id": session.id,
    }


@router.post("/{cart_id}/apply-discount")
def apply_discount(
    cart_id: str,
    body: ApplyDiscountBody,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(get_auth_context),
) -> dict:
    cart = _get_cart_or_404(db, cart_id)
    _require_open_cart(cart)

    normalized_code = body.code.upper().strip()
    discount = db.query(Discount).filter(Discount.code == normalized_code).first()
    if not discount:
        raise ApiError.not_found("Discount code not found")

    items = db.query(CartItem).filter(CartItem.cart_id == cart_id).all()
    if not items:
        raise ApiError.invalid_request("Cart is empty")

    subtotal = _subtotal_cents(items)
    validate_discount(db, discount, subtotal, cart.customer_email)
    discount_amount = calculate_discount(discount, subtotal)

    cart.discount_code = discount.code
    cart.discount_id = discount.id
    cart.discount_amount_cents = discount_amount
    db.commit()

    return {
        "discount": {
            "code": discount.code,
            "type": discount.type,
            "amount_cents": discount_amount,
        },
        "totals": _totals(subtotal, discount_amount),
    }


@router.delete("/{cart_id}/discount")
def remove_discount(
    cart_id: str,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(get_auth_context),
) -> dict:
    cart = _get_cart_or_404(db, cart_id)
    _require_open_cart(cart)

    _clear_cart_discount(db, cart)
    items = db.query(CartItem).filter(CartItem.cart_id == cart_id).all()
    subtotal = _subtotal_cents(items)
    db.commit()

    return {
        "discount": None,
        "totals": _totals(subtotal),
    }
