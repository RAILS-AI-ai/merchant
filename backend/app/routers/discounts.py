import logging
from typing import Literal

import stripe
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models import Discount
from app.db.session import get_db
from app.deps.auth import AuthContext, require_admin
from app.domain.errors import ApiError
from app.domain.utils import now_iso, uuid4

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/discounts", tags=["Discounts"])

DiscountType = Literal["percentage", "fixed_amount"]
DiscountStatus = Literal["active", "inactive"]


class DiscountOut(BaseModel):
    id: str
    code: str | None
    type: str
    value: int
    status: str
    min_purchase_cents: int
    max_discount_cents: int | None
    starts_at: str | None
    expires_at: str | None
    usage_limit: int | None
    usage_limit_per_customer: int | None
    usage_count: int
    created_at: str
    updated_at: str | None = None


class DiscountListOut(BaseModel):
    items: list[DiscountOut]


class CreateDiscountIn(BaseModel):
    code: str | None = None
    type: DiscountType
    value: int = Field(ge=0)
    min_purchase_cents: int | None = Field(default=None, ge=0)
    max_discount_cents: int | None = Field(default=None, gt=0)
    starts_at: str | None = None
    expires_at: str | None = None
    usage_limit: int | None = Field(default=None, gt=0)
    usage_limit_per_customer: int | None = Field(default=None, gt=0)


class UpdateDiscountIn(BaseModel):
    status: DiscountStatus | None = None
    code: str | None = None
    value: int | None = Field(default=None, ge=0)
    min_purchase_cents: int | None = Field(default=None, ge=0)
    max_discount_cents: int | None = Field(default=None, gt=0)
    starts_at: str | None = None
    expires_at: str | None = None
    usage_limit: int | None = Field(default=None, gt=0)
    usage_limit_per_customer: int | None = Field(default=None, gt=0)


class OkOut(BaseModel):
    ok: Literal[True] = True


def _discount_out(d: Discount, *, include_updated: bool = False) -> DiscountOut:
    return DiscountOut(
        id=d.id,
        code=d.code,
        type=d.type,
        value=d.value,
        status=d.status,
        min_purchase_cents=d.min_purchase_cents or 0,
        max_discount_cents=d.max_discount_cents,
        starts_at=d.starts_at,
        expires_at=d.expires_at,
        usage_limit=d.usage_limit,
        usage_limit_per_customer=d.usage_limit_per_customer,
        usage_count=d.usage_count or 0,
        created_at=d.created_at,
        updated_at=d.updated_at if include_updated else None,
    )


def _sync_discount_to_stripe(
    stripe_secret_key: str | None,
    discount: Discount,
    *,
    status: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    if not stripe_secret_key:
        return None, None, None

    stripe.api_key = stripe_secret_key

    try:
        coupon_id = discount.stripe_coupon_id
        coupon_params: dict = {
            "duration": "once",
            "metadata": {"merchant_discount_id": discount.id},
        }

        if discount.type == "percentage":
            if discount.max_discount_cents:
                return discount.stripe_coupon_id, discount.stripe_promotion_code_id, None
            coupon_params["percent_off"] = discount.value
        else:
            coupon_params["amount_off"] = discount.value
            coupon_params["currency"] = "usd"

        if discount.expires_at:
            from datetime import datetime

            expires = datetime.fromisoformat(discount.expires_at.replace("Z", "+00:00"))
            coupon_params["redeem_by"] = int(expires.timestamp())

        if coupon_id:
            try:
                stripe.Coupon.delete(coupon_id)
            except stripe.StripeError:
                pass
            coupon_id = None

        coupon = stripe.Coupon.create(**coupon_params)
        coupon_id = coupon.id

        promotion_code_id = discount.stripe_promotion_code_id
        effective_status = status if status is not None else discount.status
        is_active = effective_status != "inactive"

        if discount.code and is_active:
            if promotion_code_id:
                try:
                    stripe.PromotionCode.modify(promotion_code_id, active=False)
                except stripe.StripeError:
                    pass

            promotion_code = stripe.PromotionCode.create(
                coupon=coupon_id,
                code=discount.code.upper(),
                active=True,
                metadata={"merchant_discount_id": discount.id},
            )
            promotion_code_id = promotion_code.id
        elif promotion_code_id:
            try:
                stripe.PromotionCode.modify(promotion_code_id, active=False)
            except stripe.StripeError:
                pass

        return coupon_id, promotion_code_id, None
    except stripe.StripeError as exc:
        logger.error("Failed to sync discount to Stripe: %s", exc)
        return (
            discount.stripe_coupon_id,
            discount.stripe_promotion_code_id,
            str(exc),
        )


@router.get("", response_model=DiscountListOut)
def list_discounts(
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
) -> DiscountListOut:
    discounts = db.query(Discount).order_by(Discount.created_at.desc()).all()
    return DiscountListOut(items=[_discount_out(d) for d in discounts])


@router.get("/{discount_id}", response_model=DiscountOut)
def get_discount(
    discount_id: str,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
) -> DiscountOut:
    discount = db.get(Discount, discount_id)
    if not discount:
        raise ApiError.not_found("Discount not found")
    return _discount_out(discount, include_updated=True)


@router.post("", response_model=DiscountOut, status_code=201)
def create_discount(
    body: CreateDiscountIn,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_admin),
) -> DiscountOut:
    if body.type == "percentage" and (body.value < 0 or body.value > 100):
        raise ApiError.invalid_request("percentage value must be between 0 and 100")

    normalized_code = body.code.upper().strip() if body.code else None
    if normalized_code:
        existing = db.query(Discount).filter(Discount.code == normalized_code).first()
        if existing:
            raise ApiError.conflict(f"Discount code {normalized_code} already exists")

    discount_id = uuid4()
    timestamp = now_iso()

    stripe_coupon_id = None
    stripe_promotion_code_id = None

    if auth.stripe_secret_key:
        temp = Discount(
            id=discount_id,
            code=normalized_code,
            type=body.type,
            value=body.value,
            max_discount_cents=body.max_discount_cents,
            expires_at=body.expires_at,
            stripe_coupon_id=None,
            stripe_promotion_code_id=None,
        )
        stripe_coupon_id, stripe_promotion_code_id, sync_error = _sync_discount_to_stripe(
            auth.stripe_secret_key,
            temp,
            status="active",
        )
        if sync_error:
            logger.warning("Discount %s created but Stripe sync failed: %s", discount_id, sync_error)

    discount = Discount(
        id=discount_id,
        code=normalized_code,
        type=body.type,
        value=body.value,
        min_purchase_cents=body.min_purchase_cents or 0,
        max_discount_cents=body.max_discount_cents,
        starts_at=body.starts_at,
        expires_at=body.expires_at,
        usage_limit=body.usage_limit,
        usage_limit_per_customer=body.usage_limit_per_customer,
        stripe_coupon_id=stripe_coupon_id,
        stripe_promotion_code_id=stripe_promotion_code_id,
        created_at=timestamp,
        updated_at=timestamp,
    )
    db.add(discount)
    db.commit()
    db.refresh(discount)
    return _discount_out(discount)


