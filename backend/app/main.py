"""Merchant Commerce API — Python FastAPI (RAILS AI aligned)."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import APP_VERSION, cors_origins_list, get_settings, is_production
from app.db.models import init_db
from app.domain.errors import ApiError
from app.lib.rate_limit import rate_limit_middleware
from app.routers import (
    catalog,
    checkout,
    customers,
    discounts,
    images,
    inventory,
    mcp,
    oauth,
    orders,
    setup,
    ucp,
    webhooks,
)
from app.workers.scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    Path(settings.storage_root).mkdir(parents=True, exist_ok=True)
    Path(settings.storage_root, "images").mkdir(parents=True, exist_ok=True)
    init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(
    title="Merchant API",
    description="Open-source commerce backend for Stripe — RAILS AI agent-ready (UCP + MCP)",
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url=None if is_production() else "/docs",
    redoc_url=None if is_production() else "/redoc",
    openapi_url=None if is_production() else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def apply_rate_limit(request: Request, call_next):
    return await rate_limit_middleware(request, call_next)


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status_code, content=exc.detail)


@app.get("/")
def root():
    return {"name": "merchant", "version": APP_VERSION, "ok": True, "runtime": "python"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "merchant-python", "version": APP_VERSION}


# REST API v1 (routers with embedded /v1/* prefix mount without extra prefix)
app.include_router(setup.router, prefix="/v1/setup")
app.include_router(catalog.router)
app.include_router(inventory.router)
app.include_router(checkout.router, prefix="/v1/carts")
app.include_router(orders.router, prefix="/v1/orders")
app.include_router(customers.router, prefix="/v1/customers")
app.include_router(webhooks.router)
app.include_router(images.router)
app.include_router(discounts.router)

# OAuth + UCP + RAILS MCP
app.include_router(oauth.root_router)
app.include_router(oauth.router)
app.include_router(ucp.router)
app.include_router(mcp.root_router)
app.include_router(mcp.router)
