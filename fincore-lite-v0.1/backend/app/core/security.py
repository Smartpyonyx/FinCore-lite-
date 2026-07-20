"""FinCore Lite v0.1 - Security Stack"""
from datetime import datetime, timedelta, timezone
from typing import Optional, Union
import uuid

from jose import JWTError, jwt
from passlib.context import CryptContext
import pyotp
import qrcode
import io
import base64
from PIL import Image

from app.core.config import get_settings

settings = get_settings()

# Argon2id password hashing (memory-hard, resistant to GPU attacks)
pwd_context = CryptContext(
    schemes=["argon2"],
    deprecated="auto",
    argon2__time_cost=settings.ARGON2_TIME_COST,
    argon2__memory_cost=settings.ARGON2_MEMORY_COST,
    argon2__parallelism=settings.ARGON2_PARALLELISM,
    argon2__type="ID",  # Argon2id
    argon2__hash_len=32,
    argon2__salt_len=16,
)

def hash_password(password: str) -> str:
    """Hash password using Argon2id."""
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password against Argon2id hash."""
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT access token (15 min expiry)."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access",
        "jti": str(uuid.uuid4())  # Unique token ID for revocation
    })

    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

def create_refresh_token(data: dict) -> str:
    """Create JWT refresh token (7 day expiry, rotates on use)."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    to_encode.update({
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "refresh",
        "jti": str(uuid.uuid4())
    })

    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

def decode_token(token: str) -> Optional[dict]:
    """Decode and validate JWT token."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except JWTError:
        return None

# MFA / TOTP
def generate_mfa_secret() -> str:
    """Generate new TOTP secret."""
    return pyotp.random_base32()

def get_totp_uri(secret: str, email: str) -> str:
    """Generate TOTP provisioning URI for QR code."""
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=settings.MFA_ISSUER)

def generate_qr_code(uri: str) -> str:
    """Generate base64 QR code image."""
    qr = qrcode.make(uri)
    buffer = io.BytesIO()
    qr.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()

def verify_totp(secret: str, token: str) -> bool:
    """Verify TOTP code."""
    totp = pyotp.TOTP(secret)
    return totp.verify(token, valid_window=1)  # Allow 1 window (30s) drift

def generate_backup_codes() -> list:
    """Generate 10 single-use backup codes."""
    import secrets
    return [secrets.token_hex(4).upper() for _ in range(10)]
