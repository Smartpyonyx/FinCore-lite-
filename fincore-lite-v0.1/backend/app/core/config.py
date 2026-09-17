"""FinCore Lite v0.1 - Core Configuration"""
from typing import List
from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache
import json


def parse_json_list(value: str) -> List[str]:
    """Parse JSON array string to list."""
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        # Fallback: split by comma
        return [v.strip() for v in value.split(",") if v.strip()]


class Settings(BaseSettings):
    # App
    APP_NAME: str = "FinCore Lite"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://fincore:fincore_secret@db:5432/fincore_lite"
    DATABASE_POOL_SIZE: int = 20
    DATABASE_MAX_OVERFLOW: int = 30

    # Redis (caching + sessions)
    REDIS_URL: str = "redis://redis:6379/0"
    CACHE_TTL: int = 300  # 5 minutes

    # Security
    SECRET_KEY: str = Field(..., description="Must be set in .env - 256 bits minimum")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Password hashing (Argon2id)
    ARGON2_TIME_COST: int = 3
    ARGON2_MEMORY_COST: int = 65536  # 64MB
    ARGON2_PARALLELISM: int = 4

    # MFA
    MFA_ISSUER: str = "FinCore Lite"
    MFA_ENABLED_FOR_OWNER: bool = True
    MFA_ENABLED_FOR_ACCOUNTANT: bool = True

    # M-Pesa Daraja
    MPESA_ENV: str = "sandbox"  # sandbox | production
    MPESA_SHORTCODE: str = "174379"
    MPESA_PASSKEY: str = Field(default="", description="Set in .env for production")
    MPESA_CONSUMER_KEY: str = Field(default="", description="Set in .env for production")
    MPESA_CONSUMER_SECRET: str = Field(default="", description="Set in .env for production")
    MPESA_CALLBACK_URL: str = "https://api.fincore.africa/v1/mpesa/callback"
    MPESA_IP_WHITELIST: List[str] = Field(
        default_factory=lambda: ["196.201.214.0/24", "196.201.213.0/24"]
    )

    # Exchange Rates
    EXCHANGE_RATE_PROVIDER: str = "open_exchange_rates"  # cbk | open_exchange_rates
    EXCHANGE_RATE_API_KEY: str = Field(default="", description="Set in .env for production")
    CRYPTO_RATE_PROVIDER: str = "coingecko"
    FIAT_RATE_REFRESH_HOURS: int = 24
    CRYPTO_RATE_REFRESH_MINUTES: int = 60

    # Rate Limiting
    RATE_LIMIT_LOGIN: int = 5
    RATE_LIMIT_WINDOW_SECONDS: int = 900  # 15 minutes

    # CORS
    CORS_ORIGINS: List[str] = Field(
        default_factory=lambda: ["https://app.fincore.africa", "http://localhost:3000", "http://localhost:8080"]
    )

    # Trusted Hosts
    TRUSTED_HOSTS: List[str] = Field(
        default_factory=lambda: ["*.fincore.africa", "localhost", "127.0.0.1"]
    )

    # Audit
    AUDIT_LOG_RETENTION_DAYS: int = 2555  # 7 years

    # Performance
    MAX_CONNECTIONS: int = 1000
    WORKERS: int = 4

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()
