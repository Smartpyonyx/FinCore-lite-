"""FinCore Lite v0.1 - Database Models"""
from sqlalchemy import (
    Column, String, Integer, DateTime, Date, Boolean, Numeric, 
    Text, ForeignKey, JSON, ARRAY, UniqueConstraint, Index, event
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func
import uuid
from app.core.database import Base

# Standard columns mixin for every table
class StandardColumns:
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)  # Soft delete
    status = Column(String(32), nullable=False, default="ACTIVE")
    version = Column(Integer, nullable=False, default=1)  # Optimistic locking

class Organisation(Base, StandardColumns):
    __tablename__ = "organisations"

    name = Column(String(256), nullable=False)
    slug = Column(String(128), unique=True, nullable=False)
    functional_currency = Column(String(3), nullable=False, default="KES")
    timezone = Column(String(64), nullable=False, default="Africa/Nairobi")
    fiscal_year_start = Column(String(5), nullable=False, default="01-01")
    plan = Column(String(32), nullable=False, default="LITE")
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    mpesa_shortcode = Column(String(16))
    mpesa_paybill = Column(String(16))

    # Indexes for performance
    __table_args__ = (
        Index("idx_org_slug", "slug"),
        Index("idx_org_owner", "owner_id"),
        Index("idx_org_status", "status"),
    )

class User(Base, StandardColumns):
    __tablename__ = "users"

    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=True)
    email = Column(String(256), unique=True, nullable=False)
    phone = Column(String(20))
    full_name = Column(String(256), nullable=False)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(64), nullable=False)  # SUPER_ADMIN|SUPPORT|OWNER|ACCOUNTANT|STAFF|VIEWER|CUSTOM
    custom_role_id = Column(UUID(as_uuid=True), ForeignKey("custom_roles.id"), nullable=True)
    mfa_enabled = Column(Boolean, nullable=False, default=False)
    mfa_secret = Column(String(64))
    last_login_at = Column(DateTime(timezone=True))
    theme_preference = Column(String(16), default="dark")  # dark | light | system
    zoom_level = Column(Numeric(3, 2), default=1.00)

    __table_args__ = (
        Index("idx_user_email", "email"),
        Index("idx_user_org", "organisation_id"),
        Index("idx_user_role", "role"),
    )

class CustomRole(Base, StandardColumns):
    __tablename__ = "custom_roles"

    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    name = Column(String(64), nullable=False)
    permissions = Column(JSONB, default=dict)  # JSON permission matrix

    __table_args__ = (
        UniqueConstraint("organisation_id", "name"),
    )

class Account(Base, StandardColumns):
    __tablename__ = "accounts"

    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    code = Column(String(16), nullable=False)
    name = Column(String(256), nullable=False)
    account_type = Column(String(32), nullable=False)  # ASSET|LIABILITY|EQUITY|INCOME|EXPENSE
    normal_balance = Column(String(8), nullable=False)  # DEBIT|CREDIT
    parent_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=True)
    is_system = Column(Boolean, nullable=False, default=False)
    is_postable = Column(Boolean, nullable=False, default=True)
    currency = Column(String(3))
    description = Column(Text)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    updated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))

    __table_args__ = (
        UniqueConstraint("organisation_id", "code"),
        Index("idx_account_org", "organisation_id"),
        Index("idx_account_type", "account_type"),
    )

class JournalEntry(Base, StandardColumns):
    __tablename__ = "journal_entries"

    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    reference = Column(String(64), unique=True, nullable=False)
    journal_type = Column(String(64), nullable=False)  # MANUAL|MPESA|BANK_IMPORT|DEPRECIATION|FX_REVALUATION|REVERSAL|SYSTEM
    posting_date = Column(Date, nullable=False)
    posting_period = Column(String(7), nullable=False)  # YYYY-MM
    narration = Column(Text, nullable=False)
    source_document = Column(String(64))
    source_type = Column(String(32))
    currency = Column(String(3), nullable=False)
    exchange_rate = Column(Numeric(18, 8), nullable=False, default=1)
    total_debit_kes = Column(Numeric(20, 4), nullable=False, default=0)
    total_credit_kes = Column(Numeric(20, 4), nullable=False, default=0)
    reversed_by = Column(UUID(as_uuid=True), ForeignKey("journal_entries.id"), nullable=True)
    reversal_of = Column(UUID(as_uuid=True), ForeignKey("journal_entries.id"), nullable=True)
    posted_at = Column(DateTime(timezone=True))
    posted_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    updated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))

    __table_args__ = (
        Index("idx_je_org", "organisation_id"),
        Index("idx_je_period", "posting_period"),
        Index("idx_je_date", "posting_date"),
        Index("idx_je_type", "journal_type"),
    )

