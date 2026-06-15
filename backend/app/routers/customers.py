"""Admin customers API — mirrors src/routes/customers.ts."""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.deps.auth import require_admin
from app.db.models import Customer, CustomerAddress, Order, OrderItem
from app.db.session import get_db
from app.domain.errors import ApiError
from app.domain.utils import now_iso, uuid4
from app.lib.crypto import AuthContext

router = APIRouter(tags=["Customers"])


class PaginationResponse(BaseModel):
    has_more: bool
    next_cursor: str | None


class CustomerStatsOut(BaseModel):
    order_count: int
    total_spent_cents: int
    last_order_at: str | None


class CustomerOut(BaseModel):
    id: str
    email: str
    name: str | None
    phone: str | None
    has_account: bool
    accepts_marketing: bool
    stats: CustomerStatsOut
    metadata: dict[str, Any] | None
    created_at: str
    updated_at: str


class AddressOut(BaseModel):
    id: str
    label: str | None
    is_default: bool
    name: str | None
    company: str | None
    line1: str
    line2: str | None
    city: str
    state: str | None
    postal_code: str
    country: str
    phone: str | None


class CustomerWithAddressesOut(CustomerOut):
    addresses: list[AddressOut]


class CustomerListOut(BaseModel):
    items: list[CustomerOut]
    pagination: PaginationResponse


class CustomerOrderOut(BaseModel):
    id: str
    number: str
    status: str
    shipping: dict[str, Any]
    amounts: dict[str, Any]
    items: list[dict[str, Any]]
    tracking: dict[str, Any] | None
    created_at: str


class CustomerOrdersOut(BaseModel):
    items: list[CustomerOrderOut]
    pagination: PaginationResponse


class UpdateCustomerBody(BaseModel):
    name: str | None = None
    phone: str | None = None
    accepts_marketing: bool | None = None
    metadata: dict[str, Any] | None = None


class CreateAddressBody(BaseModel):
    label: str | None = None
    is_default: bool | None = None
    name: str | None = None
    company: str | None = None
    line1: str = Field(min_length=1)
    line2: str | None = None
    city: str = Field(min_length=1)
    state: str | None = None
    postal_code: str = Field(min_length=1)
    country: str = "US"
    phone: str | None = None


class DeletedOut(BaseModel):
    deleted: bool = True


def format_customer(customer: Customer) -> dict[str, Any]:
    metadata = None
    if customer.metadata_json:
        try:
            metadata = json.loads(customer.metadata_json)
        except json.JSONDecodeError:
            metadata = None

    return {
        "id": customer.id,
        "email": customer.email,
        "name": customer.name,
        "phone": customer.phone,
        "has_account": bool(customer.password_hash),
        "accepts_marketing": bool(customer.accepts_marketing),
        "stats": {
            "order_count": customer.order_count or 0,
            "total_spent_cents": customer.total_spent_cents or 0,
            "last_order_at": customer.last_order_at,
        },
        "metadata": metadata,
        "created_at": customer.created_at,
        "updated_at": customer.updated_at,
    }


def format_address(address: CustomerAddress) -> dict[str, Any]:
    return {
        "id": address.id,
        "label": address.label,
        "is_default": bool(address.is_default),
        "name": address.name,
        "company": address.company,
        "line1": address.line1,
        "line2": address.line2,
        "city": address.city,
        "state": address.state,
        "postal_code": address.postal_code,
        "country": address.country,
        "phone": address.phone,
    }


def format_customer_order(order: Order, items: list[OrderItem]) -> dict[str, Any]:
    ship_to = None
    if order.ship_to:
        try:
            ship_to = json.loads(order.ship_to)
        except json.JSONDecodeError:
            ship_to = None

    tracking = None
    if order.tracking_number:
        tracking = {
            "number": order.tracking_number,
            "url": order.tracking_url,
            "shipped_at": order.shipped_at,
        }

    return {
        "id": order.id,
        "number": order.number,
        "status": order.status,
        "shipping": {
            "name": order.shipping_name,
            "phone": order.shipping_phone,
            "address": ship_to,
        },
        "amounts": {
            "subtotal_cents": order.subtotal_cents,
            "tax_cents": order.tax_cents,
            "shipping_cents": order.shipping_cents,
            "total_cents": order.total_cents,
            "currency": order.currency,
        },
        "items": [
            {
                "sku": i.sku,
                "title": i.title,
                "qty": i.qty,
                "unit_price_cents": i.unit_price_cents,
            }
            for i in items
        ],
        "tracking": tracking,
        "created_at": order.created_at,
    }


def _load_items_by_order(db: Session, order_ids: list[str]) -> dict[str, list[OrderItem]]:
    items_by_order: dict[str, list[OrderItem]] = {}
    if not order_ids:
        return items_by_order

    all_items = db.query(OrderItem).filter(OrderItem.order_id.in_(order_ids)).all()
    for item in all_items:
        items_by_order.setdefault(item.order_id, []).append(item)
    return items_by_order


