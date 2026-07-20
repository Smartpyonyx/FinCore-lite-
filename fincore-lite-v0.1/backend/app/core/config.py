"""FinCore Lite v0.1 - Core Configuration"""
from pydantic_settings import BaseSettings
from functools import lru_cache

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
    SECRET_KEY: str = "fincore-super-secret-key-change-in-production-256-bits-min"
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
    MPESA_PASSKEY: str = ""
    MPESA_CONSUMER_KEY: str = ""
    MPESA_CONSUMER_SECRET: str = ""
    MPESA_CALLBACK_URL: str = "https://api.fincore.africa/v1/mpesa/callback"
    MPESA_IP_WHITELIST: list = ["196.201.214.0/24", "196.201.213.0/24"]

    # Exchange Rates
    EXCHANGE_RATE_PROVIDER: str = "open_exchange_rates"  # cbk | open_exchange_rates
    EXCHANGE_RATE_API_KEY: str = ""
    CRYPTO_RATE_PROVIDER: str = "coingecko"
    FIAT_RATE_REFRESH_HOURS: int = 24
    CRYPTO_RATE_REFRESH_MINUTES: int = 60

    # Rate Limiting
    RATE_LIMIT_LOGIN: int = 5
    RATE_LIMIT_WINDOW_SECONDS: int = 900  # 15 minutes

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
