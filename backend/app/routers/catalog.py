from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models import Inventory, OrderItem, Product, Variant
from app.db.session import get_db
from app.deps.auth import get_auth_context, require_admin
from app.domain.errors import ApiError
from app.domain.utils import now_iso, uuid4
from app.lib.crypto import AuthContext

router = APIRouter(prefix="/v1/products", tags=["Products"])


class CreateProductBody(BaseModel):
    title: str = Field(min_length=1)
    description: str | None = None


class UpdateProductBody(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    status: Literal["active", "draft"] | None = None


class CreateVariantBody(BaseModel):
    sku: str = Field(min_length=1)
    title: str = Field(min_length=1)
    price_cents: int = Field(ge=0)
    image_url: str | None = None


class UpdateVariantBody(BaseModel):
    sku: str | None = Field(default=None, min_length=1)
    title: str | None = Field(default=None, min_length=1)
    price_cents: int | None = Field(default=None, ge=0)
    image_url: str | None = None


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


def _variants_for_products(db: Session, product_ids: list[str]) -> dict[str, list[Variant]]:
    if not product_ids:
        return {}

    variants = (
        db.query(Variant)
        .filter(Variant.product_id.in_(product_ids))
        .order_by(Variant.created_at.asc())
        .all()
    )
    by_product: dict[str, list[Variant]] = {}
    for variant in variants:
        by_product.setdefault(variant.product_id, []).append(variant)
    return by_product


@router.get("")
def list_products(
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(get_auth_context),
    limit: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    status: Literal["active", "draft"] | None = Query(default=None),
):
    page_limit = min(int(limit or "20"), 100)

    query = db.query(Product)
    if status:
        query = query.filter(Product.status == status)
    if cursor:
        query = query.filter(Product.created_at < cursor)

    products = query.order_by(Product.created_at.desc()).limit(page_limit + 1).all()
    has_more = len(products) > page_limit
    if has_more:
        products = products[:page_limit]

    variants_by_product = _variants_for_products(db, [p.id for p in products])
    items = [_product_dict(p, variants_by_product.get(p.id, [])) for p in products]
    next_cursor = items[-1]["created_at"] if has_more and items else None

    return {"items": items, "pagination": {"has_more": has_more, "next_cursor": next_cursor}}


@router.get("/{id}")
def get_product(
    id: str,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(get_auth_context),
):
    product = db.get(Product, id)
    if not product:
        raise ApiError.not_found("Product not found")

    variants = (
        db.query(Variant)
        .filter(Variant.product_id == id)
        .order_by(Variant.created_at.asc())
        .all()
    )
    return _product_dict(product, variants)


@router.post("", status_code=201)
def create_product(
    body: CreateProductBody,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
):
    product_id = uuid4()
    timestamp = now_iso()
    description = body.description or None

    product = Product(
        id=product_id,
        title=body.title,
        description=description,
        status="active",
        created_at=timestamp,
    )
    db.add(product)
    db.commit()

    return {
        "id": product_id,
        "title": body.title,
        "description": description,
        "status": "active",
        "created_at": timestamp,
        "variants": [],
    }


@router.patch("/{id}")
def update_product(
    id: str,
    body: UpdateProductBody,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
):
    product = db.get(Product, id)
    if not product:
        raise ApiError.not_found("Product not found")

    if body.title is not None:
        product.title = body.title
    if body.description is not None:
        product.description = body.description
    if body.status is not None:
        product.status = body.status

    db.commit()
    db.refresh(product)

    variants = db.query(Variant).filter(Variant.product_id == id).all()
    return _product_dict(product, variants)


@router.delete("/{id}")
def delete_product(
    id: str,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
):
    product = db.get(Product, id)
    if not product:
        raise ApiError.not_found("Product not found")

    variants = db.query(Variant).filter(Variant.product_id == id).all()
    if variants:
        skus = [v.sku for v in variants]
        order_item = db.query(OrderItem).filter(OrderItem.sku.in_(skus)).first()
        if order_item:
            raise ApiError.conflict(
                "Cannot delete product with variants that have been ordered. Set status to draft instead."
            )

    for variant in variants:
        db.query(Inventory).filter(Inventory.sku == variant.sku).delete()

    db.query(Variant).filter(Variant.product_id == id).delete()
    db.delete(product)
    db.commit()

    return {"deleted": True}


@router.post("/{id}/variants", status_code=201)
def create_variant(
    id: str,
    body: CreateVariantBody,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
):
    product = db.get(Product, id)
    if not product:
        raise ApiError.not_found("Product not found")

    existing_sku = db.query(Variant).filter(Variant.sku == body.sku).first()
    if existing_sku:
        raise ApiError.conflict(f"SKU {body.sku} already exists")

    variant_id = uuid4()
    timestamp = now_iso()
    image_url = body.image_url or None

    variant = Variant(
        id=variant_id,
        product_id=id,
        sku=body.sku,
        title=body.title,
        price_cents=body.price_cents,
        weight_g=0,
        image_url=image_url,
        created_at=timestamp,
    )
    inventory = Inventory(
        id=uuid4(),
        sku=body.sku,
        on_hand=0,
        reserved=0,
        updated_at=timestamp,
    )
    db.add(variant)
    db.add(inventory)
    db.commit()

    return {
        "id": variant_id,
        "sku": body.sku,
        "title": body.title,
        "price_cents": body.price_cents,
        "image_url": image_url,
    }


@router.patch("/{id}/variants/{variant_id}")
def update_variant(
    id: str,
    variant_id: str,
    body: UpdateVariantBody,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
):
    variant = (
        db.query(Variant)
        .filter(Variant.id == variant_id, Variant.product_id == id)
        .first()
    )
    if not variant:
        raise ApiError.not_found("Variant not found")

    if body.sku is not None:
        existing_sku = (
            db.query(Variant)
            .filter(Variant.sku == body.sku, Variant.id != variant_id)
            .first()
        )
        if existing_sku:
            raise ApiError.conflict(f"SKU {body.sku} already exists")

        inventory = db.query(Inventory).filter(Inventory.sku == variant.sku).first()
        if inventory:
            inventory.sku = body.sku
        variant.sku = body.sku

    if body.title is not None:
        variant.title = body.title
    if body.price_cents is not None:
        variant.price_cents = body.price_cents
    if body.image_url is not None:
        variant.image_url = body.image_url

    db.commit()
    db.refresh(variant)

    return _variant_dict(variant)


@router.delete("/{id}/variants/{variant_id}")
def delete_variant(
    id: str,
    variant_id: str,
    db: Session = Depends(get_db),
    _auth: AuthContext = Depends(require_admin),
):
    variant = (
        db.query(Variant)
        .filter(Variant.id == variant_id, Variant.product_id == id)
        .first()
    )
    if not variant:
        raise ApiError.not_found("Variant not found")

    order_item = db.query(OrderItem).filter(OrderItem.sku == variant.sku).first()
    if order_item:
        raise ApiError.conflict(
            "Cannot delete variant that has been ordered. Set product status to draft instead."
        )

    db.query(Inventory).filter(Inventory.sku == variant.sku).delete()
    db.delete(variant)
    db.commit()

    return {"deleted": True}
