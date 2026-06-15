import json
import logging
from typing import Any, Literal
from urllib.parse import urlparse

import stripe
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import (
    Cart,
    CartItem,
    Customer,
    CustomerAddress,
    Discount,
    DiscountUsage,
    Event,
    Inventory,
    InventoryLog,
    Order,
    OrderItem,
    UcpCheckoutSession,
    Webhook,
    WebhookDelivery,
    get_session_factory,
)
from app.db.session import get_db, get_stripe_config
from app.deps.auth import AuthContext, require_admin
from app.domain.errors import ApiError
from app.domain.utils import generate_order_number, now_iso, uuid4
from app.lib.crypto import generate_webhook_secret
from app.lib.webhooks import dispatch_webhooks, retry_delivery

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["Webhooks"])

VALID_EVENTS = frozenset(
    {
        "order.created",
        "order.updated",
        "order.shipped",
        "order.refunded",
        "inventory.low",
        "order.*",
        "*",
    }
)


# ---------------------------------------------------------------------------
# Schemas — outbound webhook CRUD
# ---------------------------------------------------------------------------


class WebhookOut(BaseModel):
    id: str
    url: str
    events: list[str]
    status: str
    has_secret: bool
    created_at: str


class WebhookWithSecretOut(BaseModel):
    id: str
    url: str
    events: list[str]
    status: str
    secret: str
    created_at: str


class WebhookListOut(BaseModel):
    items: list[WebhookOut]


class DeliverySummaryOut(BaseModel):
    id: str
    event_type: str
    status: str
    attempts: int
    response_code: int | None
    created_at: str
    last_attempt_at: str | None


class WebhookDetailOut(WebhookOut):
    recent_deliveries: list[DeliverySummaryOut]


class CreateWebhookIn(BaseModel):
    url: str
    events: list[str] = Field(min_length=1)


class UpdateWebhookIn(BaseModel):
    url: str | None = None
    events: list[str] | None = Field(default=None, min_length=1)
    status: Literal["active", "disabled"] | None = None


class WebhookDeliveryOut(BaseModel):
    id: str
    event_type: str
    payload: dict[str, Any]
    status: str
    attempts: int
    response_code: int | None
    response_body: str | None
    created_at: str
    last_attempt_at: str | None


class RotateSecretOut(BaseModel):
    secret: str


class RetryOut(BaseModel):
    status: str
    message: str


class DeletedOut(BaseModel):
    deleted: Literal[True] = True


class StripeOkOut(BaseModel):
    ok: Literal[True] = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_url(url: str) -> None:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Invalid protocol")
    except Exception:
        raise ApiError.invalid_request("url must be a valid HTTP(S) URL") from None


def _validate_events(events: list[str]) -> None:
    for event in events:
        if event not in VALID_EVENTS:
            raise ApiError.invalid_request(
                f"Invalid event type: {event}. Valid types: {', '.join(sorted(VALID_EVENTS))}"
            )


def _webhook_out(w: Webhook) -> WebhookOut:
    return WebhookOut(
        id=w.id,
        url=w.url,
        events=json.loads(w.events or "[]"),
        status=w.status,
        has_secret=bool(w.secret),
        created_at=w.created_at,
    )


def _payment_intent_id(session: stripe.checkout.Session) -> str | None:
    pi = session.payment_intent
    if pi is None:
        return None
    if isinstance(pi, str):
        return pi
    return getattr(pi, "id", None)


def _shipping_details(session: stripe.checkout.Session) -> tuple[str | None, str | None, Any | None]:
    shipping = getattr(session, "shipping_details", None) or getattr(session, "shipping", None)
    customer_details = session.customer_details

    shipping_name = None
    shipping_phone = None
    shipping_address = None

    if shipping:
        shipping_name = getattr(shipping, "name", None)
        shipping_phone = getattr(shipping, "phone", None)
        shipping_address = getattr(shipping, "address", None)

    if not shipping_name and customer_details:
        shipping_name = customer_details.name
    if not shipping_phone and customer_details:
        shipping_phone = customer_details.phone

    if shipping_address and hasattr(shipping_address, "to_dict"):
        shipping_address = shipping_address.to_dict()
    elif shipping_address is not None and not isinstance(shipping_address, dict):
        shipping_address = dict(shipping_address) if shipping_address else None

    return shipping_name, shipping_phone, shipping_address


