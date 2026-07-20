"""FinCore Lite v0.1 - Pydantic Schemas"""
from pydantic import BaseModel, Field, EmailStr, validator
from typing import Optional, List, Dict, Any
from decimal import Decimal
from datetime import datetime, date
from uuid import UUID

# ============== AUTH ==============
class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 900  # 15 minutes
    mfa_required: bool = False

class TokenPayload(BaseModel):
    sub: str  # user_id
    org_id: Optional[str] = None
    role: str
    exp: datetime
    type: str
    jti: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    mfa_code: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "email": "user@fincore.africa",
                "password": "secure_password",
                "mfa_code": "123456"
            }
        }

class MFAVerifyRequest(BaseModel):
    temp_token: str
    mfa_code: str

# ============== USERS ==============
class UserBase(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=2, max_length=256)
    phone: Optional[str] = None

class UserCreate(UserBase):
    password: str = Field(..., min_length=12, max_length=128)
    role: str = "OWNER"
    organisation_name: Optional[str] = None

    @validator("password")
    def validate_password(cls, v):
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        if not any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in v):
            raise ValueError("Password must contain at least one special character")
        return v

class UserResponse(UserBase):
    id: UUID
    role: str
    mfa_enabled: bool
    theme_preference: str = "dark"
    zoom_level: Decimal = Decimal("1.00")
    last_login_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True

class UserPreferenceUpdate(BaseModel):
    theme: Optional[str] = None
    zoom_level: Optional[Decimal] = Field(None, ge=0.5, le=3.0)
    sidebar_collapsed: Optional[bool] = None
    currency_display: Optional[str] = None
    date_format: Optional[str] = None

# ============== ORGANISATIONS ==============
class OrganisationBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=256)
    functional_currency: str = "KES"
    timezone: str = "Africa/Nairobi"
    fiscal_year_start: str = "01-01"

class OrganisationCreate(OrganisationBase):
    pass

class OrganisationResponse(OrganisationBase):
    id: UUID
    slug: str
    plan: str
    status: str
    mpesa_shortcode: Optional[str]
    mpesa_paybill: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True

# ============== ACCOUNTS (Chart of Accounts) ==============
class AccountBase(BaseModel):
    code: str = Field(..., max_length=16)
    name: str = Field(..., min_length=2, max_length=256)
    account_type: str = Field(..., pattern="^(ASSET|LIABILITY|EQUITY|INCOME|EXPENSE)$")
    normal_balance: str = Field(..., pattern="^(DEBIT|CREDIT)$")
    parent_id: Optional[UUID] = None
    currency: Optional[str] = None
    description: Optional[str] = None

class AccountCreate(AccountBase):
    pass

class AccountResponse(AccountBase):
    id: UUID
    is_system: bool
    is_postable: bool
    organisation_id: UUID
    created_at: datetime

    class Config:
        from_attributes = True

# ============== JOURNAL ENTRIES ==============
class JournalLineCreate(BaseModel):
    account_id: UUID
    line_type: str = Field(..., pattern="^(DEBIT|CREDIT)$")
    amount: Decimal = Field(..., gt=0)
    description: Optional[str] = None
    tags: Optional[List[str]] = None

class JournalEntryCreate(BaseModel):
    posting_date: date
    narration: str = Field(..., min_length=5, max_length=1000)
    currency: str = "KES"
    exchange_rate: Optional[Decimal] = Decimal("1.0")
    lines: List[JournalLineCreate] = Field(..., min_length=2)
    source_document: Optional[str] = None

    @validator("lines")
    def validate_balanced(cls, v):
        total_debit = sum(line.amount for line in v if line.line_type == "DEBIT")
        total_credit = sum(line.amount for line in v if line.line_type == "CREDIT")
        if total_debit != total_credit:
            raise ValueError(f"Journal must balance: Dr {total_debit} != Cr {total_credit}")
        return v

class JournalEntryResponse(BaseModel):
    id: UUID
    reference: str
    journal_type: str
    status: str
    posting_date: date
    posting_period: str
    narration: str
    currency: str
    total_debit_kes: Decimal
    total_credit_kes: Decimal
    posted_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True

# ============== M-PESA ==============
class MpesaCallback(BaseModel):
    TransactionType: str
    TransID: str
    TransTime: str
    TransAmount: str
    BusinessShortCode: str
    BillRefNumber: str
    InvoiceNumber: Optional[str] = None
    OrgAccountBalance: str
    ThirdPartyTransID: Optional[str] = None
    MSISDN: str
    FirstName: Optional[str] = None
    MiddleName: Optional[str] = None
    LastName: Optional[str] = None

class MpesaCategoriseRequest(BaseModel):
    mpesa_transaction_id: UUID
    category_id: UUID
    description: Optional[str] = None

class MpesaTransactionResponse(BaseModel):
    id: UUID
    mpesa_reference: str
    transaction_type: str
    direction: str
    amount: Decimal
    charge: Decimal
    phone_number: Optional[str]
    status: str
    transaction_date: datetime
    category_name: Optional[str] = None

    class Config:
        from_attributes = True

# ============== REPORTS ==============
class PnLReport(BaseModel):
    period: str
    income: Dict[str, Decimal]
    expenses: Dict[str, Decimal]
    total_income: Decimal
    total_expenses: Decimal
    net_profit: Decimal
    margin_percent: Decimal

class BalanceSheetReport(BaseModel):
    as_at: date
    assets: Dict[str, Decimal]
    liabilities: Dict[str, Decimal]
    equity: Dict[str, Decimal]
    total_assets: Decimal
    total_liabilities: Decimal
    total_equity: Decimal
    balanced: bool

class DashboardKPIs(BaseModel):
    total_revenue_mtd: Decimal
    total_expenses_mtd: Decimal
    net_profit_mtd: Decimal
    mpesa_charges_mtd: Decimal
    revenue_change_percent: float
    expense_change_percent: float
    profit_margin_percent: float
    uncategorised_count: int

class CashFlowData(BaseModel):
    labels: List[str]
    money_in: List[Decimal]
    money_out: List[Decimal]
    net: List[Decimal]

class ExpenseBreakdown(BaseModel):
    labels: List[str]
    values: List[Decimal]
    percentages: List[float]
    colors: List[str]

# ============== AUDIT ==============
class AuditLogResponse(BaseModel):
    id: UUID
    action: str
    entity_type: str
    entity_id: Optional[UUID]
    ip_address: Optional[str]
    timestamp: datetime
    user_email: Optional[str] = None

    class Config:
        from_attributes = True

# ============== API RESPONSE ==============
class APIResponse(BaseModel):
    success: bool = True
    message: str = "Success"
    data: Optional[Any] = None
    meta: Optional[Dict[str, Any]] = None
