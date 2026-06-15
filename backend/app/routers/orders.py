"""Admin orders API — mirrors src/routes/orders.ts."""

import json
from typing import Annotated, Any, Literal

import stripe
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.deps.auth import require_admin
from app.db.models import (
    Customer,
    Discount,
    DiscountUsage,
    Inventory,
    Order,
    OrderItem,
    Refund,
    Variant,
)
from app.db.session import get_db
from app.domain.errors import ApiError
from app.domain.utils import generate_order_number, now_iso, uuid4
from app.lib.crypto import AuthContext
from app.lib.webhooks import dispatch_webhooks

router = APIRouter(tags=["Orders"])

OrderStatus = Literal[
    "pending", "paid", "processing", "shipped", "delivered", "refunded", "canceled"
]


class PaginationResponse(BaseModel):
    has_more: bool
    next_cursor: str | None


class OrderItemOut(BaseModel):
    sku: str
    title: str
    qty: int
    unit_price_cents: int


class OrderOut(BaseModel):
    id: str
    number: str
    status: str
    customer_email: str
    customer_id: str | None
    shipping: dict[str, Any]
    amounts: dict[str, Any]
    discount: dict[str, Any] | None
    tracking: dict[str, Any]
    stripe: dict[str, str | None]
    items: list[OrderItemOut]
    created_at: str


class OrderListOut(BaseModel):
    items: list[OrderOut]
    pagination: PaginationResponse


class UpdateOrderBody(BaseModel):
    status: OrderStatus | None = None
    tracking_number: str | None = None
    tracking_url: str | None = None


class RefundOrderBody(BaseModel):
    amount_cents: int | None = Field(default=None, gt=0)


class RefundOut(BaseModel):
    stripe_refund_id: str
    status: str


class TestOrderItemIn(BaseModel):
    sku: str
    qty: int = Field(gt=0)


class CreateTestOrderBody(BaseModel):
    customer_email: str
    items: list[TestOrderItemIn] = Field(min_length=1)
    discount_code: str | None = None


def format_order(order: Order, items: list[OrderItem] | list[dict[str, Any]]) -> dict[str, Any]:
    ship_to = None
    if order.ship_to:
        try:
            ship_to = json.loads(order.ship_to)
        except json.JSONDecodeError:
            ship_to = None

    def _item_fields(i: OrderItem | dict[str, Any]) -> dict[str, Any]:
        if isinstance(i, OrderItem):
            return {
                "sku": i.sku,
                "title": i.title,
                "qty": i.qty,
                "unit_price_cents": i.unit_price_cents,
            }
        return {
            "sku": i["sku"],
            "title": i["title"],
            "qty": i["qty"],
            "unit_price_cents": i["unit_price_cents"],
        }

    return {
        "id": order.id,
        "number": order.number,
        "status": order.status,
        "customer_email": order.customer_email,
        "customer_id": order.customer_id or None,
        "shipping": {
            "name": order.shipping_name or None,
            "phone": order.shipping_phone or None,
            "address": ship_to,
        },
        "amounts": {
            "subtotal_cents": order.subtotal_cents,
            "discount_cents": order.discount_amount_cents or 0,
            "tax_cents": order.tax_cents,
            "shipping_cents": order.shipping_cents,
            "total_cents": order.total_cents,
            "currency": order.currency,
        },
        "discount": (
            {
                "code": order.discount_code,
                "amount_cents": order.discount_amount_cents or 0,
            }
            if order.discount_code
            else None
        ),
        "tracking": {
            "number": order.tracking_number,
            "url": order.tracking_url,
            "shipped_at": order.shipped_at,
        },
        "stripe": {
            "checkout_session_id": order.stripe_checkout_session_id,
            "payment_intent_id": order.stripe_payment_intent_id,
        },
        "items": [_item_fields(i) for i in items],
        "created_at": order.created_at,
    }


def _calculate_discount(discount: Discount, subtotal_cents: int) -> int:
    if discount.type == "percentage":
        amount = (subtotal_cents * discount.value) // 100
        if discount.max_discount_cents is not None and amount > discount.max_discount_cents:
            amount = discount.max_discount_cents
        return amount
    if discount.type == "fixed_amount":
        return min(discount.value, subtotal_cents)
    return 0