async def _run_retry_delivery(webhook_id: str, delivery_id: str) -> None:
    db = get_session_factory()()
    try:
        webhook = db.get(Webhook, webhook_id)
        delivery = db.get(WebhookDelivery, delivery_id)
        if webhook and delivery:
            await retry_delivery(db, webhook, delivery)
    finally:
        db.close()


async def _handle_ucp_stripe_webhook(
    db: Session,
    stripe_session_id: str,
    webhook_session: stripe.checkout.Session,
) -> None:
    ucp_session_id = (webhook_session.metadata or {}).get("ucp_checkout_session_id")
    if not ucp_session_id:
        return

    session = (
        db.query(UcpCheckoutSession)
        .filter(
            UcpCheckoutSession.id == ucp_session_id,
            UcpCheckoutSession.stripe_session_id == stripe_session_id,
        )
        .first()
    )
    if not session or session.status == "completed":
        return

    line_items = json.loads(session.line_items or "[]")
    buyer = json.loads(session.buyer or "{}")
    totals = json.loads(session.totals or "[]")
    grand_total = next((t.get("amount", 0) for t in totals if t.get("type") == "grand_total"), 0)

    order_id = uuid4()
    order_number = generate_order_number()
    timestamp = now_iso()
    customer_email = buyer.get("email") or webhook_session.customer_email or ""

    order = Order(
        id=order_id,
        number=order_number,
        status="paid",
        customer_email=customer_email,
        subtotal_cents=grand_total,
        tax_cents=0,
        shipping_cents=0,
        total_cents=grand_total,
        currency=session.currency,
        stripe_checkout_session_id=stripe_session_id,
        created_at=timestamp,
    )
    db.add(order)

    for li in line_items:
        item = li.get("item", {})
        sku = item.get("id", "")
        title = item.get("title", "")
        qty = li.get("quantity", 0)
        unit_price = li.get("unit_price", {}).get("amount", 0)

        db.add(
            OrderItem(
                id=uuid4(),
                order_id=order_id,
                sku=sku,
                title=title,
                qty=qty,
                unit_price_cents=unit_price,
            )
        )

        inv = db.query(Inventory).filter(Inventory.sku == sku).first()
        if inv:
            inv.on_hand = (inv.on_hand or 0) - qty
            inv.reserved = max(0, (inv.reserved or 0) - qty)
            inv.updated_at = timestamp

    session.status = "completed"
    session.order_id = order_id
    session.order_number = order_number
    session.updated_at = timestamp
    db.commit()