class JournalLine(Base):
    __tablename__ = "journal_lines"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    journal_entry_id = Column(UUID(as_uuid=True), ForeignKey("journal_entries.id"), nullable=False)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    line_type = Column(String(8), nullable=False)  # DEBIT|CREDIT
    amount_original = Column(Numeric(20, 4), nullable=False)
    currency = Column(String(3), nullable=False)
    exchange_rate = Column(Numeric(18, 8), nullable=False, default=1)
    amount_kes = Column(Numeric(20, 4), nullable=False)
    description = Column(Text)
    tags = Column(ARRAY(String))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))

    __table_args__ = (
        Index("idx_jl_entry", "journal_entry_id"),
        Index("idx_jl_account", "account_id"),
        Index("idx_jl_org", "organisation_id"),
    )

class MpesaTransaction(Base, StandardColumns):
    __tablename__ = "mpesa_transactions"

    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=False)
    mpesa_reference = Column(String(32), unique=True, nullable=False)
    transaction_type = Column(String(32), nullable=False)  # TILL|PAYBILL|B2C|B2B|REVERSAL|AIRTIME
    direction = Column(String(8), nullable=False)  # IN|OUT
    amount = Column(Numeric(20, 4), nullable=False)
    charge = Column(Numeric(20, 4), nullable=False, default=0)
    phone_number = Column(String(20))
    account_reference = Column(String(64))
    description = Column(Text)
    transaction_date = Column(DateTime(timezone=True), nullable=False)
    journal_entry_id = Column(UUID(as_uuid=True), ForeignKey("journal_entries.id"), nullable=True)
    category_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=True)
    categorised_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    categorised_at = Column(DateTime(timezone=True))
    raw_payload = Column(JSONB)

    __table_args__ = (
        Index("idx_mpesa_org", "organisation_id"),
        Index("idx_mpesa_ref", "mpesa_reference"),
        Index("idx_mpesa_status", "status"),
        Index("idx_mpesa_date", "transaction_date"),
    )

class ExchangeRate(Base):
    __tablename__ = "exchange_rates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_currency = Column(String(3), nullable=False)
    to_currency = Column(String(3), nullable=False, default="KES")
    rate = Column(Numeric(18, 8), nullable=False)
    rate_date = Column(Date, nullable=False)
    source = Column(String(32), nullable=False)  # CBK|OPEN_EXCHANGE|COINGECKO|MANUAL
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("from_currency", "to_currency", "rate_date", "source"),
        Index("idx_rate_lookup", "from_currency", "to_currency", "rate_date"),
    )

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organisation_id = Column(UUID(as_uuid=True), ForeignKey("organisations.id"), nullable=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    action = Column(String(64), nullable=False)
    entity_type = Column(String(64), nullable=False)
    entity_id = Column(UUID(as_uuid=True))
    before_state = Column(JSONB)
    after_state = Column(JSONB)
    ip_address = Column(String(45))
    user_agent = Column(Text)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("idx_audit_org", "organisation_id"),
        Index("idx_audit_user", "user_id"),
        Index("idx_audit_timestamp", "timestamp"),
        Index("idx_audit_entity", "entity_type", "entity_id"),
    )

class UserPreference(Base, StandardColumns):
    __tablename__ = "user_preferences"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True)
    theme = Column(String(16), default="dark")  # dark | light | system
    zoom_level = Column(Numeric(3, 2), default=1.00)
    sidebar_collapsed = Column(Boolean, default=False)
    dashboard_layout = Column(JSONB, default=dict)
    notification_settings = Column(JSONB, default=dict)
    currency_display = Column(String(3), default="KES")
    date_format = Column(String(16), default="DD/MM/YYYY")

    __table_args__ = (
        Index("idx_pref_user", "user_id"),
    )

# Row-level security event listeners
@event.listens_for(Organisation, "before_insert")
def set_org_timestamps(mapper, connection, target):
    target.created_at = func.now()
    target.updated_at = func.now()