@router.patch("/{discount_id}", response_model=DiscountOut)
def update_discount(
    discount_id: str,
    body: UpdateDiscountIn,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_admin),
) -> DiscountOut:
    discount = db.get(Discount, discount_id)
    if not discount:
        raise ApiError.not_found("Discount not found")

    stripe_relevant = False

    if body.status is not None:
        discount.status = body.status
        stripe_relevant = True

    if body.code is not None:
        normalized_code = body.code.upper().strip() if body.code else None
        if normalized_code and normalized_code != discount.code:
            duplicate = (
                db.query(Discount)
                .filter(Discount.code == normalized_code, Discount.id != discount_id)
                .first()
            )
            if duplicate:
                raise ApiError.conflict(f"Discount code {normalized_code} already exists")
        discount.code = normalized_code
        stripe_relevant = True

    if body.value is not None:
        if discount.type == "percentage" and (body.value < 0 or body.value > 100):
            raise ApiError.invalid_request("percentage value must be between 0 and 100")
        discount.value = body.value
        stripe_relevant = True

    if body.min_purchase_cents is not None:
        discount.min_purchase_cents = body.min_purchase_cents

    if body.max_discount_cents is not None:
        discount.max_discount_cents = body.max_discount_cents
        stripe_relevant = True

    if body.starts_at is not None:
        discount.starts_at = body.starts_at

    if body.expires_at is not None:
        discount.expires_at = body.expires_at
        stripe_relevant = True

    if body.usage_limit is not None:
        discount.usage_limit = body.usage_limit

    if body.usage_limit_per_customer is not None:
        discount.usage_limit_per_customer = body.usage_limit_per_customer

    if any(
        v is not None
        for v in (
            body.status,
            body.code,
            body.value,
            body.min_purchase_cents,
            body.max_discount_cents,
            body.starts_at,
            body.expires_at,
            body.usage_limit,
            body.usage_limit_per_customer,
        )
    ):
        discount.updated_at = now_iso()

    db.commit()
    db.refresh(discount)

    if stripe_relevant and auth.stripe_secret_key:
        coupon_id, promotion_code_id, sync_error = _sync_discount_to_stripe(
            auth.stripe_secret_key,
            discount,
        )
        if sync_error:
            logger.warning("Discount %s updated but Stripe sync failed: %s", discount.id, sync_error)
        if (
            coupon_id != discount.stripe_coupon_id
            or promotion_code_id != discount.stripe_promotion_code_id
        ):
            discount.stripe_coupon_id = coupon_id
            discount.stripe_promotion_code_id = promotion_code_id
            db.commit()
            db.refresh(discount)

    return _discount_out(discount, include_updated=True)


@router.delete("/{discount_id}", response_model=OkOut)
def delete_discount(
    discount_id: str,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
) -> OkOut:
    discount = db.get(Discount, discount_id)
    if not discount:
        raise ApiError.not_found("Discount not found")

    discount.status = "inactive"
    discount.updated_at = now_iso()
    db.commit()
    return OkOut()
