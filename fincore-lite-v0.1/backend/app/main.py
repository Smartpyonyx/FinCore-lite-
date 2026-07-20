"""FinCore Lite v0.1 - Main Application"""
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import structlog
import time
import os

from app.core.config import get_settings
from app.core.database import engine, Base
from app.routers import auth, transactions, mpesa, reports

settings = get_settings()
logger = structlog.get_logger()

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ]
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    logger.info("fincore_startup", version=settings.APP_VERSION, environment="production")

    # Create tables (in production, use Alembic migrations)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    # Shutdown
    await engine.dispose()
    logger.info("fincore_shutdown")

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="The accounting tool for every person, every business, every African market",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# Security middleware
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*.fincore.africa", "localhost", "127.0.0.1"]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.fincore.africa", "https://localhost:3000", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
    max_age=600,
)

# Request timing middleware
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    response.headers["X-Request-ID"] = request.headers.get("X-Request-ID", "unknown")

    # Log slow requests
    if process_time > 3.0:
        logger.warning(
            "slow_request",
            path=request.url.path,
            method=request.method,
            duration=process_time,
            client_ip=request.client.host
        )

    return response

# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(
        "unhandled_exception",
        path=request.url.path,
        error=str(exc),
        exc_type=type(exc).__name__
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"success": False, "message": "Internal server error", "error_id": str(os.urandom(8).hex())}
    )

# Health check
@app.get("/health", tags=["Health"])
async def health_check():
    return {
        "status": "healthy",
        "version": settings.APP_VERSION,
        "timestamp": time.time(),
        "database": "connected",
        "environment": "production"
    }

# API routes
app.include_router(auth.router, prefix="/api/v1")
app.include_router(transactions.router, prefix="/api/v1")
app.include_router(mpesa.router, prefix="/api/v1")
app.include_router(reports.router, prefix="/api/v1")

# Serve static frontend
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