def _validate_discount(
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
        minimum = f"{discount.min_purchase_cents / 100:.2f}"
        raise ApiError.invalid_request(f"Minimum purchase of ${minimum} required")

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


def _load_items_by_order(db: Session, order_ids: list[str]) -> dict[str, list[OrderItem]]:
    items_by_order: dict[str, list[OrderItem]] = {}
    if not order_ids:
        return items_by_order

    all_items = db.query(OrderItem).filter(OrderItem.order_id.in_(order_ids)).all()
    for item in all_items:
        items_by_order.setdefault(item.order_id, []).append(item)
    return items_by_order


def _active_discount_filter(current_time: str):
    return and_(
        Discount.status == "active",
        or_(Discount.starts_at.is_(None), Discount.starts_at <= current_time),
        or_(Discount.expires_at.is_(None), Discount.expires_at >= current_time),
    )


@router.get("/", response_model=OrderListOut)
async def list_orders(
    auth: Annotated[AuthContext, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    limit: str | None = Query(default="20"),
    cursor: str | None = Query(default=None),
    status: OrderStatus | None = Query(default=None),
    email: str | None = Query(default=None),
) -> dict[str, Any]:
    page_limit = min(int(limit or "20"), 100)

    query = db.query(Order)
    if status:
        query = query.filter(Order.status == status)
    if email:
        query = query.filter(Order.customer_email == email)
    if cursor:
        query = query.filter(Order.created_at < cursor)

    order_list = (
        query.order_by(Order.created_at.desc()).limit(page_limit + 1).all()
    )

    has_more = len(order_list) > page_limit
    if has_more:
        order_list = order_list[:page_limit]

    items_by_order = _load_items_by_order(db, [o.id for o in order_list])
    items = [format_order(o, items_by_order.get(o.id, [])) for o in order_list]
    next_cursor = items[-1]["created_at"] if has_more and items else None

    return {
        "items": items,
        "pagination": {"has_more": has_more, "next_cursor": next_cursor},
    }


@router.post("/test", response_model=OrderOut)
async def create_test_order(
    body: CreateTestOrderBody,
    auth: Annotated[AuthContext, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    subtotal = 0
    order_items: list[dict[str, Any]] = []

    for item in body.items:
        variant = db.query(Variant).filter(Variant.sku == item.sku).first()
        if not variant:
            raise ApiError.not_found(f"SKU not found: {item.sku}")

        inv = db.query(Inventory).filter(Inventory.sku == item.sku).first()
        available = (inv.on_hand if inv else 0) - (inv.reserved if inv else 0)
        if available < item.qty:
            raise ApiError.insufficient_inventory(item.sku)

        subtotal += variant.price_cents * item.qty
        order_items.append(
            {
                "sku": item.sku,
                "title": variant.title,
                "qty": item.qty,
                "unit_price_cents": variant.price_cents,
            }
        )

    discount_id: str | None = None
    discount_code: str | None = None
    discount_amount_cents = 0
    discount: Discount | None = None

    if body.discount_code:
        normalized_code = body.discount_code.upper().strip()
        discount = db.query(Discount).filter(Discount.code == normalized_code).first()
        if not discount:
            raise ApiError.not_found("Discount code not found")

        _validate_discount(db, discount, subtotal, body.customer_email)
        discount_amount_cents = _calculate_discount(discount, subtotal)
        discount_id = discount.id
        discount_code = discount.code

    total_cents = subtotal - discount_amount_cents
    timestamp = now_iso()
    customer_id: str | None = None

    existing_customer = (
        db.query(Customer).filter(Customer.email == body.customer_email).first()
    )
    if existing_customer:
        customer_id = existing_customer.id
        existing_customer.order_count = (existing_customer.order_count or 0) + 1
        existing_customer.total_spent_cents = (
            existing_customer.total_spent_cents or 0
        ) + total_cents
        existing_customer.last_order_at = timestamp
        existing_customer.updated_at = timestamp
    else:
        customer_id = uuid4()
        db.add(
            Customer(
                id=customer_id,
                email=body.customer_email,
                order_count=1,
                total_spent_cents=total_cents,
                last_order_at=timestamp,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )

    if discount and discount_amount_cents > 0:
        current_time = now_iso()

        if discount.usage_limit_per_customer is not None:
            usage_count = (
                db.query(func.count(DiscountUsage.id))
                .filter(
                    DiscountUsage.discount_id == discount.id,
                    DiscountUsage.customer_email == body.customer_email.lower(),
                )
                .scalar()
                or 0
            )
            if usage_count >= discount.usage_limit_per_customer:
                raise ApiError.invalid_request("You have already used this discount")

        if discount.usage_limit is not None:
            updated = (
                db.query(Discount)
                .filter(
                    Discount.id == discount_id,
                    _active_discount_filter(current_time),
                    Discount.usage_count < discount.usage_limit,
                )
                .update(
                    {
                        Discount.usage_count: Discount.usage_count + 1,
                        Discount.updated_at: current_time,
                    },
                    synchronize_session=False,
                )
            )
            if updated == 0:
                raise ApiError.invalid_request("Discount usage limit reached")
        else:
            updated = (
                db.query(Discount)
                .filter(Discount.id == discount_id, _active_discount_filter(current_time))
                .update({Discount.updated_at: current_time}, synchronize_session=False)
            )
            if updated == 0:
                raise ApiError.invalid_request("Discount is no longer valid")

    order_number = generate_order_number()
    order_id = uuid4()

    db.add(
        Order(
            id=order_id,
            customer_id=customer_id,
            number=order_number,
            status="paid",
            customer_email=body.customer_email,
            subtotal_cents=subtotal,
            tax_cents=0,
            shipping_cents=0,
            total_cents=total_cents,
            discount_code=discount_code,
            discount_id=discount_id,
            discount_amount_cents=discount_amount_cents,
            created_at=timestamp,
        )
    )

    for item in order_items:
        db.add(
            OrderItem(
                id=uuid4(),
                order_id=order_id,
                sku=item["sku"],
                title=item["title"],
                qty=item["qty"],
                unit_price_cents=item["unit_price_cents"],
            )
        )
        inv = db.query(Inventory).filter(Inventory.sku == item["sku"]).first()
        if inv:
            inv.on_hand -= item["qty"]
            inv.updated_at = timestamp

    if discount and discount_amount_cents > 0:
        existing_usage = (
            db.query(DiscountUsage)
            .filter(
                DiscountUsage.order_id == order_id,
                DiscountUsage.discount_id == discount_id,
            )
            .first()
        )
        if not existing_usage:
            db.add(
                DiscountUsage(
                    id=uuid4(),
                    discount_id=discount_id,
                    order_id=order_id,
                    customer_email=body.customer_email.lower(),
                    discount_amount_cents=discount_amount_cents,
                    created_at=timestamp,
                )
            )

    db.commit()

    order = db.query(Order).filter(Order.id == order_id).first()
    return format_order(order, order_items)


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(
    order_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise ApiError.not_found("Order not found")

    order_items = db.query(OrderItem).filter(OrderItem.order_id == order.id).all()
    return format_order(order, order_items)


@router.patch("/{order_id}", response_model=OrderOut)
async def update_order(
    order_id: str,
    body: UpdateOrderBody,
    auth: Annotated[AuthContext, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise ApiError.not_found("Order not found")

    previous_status = order.status
    has_updates = False

    if body.status is not None:
        order.status = body.status
        has_updates = True
        if body.status == "shipped" and not order.shipped_at:
            order.shipped_at = now_iso()

    if body.tracking_number is not None:
        order.tracking_number = body.tracking_number or None
        has_updates = True

    if body.tracking_url is not None:
        order.tracking_url = body.tracking_url or None
        has_updates = True

    if not has_updates:
        raise ApiError.invalid_request("No fields to update")

    db.commit()
    db.refresh(order)

    order_items = db.query(OrderItem).filter(OrderItem.order_id == order_id).all()
    formatted_order = format_order(order, order_items)

    if body.status is not None and body.status != previous_status:
        event_type = "order.shipped" if body.status == "shipped" else "order.updated"
        await dispatch_webhooks(
            db,
            event_type,
            {
                "order": formatted_order,
                "previous_status": previous_status,
            },
        )

    return formatted_order


@router.post("/{order_id}/refund", response_model=RefundOut)
async def refund_order(
    order_id: str,
    body: RefundOrderBody,
    auth: Annotated[AuthContext, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, str]:
    if not auth.stripe_secret_key:
        raise ApiError.invalid_request("Stripe not connected")

    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise ApiError.not_found("Order not found")
    if order.status == "refunded":
        raise ApiError.conflict("Order already refunded")
    if not order.stripe_payment_intent_id:
        raise ApiError.invalid_request("Cannot refund test orders (no Stripe payment)")

    stripe.api_key = auth.stripe_secret_key

    try:
        refund = stripe.Refund.create(
            payment_intent=order.stripe_payment_intent_id,
            amount=body.amount_cents,
        )

        db.add(
            Refund(
                id=uuid4(),
                order_id=order.id,
                stripe_refund_id=refund.id,
                amount_cents=refund.amount,
                status=refund.status or "succeeded",
                created_at=now_iso(),
            )
        )

        if not body.amount_cents or body.amount_cents >= order.total_cents:
            order.status = "refunded"
            db.commit()

            refunded_order = db.query(Order).filter(Order.id == order_id).first()
            order_items = (
                db.query(OrderItem).filter(OrderItem.order_id == order_id).all()
            )
            await dispatch_webhooks(
                db,
                "order.refunded",
                {
                    "order": format_order(refunded_order, order_items),
                    "refund": {
                        "stripe_refund_id": refund.id,
                        "amount_cents": refund.amount,
                    },
                },
            )
        else:
            db.commit()

        return {
            "stripe_refund_id": refund.id,
            "status": refund.status or "succeeded",
        }
    except stripe.StripeError as exc:
        raise ApiError.stripe_error(str(exc.user_message or exc)) from exc
    except Exception as exc:
        raise ApiError.stripe_error(str(exc) or "Refund failed") from exc