@router.get("/", response_model=CustomerListOut)
async def list_customers(
    auth: Annotated[AuthContext, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    limit: str | None = Query(default="50"),
    cursor: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> dict[str, Any]:
    page_limit = min(int(limit or "50"), 100)

    query = db.query(Customer)
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            (Customer.email.like(pattern)) | (Customer.name.like(pattern))
        )
    if cursor:
        query = query.filter(Customer.created_at < cursor)

    rows = query.order_by(Customer.created_at.desc()).limit(page_limit + 1).all()

    has_more = len(rows) > page_limit
    items = rows[:page_limit] if has_more else rows

    return {
        "items": [format_customer(c) for c in items],
        "pagination": {
            "has_more": has_more,
            "next_cursor": items[-1].created_at if has_more and items else None,
        },
    }


@router.get("/{customer_id}", response_model=CustomerWithAddressesOut)
async def get_customer(
    customer_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise ApiError.not_found("Customer")

    addresses = (
        db.query(CustomerAddress)
        .filter(CustomerAddress.customer_id == customer_id)
        .order_by(CustomerAddress.is_default.desc(), CustomerAddress.created_at.desc())
        .all()
    )

    return {
        **format_customer(customer),
        "addresses": [format_address(a) for a in addresses],
    }


@router.get("/{customer_id}/orders", response_model=CustomerOrdersOut)
async def get_customer_orders(
    customer_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    limit: str | None = Query(default="20"),
    cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    page_limit = min(int(limit or "20"), 100)

    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise ApiError.not_found("Customer")

    query = db.query(Order).filter(Order.customer_id == customer_id)
    if cursor:
        query = query.filter(Order.created_at < cursor)

    rows = query.order_by(Order.created_at.desc()).limit(page_limit + 1).all()

    has_more = len(rows) > page_limit
    items = rows[:page_limit] if has_more else rows

    items_by_order = _load_items_by_order(db, [o.id for o in items])

    return {
        "items": [
            format_customer_order(o, items_by_order.get(o.id, [])) for o in items
        ],
        "pagination": {
            "has_more": has_more,
            "next_cursor": items[-1].created_at if has_more and items else None,
        },
    }


@router.patch("/{customer_id}", response_model=CustomerOut)
async def update_customer(
    customer_id: str,
    body: UpdateCustomerBody,
    auth: Annotated[AuthContext, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise ApiError.not_found("Customer")

    has_updates = False

    if body.name is not None:
        customer.name = body.name
        has_updates = True
    if body.phone is not None:
        customer.phone = body.phone
        has_updates = True
    if body.accepts_marketing is not None:
        customer.accepts_marketing = 1 if body.accepts_marketing else 0
        has_updates = True
    if body.metadata is not None:
        customer.metadata_json = json.dumps(body.metadata)
        has_updates = True

    if not has_updates:
        return format_customer(customer)

    customer.updated_at = now_iso()
    db.commit()
    db.refresh(customer)

    return format_customer(customer)


@router.post(
    "/{customer_id}/addresses",
    response_model=AddressOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_address(
    customer_id: str,
    body: CreateAddressBody,
    auth: Annotated[AuthContext, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, Any]:
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise ApiError.not_found("Customer")

    address_id = uuid4()
    timestamp = now_iso()

    if body.is_default:
        db.query(CustomerAddress).filter(
            CustomerAddress.customer_id == customer_id
        ).update({CustomerAddress.is_default: 0}, synchronize_session=False)

    address_count = (
        db.query(CustomerAddress)
        .filter(CustomerAddress.customer_id == customer_id)
        .count()
    )
    is_default = 1 if (body.is_default or address_count == 0) else 0

    address = CustomerAddress(
        id=address_id,
        customer_id=customer_id,
        label=body.label,
        is_default=is_default,
        name=body.name,
        company=body.company,
        line1=body.line1,
        line2=body.line2,
        city=body.city,
        state=body.state,
        postal_code=body.postal_code,
        country=body.country or "US",
        phone=body.phone,
        created_at=timestamp,
        updated_at=timestamp,
    )
    db.add(address)
    db.commit()
    db.refresh(address)

    return format_address(address)


@router.delete("/{customer_id}/addresses/{address_id}", response_model=DeletedOut)
async def delete_address(
    customer_id: str,
    address_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, bool]:
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise ApiError.not_found("Customer")

    address = (
        db.query(CustomerAddress)
        .filter(
            CustomerAddress.id == address_id,
            CustomerAddress.customer_id == customer_id,
        )
        .first()
    )
    if not address:
        raise ApiError.not_found("Address")

    was_default = bool(address.is_default)
    db.delete(address)
    db.commit()

    if was_default:
        next_address = (
            db.query(CustomerAddress)
            .filter(CustomerAddress.customer_id == customer_id)
            .first()
        )
        if next_address:
            next_address.is_default = 1
            db.commit()

    return {"deleted": True}
