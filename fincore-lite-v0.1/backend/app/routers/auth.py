"""FinCore Lite v0.1 - Authentication Router"""
from fastapi import APIRouter, Depends, HTTPException, status, Request, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from datetime import datetime, timezone
from typing import Optional
import structlog

from app.core.database import get_db
from app.core.config import get_settings
from app.core.security import (
    hash_password, verify_password, create_access_token, create_refresh_token,
    decode_token, generate_mfa_secret, get_totp_uri, generate_qr_code,
    verify_totp, generate_backup_codes
)
from app.schemas import (
    Token, LoginRequest, MFAVerifyRequest, UserCreate, UserResponse,
    OrganisationCreate, OrganisationResponse
)
from app.models import User, Organisation, AuditLog, UserPreference

router = APIRouter(prefix="/auth", tags=["Authentication"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
logger = structlog.get_logger()
settings = get_settings()

# Rate limiting store (use Redis in production)
login_attempts = {}

def check_rate_limit(ip: str) -> bool:
    """Check if IP is rate limited."""
    now = datetime.now(timezone.utc).timestamp()
    attempts = login_attempts.get(ip, [])
    # Remove old attempts
    attempts = [a for a in attempts if now - a < settings.RATE_LIMIT_WINDOW_SECONDS]
    login_attempts[ip] = attempts
    return len(attempts) < settings.RATE_LIMIT_LOGIN

def record_login_attempt(ip: str):
    """Record a failed login attempt."""
    now = datetime.now(timezone.utc).timestamp()
    if ip not in login_attempts:
        login_attempts[ip] = []
    login_attempts[ip].append(now)

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> User:
    """Get current user from JWT token."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    payload = decode_token(token)
    if payload is None or payload.get("type") != "access":
        raise credentials_exception

    user_id = payload.get("sub")
    if user_id is None:
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == user_id, User.deleted_at.is_(None)))
    user = result.scalar_one_or_none()

    if user is None or user.status != "ACTIVE":
        raise credentials_exception

    return user

async def get_current_active_user(current_user: User = Depends(get_current_user)) -> User:
    """Ensure user is active."""
    if current_user.status != "ACTIVE":
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user

def require_role(roles: list):
    """Role-based access control dependency."""
    async def role_checker(current_user: User = Depends(get_current_active_user)):
        if current_user.role not in roles and current_user.role != "SUPER_ADMIN":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required role: {roles}"
            )
        return current_user
    return role_checker

@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    request: Request,
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db)
):
    """Register new user and organisation."""
    # Check if email exists
    result = await db.execute(select(User).where(User.email == user_data.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    # Create organisation
    org_slug = user_data.email.split("@")[0].lower().replace(".", "-")
    org = Organisation(
        name=user_data.organisation_name or f"{user_data.full_name}'s Business",
        slug=org_slug,
        functional_currency="KES",
        owner_id=None,  # Will update after user creation
    )
    db.add(org)
    await db.flush()  # Get org ID

    # Create user
    user = User(
        email=user_data.email,
        full_name=user_data.full_name,
        phone=user_data.phone,
        password_hash=hash_password(user_data.password),
        role="OWNER",
        organisation_id=org.id,
    )
    db.add(user)
    await db.flush()

    # Update org owner
    org.owner_id = user.id

    # Create default user preferences
    pref = UserPreference(
        user_id=user.id,
        theme="dark",
        zoom_level=1.00,
    )
    db.add(pref)

    # Create default chart of accounts
    await create_default_accounts(db, org.id, user.id)

    # Audit log
    audit = AuditLog(
        organisation_id=org.id,
        user_id=user.id,
        action="USER_REGISTERED",
        entity_type="User",
        entity_id=user.id,
        after_state={"email": user.email, "role": user.role},
        ip_address=request.client.host,
    )
    db.add(audit)

    await db.commit()
    await db.refresh(user)

    logger.info("user_registered", user_id=str(user.id), email=user.email)
    return user

@router.post("/login", response_model=Token)
async def login(
    request: Request,
    login_data: LoginRequest,
    db: AsyncSession = Depends(get_db)
):
    """Authenticate user and return tokens."""
    client_ip = request.client.host

    # Rate limiting
    if not check_rate_limit(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Try again in 15 minutes."
        )

    # Find user
    result = await db.execute(select(User).where(User.email == login_data.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(login_data.password, user.password_hash):
        record_login_attempt(client_ip)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Check if MFA required
    if user.mfa_enabled and settings.MFA_ENABLED_FOR_OWNER and user.role in ["OWNER", "ACCOUNTANT"]:
        if not login_data.mfa_code:
            # Return temp token for MFA verification
            temp_token = create_access_token(
                {"sub": str(user.id), "mfa_pending": True},
                expires_delta=timedelta(minutes=5)
            )
            return {"access_token": temp_token, "refresh_token": "", "token_type": "bearer", 
                    "expires_in": 300, "mfa_required": True}

        if not verify_totp(user.mfa_secret, login_data.mfa_code):
            record_login_attempt(client_ip)
            raise HTTPException(status_code=401, detail="Invalid MFA code")

    # Update last login
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    # Generate tokens
    access_token = create_access_token({
        "sub": str(user.id),
        "org_id": str(user.organisation_id) if user.organisation_id else None,
        "role": user.role,
        "email": user.email
    })
    refresh_token = create_refresh_token({"sub": str(user.id)})

    # Audit log
    audit = AuditLog(
        organisation_id=user.organisation_id,
        user_id=user.id,
        action="USER_LOGIN",
        entity_type="User",
        entity_id=user.id,
        ip_address=client_ip,
    )
    db.add(audit)
    await db.commit()

    logger.info("user_login", user_id=str(user.id), email=user.email)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "mfa_required": False
    }

@router.post("/mfa/verify", response_model=Token)
async def verify_mfa(
    request: Request,
    mfa_data: MFAVerifyRequest,
    db: AsyncSession = Depends(get_db)
):
    """Verify MFA code and return full tokens."""
    payload = decode_token(mfa_data.temp_token)
    if not payload or not payload.get("mfa_pending"):
        raise HTTPException(status_code=400, detail="Invalid MFA session")

    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user or not verify_totp(user.mfa_secret, mfa_data.mfa_code):
        raise HTTPException(status_code=401, detail="Invalid MFA code")

    access_token = create_access_token({
        "sub": str(user.id),
        "org_id": str(user.organisation_id) if user.organisation_id else None,
        "role": user.role,
        "email": user.email
    })
    refresh_token = create_refresh_token({"sub": str(user.id)})

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "mfa_required": False
    }

@router.post("/mfa/setup")
async def setup_mfa(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """Setup MFA for current user."""
    secret = generate_mfa_secret()
    uri = get_totp_uri(secret, current_user.email)
    qr_code = generate_qr_code(uri)
    backup_codes = generate_backup_codes()

    # Store secret (not enabled until verified)
    current_user.mfa_secret = secret
    await db.commit()

    return {
        "secret": secret,
        "qr_code": f"data:image/png;base64,{qr_code}",
        "backup_codes": backup_codes,
        "message": "Scan QR code with authenticator app and verify to enable MFA"
    }

@router.post("/refresh", response_model=Token)
async def refresh_token(
    refresh_token: str,
    db: AsyncSession = Depends(get_db)
):
    """Rotate refresh token and return new access token."""
    payload = decode_token(refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == user_id, User.status == "ACTIVE"))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # Rotate tokens
    new_access = create_access_token({
        "sub": str(user.id),
        "org_id": str(user.organisation_id) if user.organisation_id else None,
        "role": user.role,
        "email": user.email
    })
    new_refresh = create_refresh_token({"sub": str(user.id)})

    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "mfa_required": False
    }

@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_active_user)):
    """Get current user profile."""
    return current_user

@router.put("/me/preferences")
async def update_preferences(
    prefs: dict,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """Update user preferences (theme, zoom, etc)."""
    result = await db.execute(
        select(UserPreference).where(UserPreference.user_id == current_user.id)
    )
    user_pref = result.scalar_one_or_none()

    if not user_pref:
        user_pref = UserPreference(user_id=current_user.id)
        db.add(user_pref)

    # Update fields
    if "theme" in prefs:
        user_pref.theme = prefs["theme"]
        current_user.theme_preference = prefs["theme"]
    if "zoom_level" in prefs:
        user_pref.zoom_level = prefs["zoom_level"]
        current_user.zoom_level = prefs["zoom_level"]
    if "sidebar_collapsed" in prefs:
        user_pref.sidebar_collapsed = prefs["sidebar_collapsed"]
    if "currency_display" in prefs:
        user_pref.currency_display = prefs["currency_display"]
    if "date_format" in prefs:
        user_pref.date_format = prefs["date_format"]

    await db.commit()
    return {"success": True, "preferences": prefs}

async def create_default_accounts(db: AsyncSession, org_id: str, user_id: str):
    """Create default chart of accounts for new organisation."""
    default_accounts = [
        # Assets
        {"code": "1000", "name": "Cash / M-Pesa", "type": "ASSET", "balance": "DEBIT", "system": True},
        {"code": "1100", "name": "Accounts Receivable", "type": "ASSET", "balance": "DEBIT", "system": True},
        {"code": "1200", "name": "Inventory", "type": "ASSET", "balance": "DEBIT", "system": True},
        {"code": "1300", "name": "Equipment", "type": "ASSET", "balance": "DEBIT", "system": True},
        # Liabilities
        {"code": "2000", "name": "Accounts Payable", "type": "LIABILITY", "balance": "CREDIT", "system": True},
        {"code": "2100", "name": "Loans Payable", "type": "LIABILITY", "balance": "CREDIT", "system": True},
        # Equity
        {"code": "3000", "name": "Owner Capital", "type": "EQUITY", "balance": "CREDIT", "system": True},
        {"code": "3100", "name": "Owner Drawings", "type": "EQUITY", "balance": "DEBIT", "system": True},
        {"code": "3200", "name": "Retained Earnings", "type": "EQUITY", "balance": "CREDIT", "system": True},
        # Income
        {"code": "4000", "name": "Sales Revenue", "type": "INCOME", "balance": "CREDIT", "system": True},
        {"code": "4100", "name": "Service Income", "type": "INCOME", "balance": "CREDIT", "system": True},
        {"code": "4200", "name": "Rental Income", "type": "INCOME", "balance": "CREDIT", "system": True},
        {"code": "4300", "name": "Commission Income", "type": "INCOME", "balance": "CREDIT", "system": True},
        {"code": "4400", "name": "Interest Income", "type": "INCOME", "balance": "CREDIT", "system": True},
        {"code": "4500", "name": "Other Income", "type": "INCOME", "balance": "CREDIT", "system": True},
        # Expenses
        {"code": "5000", "name": "Cost of Goods Sold", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "5100", "name": "Salaries and Wages", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "5200", "name": "Rent and Rates", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "5300", "name": "Utilities", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "5400", "name": "Communication", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "5500", "name": "Transport and Fuel", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "5600", "name": "Marketing", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "5700", "name": "Bank Charges", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "5800", "name": "Supplier Payments", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "5900", "name": "Tax and Licences", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "6000", "name": "Professional Fees", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "6100", "name": "Office Supplies", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "6200", "name": "Repairs and Maintenance", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "6300", "name": "Insurance", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "6400", "name": "Depreciation", "type": "EXPENSE", "balance": "DEBIT", "system": True},
        {"code": "6600", "name": "Other Expenses", "type": "EXPENSE", "balance": "DEBIT", "system": True},
    ]

    from app.models import Account
    for acc in default_accounts:
        account = Account(
            organisation_id=org_id,
            code=acc["code"],
            name=acc["name"],
            account_type=acc["type"],
            normal_balance=acc["balance"],
            is_system=acc["system"],
            created_by=user_id,
        )
        db.add(account)