async def _handle_cart_checkout_completed(
    db: Session,
    stripe_client: stripe.StripeClient,
    webhook_session: stripe.checkout.Session,
) -> None:
    cart_id = (webhook_session.metadata or {}).get("cart_id")
    if not cart_id:
        return

    cart = db.get(Cart, cart_id)
    if not cart:
        return

    items = db.query(CartItem).filter(CartItem.cart_id == cart_id).all()
    session = stripe_client.checkout.sessions.retrieve(webhook_session.id)

    discount_code = None
    discount_id = None
    discount_amount_cents = 0
    discount_row: Discount | None = None

    metadata = session.metadata or {}
    if metadata.get("discount_id"):
        discount_row = db.get(Discount, metadata["discount_id"])
        if discount_row:
            discount_code = discount_row.code
            discount_id = discount_row.id
            discount_amount_cents = cart.discount_amount_cents or 0

    subtotal_cents = sum(item.unit_price_cents * item.qty for item in items)
    order_number = generate_order_number()
    shipping_name, shipping_phone, shipping_address = _shipping_details(session)
    customer_email = cart.customer_email
    timestamp = now_iso()
    total_amount = session.amount_total or 0

    customer_id: str | None = None
    existing_customer = db.query(Customer).filter(Customer.email == customer_email).first()

    if existing_customer:
        customer_id = existing_customer.id
        existing_customer.name = shipping_name or existing_customer.name
        existing_customer.phone = shipping_phone or existing_customer.phone
        existing_customer.order_count = (existing_customer.order_count or 0) + 1
        existing_customer.total_spent_cents = (existing_customer.total_spent_cents or 0) + total_amount
        existing_customer.last_order_at = timestamp
        existing_customer.updated_at = timestamp
    else:
        customer_id = uuid4()
        db.add(
            Customer(
                id=customer_id,
                email=customer_email,
                name=shipping_name,
                phone=shipping_phone,
                order_count=1,
                total_spent_cents=total_amount,
                last_order_at=timestamp,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )

    if shipping_address and customer_id:
        line1 = shipping_address.get("line1")
        postal_code = shipping_address.get("postal_code")
        existing_address = (
            db.query(CustomerAddress)
            .filter(
                CustomerAddress.customer_id == customer_id,
                CustomerAddress.line1 == line1,
                CustomerAddress.postal_code == postal_code,
            )
            .first()
        )
        if not existing_address:
            address_count = (
                db.query(CustomerAddress)
                .filter(CustomerAddress.customer_id == customer_id)
                .count()
            )
            db.add(
                CustomerAddress(
                    id=uuid4(),
                    customer_id=customer_id,
                    is_default=1 if address_count == 0 else 0,
                    name=shipping_name,
                    line1=line1,
                    line2=shipping_address.get("line2"),
                    city=shipping_address.get("city") or "",
                    state=shipping_address.get("state"),
                    postal_code=postal_code or "",
                    country=shipping_address.get("country") or "US",
                    phone=shipping_phone,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )

    order_id = uuid4()
    ship_to_json = json.dumps(shipping_address) if shipping_address else None

    db.add(
        Order(
            id=order_id,
            customer_id=customer_id,
            number=order_number,
            status="paid",
            customer_email=customer_email,
            shipping_name=shipping_name,
            shipping_phone=shipping_phone,
            ship_to=ship_to_json,
            subtotal_cents=subtotal_cents,
            tax_cents=(session.total_details.amount_tax if session.total_details else 0) or 0,
            shipping_cents=(session.total_details.amount_shipping if session.total_details else 0) or 0,
            total_cents=total_amount,
            currency=cart.currency,
            discount_code=discount_code,
            discount_id=discount_id,
            discount_amount_cents=discount_amount_cents,
            stripe_checkout_session_id=session.id,
            stripe_payment_intent_id=_payment_intent_id(session),
            created_at=timestamp,
        )
    )
    db.flush()

    if discount_id and discount_amount_cents > 0:
        existing_usage = (
            db.query(DiscountUsage)
            .filter(DiscountUsage.order_id == order_id, DiscountUsage.discount_id == discount_id)
            .first()
        )
        if not existing_usage:
            customer_email_lower = customer_email.lower()
            if discount_row and discount_row.usage_limit_per_customer is not None:
                result = db.execute(
                    text(
                        """
                        INSERT INTO discount_usage
                            (id, discount_id, order_id, customer_email, discount_amount_cents, created_at)
                        SELECT :usage_id, :discount_id, :order_id, :customer_email,
                               :discount_amount_cents, :created_at
                        WHERE (
                            SELECT COUNT(*) FROM discount_usage
                            WHERE discount_id = :discount_id AND customer_email = :customer_email
                        ) < :limit
                        """
                    ),
                    {
                        "usage_id": uuid4(),
                        "discount_id": discount_id,
                        "order_id": order_id,
                        "customer_email": customer_email_lower,
                        "discount_amount_cents": discount_amount_cents,
                        "created_at": timestamp,
                        "limit": discount_row.usage_limit_per_customer,
                    },
                )
                if result.rowcount == 0:
                    logger.warning(
                        "Discount usage limit exceeded for customer %s and discount %s, "
                        "but order %s already created (payment succeeded).",
                        customer_email_lower,
                        discount_id,
                        order_id,
                    )
            else:
                db.add(
                    DiscountUsage(
                        id=uuid4(),
                        discount_id=discount_id,
                        order_id=order_id,
                        customer_email=customer_email_lower,
                        discount_amount_cents=discount_amount_cents,
                        created_at=timestamp,
                    )
                )

    for item in items:
        db.add(
            OrderItem(
                id=uuid4(),
                order_id=order_id,
                sku=item.sku,
                title=item.title,
                qty=item.qty,
                unit_price_cents=item.unit_price_cents,
            )
        )

        inv = db.query(Inventory).filter(Inventory.sku == item.sku).first()
        if inv:
            inv.reserved = max(0, (inv.reserved or 0) - item.qty)
            inv.on_hand = (inv.on_hand or 0) - item.qty
            inv.updated_at = timestamp

        db.add(
            InventoryLog(
                id=uuid4(),
                sku=item.sku,
                delta=-item.qty,
                reason="sale",
                created_at=timestamp,
            )
        )

    cart.status = "expired"
    cart.updated_at = timestamp
    db.commit()

    order_items = db.query(OrderItem).filter(OrderItem.order_id == order_id).all()
    await dispatch_webhooks(
        db,
        "order.created",
        {
            "order": {
                "id": order_id,
                "number": order_number,
                "status": "paid",
                "customer_email": customer_email,
                "customer_id": customer_id,
                "shipping": {
                    "name": shipping_name,
                    "phone": shipping_phone,
                    "address": shipping_address,
                },
                "amounts": {
                    "subtotal_cents": session.amount_subtotal or 0,
                    "tax_cents": (session.total_details.amount_tax if session.total_details else 0) or 0,
                    "shipping_cents": (session.total_details.amount_shipping if session.total_details else 0) or 0,
                    "total_cents": total_amount,
                    "currency": cart.currency,
                },
                "items": [
                    {
                        "sku": i.sku,
                        "title": i.title,
                        "qty": i.qty,
                        "unit_price_cents": i.unit_price_cents,
                    }
                    for i in order_items
                ],
                "stripe": {
                    "checkout_session_id": session.id,
                    "payment_intent_id": _payment_intent_id(session),
                },
            },
        },
    )


# ---------------------------------------------------------------------------
# Stripe inbound webhook (no bearer auth)
# ---------------------------------------------------------------------------


@router.post("/stripe", response_model=StripeOkOut)
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> StripeOkOut:
    signature = request.headers.get("stripe-signature")
    body = await request.body()

    if not signature:
        raise ApiError.invalid_request("Missing stripe-signature header")

    stripe_cfg = get_stripe_config(db)
    if not stripe_cfg.get("secret_key"):
        raise ApiError.invalid_request("Stripe not configured")
    if not stripe_cfg.get("webhook_secret"):
        raise ApiError.invalid_request("Stripe webhook secret not configured")

    try:
        event = stripe.Webhook.construct_event(
            body,
            signature,
            stripe_cfg["webhook_secret"],
        )
    except stripe.SignatureVerificationError as exc:
        raise ApiError("webhook_signature_invalid", 400, str(exc)) from exc

    existing = db.query(Event).filter(Event.stripe_event_id == event.id).first()
    if existing:
        return StripeOkOut()

    stripe_client = stripe.StripeClient(stripe_cfg["secret_key"])

    if event.type == "checkout.session.completed":
        webhook_session = event.data.object

        if (webhook_session.metadata or {}).get("ucp_checkout_session_id"):
            await _handle_ucp_stripe_webhook(db, webhook_session.id, webhook_session)

        if (webhook_session.metadata or {}).get("cart_id"):
            await _handle_cart_checkout_completed(db, stripe_client, webhook_session)

    db.add(
        Event(
            id=uuid4(),
            stripe_event_id=event.id,
            type=event.type,
            payload=json.dumps(event.data.object.to_dict() if hasattr(event.data.object, "to_dict") else dict(event.data.object)),
            processed_at=now_iso(),
        )
    )
    db.commit()
    return StripeOkOut()


# ---------------------------------------------------------------------------
# Outbound webhook CRUD (admin)
# ---------------------------------------------------------------------------


@router.get("", response_model=WebhookListOut)
def list_webhooks(
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
) -> WebhookListOut:
    webhooks = db.query(Webhook).order_by(Webhook.created_at.desc()).all()
    return WebhookListOut(items=[_webhook_out(w) for w in webhooks])


@router.get("/{webhook_id}", response_model=WebhookDetailOut)
def get_webhook(
    webhook_id: str,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
) -> WebhookDetailOut:
    webhook = db.get(Webhook, webhook_id)
    if not webhook:
        raise ApiError.not_found("Webhook not found")

    deliveries = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.webhook_id == webhook_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(20)
        .all()
    )

    base = _webhook_out(webhook)
    return WebhookDetailOut(
        **base.model_dump(),
        recent_deliveries=[
            DeliverySummaryOut(
                id=d.id,
                event_type=d.event_type,
                status=d.status,
                attempts=d.attempts,
                response_code=d.response_code,
                created_at=d.created_at,
                last_attempt_at=d.last_attempt_at,
            )
            for d in deliveries
        ],
    )


@router.post("", response_model=WebhookWithSecretOut, status_code=201)
def create_webhook(
    body: CreateWebhookIn,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
) -> WebhookWithSecretOut:
    _validate_url(body.url)
    _validate_events(body.events)

    webhook_id = uuid4()
    secret = generate_webhook_secret()
    timestamp = now_iso()

    webhook = Webhook(
        id=webhook_id,
        url=body.url,
        events=json.dumps(body.events),
        secret=secret,
        status="active",
        created_at=timestamp,
    )
    db.add(webhook)
    db.commit()

    return WebhookWithSecretOut(
        id=webhook_id,
        url=body.url,
        events=body.events,
        status="active",
        secret=secret,
        created_at=timestamp,
    )


@router.patch("/{webhook_id}", response_model=WebhookOut)
def update_webhook(
    webhook_id: str,
    body: UpdateWebhookIn,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
) -> WebhookOut:
    webhook = db.get(Webhook, webhook_id)
    if not webhook:
        raise ApiError.not_found("Webhook not found")

    if body.url is not None:
        _validate_url(body.url)
        webhook.url = body.url

    if body.events is not None:
        _validate_events(body.events)
        webhook.events = json.dumps(body.events)

    if body.status is not None:
        webhook.status = body.status

    db.commit()
    db.refresh(webhook)
    return _webhook_out(webhook)


@router.delete("/{webhook_id}", response_model=DeletedOut)
def delete_webhook(
    webhook_id: str,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
) -> DeletedOut:
    webhook = db.get(Webhook, webhook_id)
    if not webhook:
        raise ApiError.not_found("Webhook not found")

    db.query(WebhookDelivery).filter(WebhookDelivery.webhook_id == webhook_id).delete()
    db.delete(webhook)
    db.commit()
    return DeletedOut()


@router.post("/{webhook_id}/rotate-secret", response_model=RotateSecretOut)
def rotate_webhook_secret(
    webhook_id: str,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
) -> RotateSecretOut:
    webhook = db.get(Webhook, webhook_id)
    if not webhook:
        raise ApiError.not_found("Webhook not found")

    new_secret = generate_webhook_secret()
    webhook.secret = new_secret
    db.commit()
    return RotateSecretOut(secret=new_secret)


@router.get("/{webhook_id}/deliveries/{delivery_id}", response_model=WebhookDeliveryOut)
def get_delivery(
    webhook_id: str,
    delivery_id: str,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
) -> WebhookDeliveryOut:
    webhook = db.get(Webhook, webhook_id)
    if not webhook:
        raise ApiError.not_found("Webhook not found")

    delivery = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.id == delivery_id, WebhookDelivery.webhook_id == webhook_id)
        .first()
    )
    if not delivery:
        raise ApiError.not_found("Delivery not found")

    return WebhookDeliveryOut(
        id=delivery.id,
        event_type=delivery.event_type,
        payload=json.loads(delivery.payload),
        status=delivery.status,
        attempts=delivery.attempts,
        response_code=delivery.response_code,
        response_body=delivery.response_body,
        created_at=delivery.created_at,
        last_attempt_at=delivery.last_attempt_at,
    )


@router.post("/{webhook_id}/deliveries/{delivery_id}/retry", response_model=RetryOut)
async def retry_webhook_delivery(
    webhook_id: str,
    delivery_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
) -> RetryOut:
    webhook = db.get(Webhook, webhook_id)
    if not webhook:
        raise ApiError.not_found("Webhook not found")

    delivery = (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.id == delivery_id, WebhookDelivery.webhook_id == webhook_id)
        .first()
    )
    if not delivery:
        raise ApiError.not_found("Delivery not found")

    delivery.status = "pending"
    delivery.attempts = 0
    db.commit()

    background_tasks.add_task(_run_retry_delivery, webhook_id, delivery_id)

    return RetryOut(status="pending", message="Delivery retry triggered")
