"""FinCore Lite v0.1 - Transactions Router"""
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, text
from sqlalchemy.orm import selectinload
from datetime import datetime, timezone, date
from decimal import Decimal
from typing import List
import uuid
import structlog

from app.core.database import get_db
from app.core.config import get_settings
from app.routers.auth import get_current_active_user, require_role
from app.schemas import (
    JournalEntryCreate, JournalEntryResponse, JournalLineCreate,
    APIResponse, DashboardKPIs, CashFlowData, ExpenseBreakdown
)
from app.models import (
    User, JournalEntry, JournalLine, Account, AuditLog, ExchangeRate
)

router = APIRouter(prefix="/transactions", tags=["Transactions"])
logger = structlog.get_logger()
settings = get_settings()

async def generate_reference(db: AsyncSession, org_id: str) -> str:
    """Generate unique journal entry reference: JE-YYYY-XXXX."""
    today = date.today()
    year = today.year

    # Count entries this year for this org
    result = await db.execute(
        select(func.count(JournalEntry.id)).where(
            JournalEntry.organisation_id == org_id,
            JournalEntry.posting_period.like(f"{year}-%")
        )
    )
    count = result.scalar() + 1
    return f"JE-{year}-{count:04d}"

async def get_exchange_rate(
    db: AsyncSession,
    from_currency: str,
    to_currency: str = "KES",
    rate_date: date = None
) -> Decimal:
    """Get exchange rate for currency conversion."""
    if from_currency == to_currency:
        return Decimal("1.0")

    if rate_date is None:
        rate_date = date.today()

    result = await db.execute(
        select(ExchangeRate).where(
            ExchangeRate.from_currency == from_currency,
            ExchangeRate.to_currency == to_currency,
            ExchangeRate.rate_date == rate_date
        ).order_by(ExchangeRate.created_at.desc())
    )
    rate = result.scalar_one_or_none()

    if rate:
        return rate.rate

    # Fallback: use most recent rate
    result = await db.execute(
        select(ExchangeRate).where(
            ExchangeRate.from_currency == from_currency,
            ExchangeRate.to_currency == to_currency
        ).order_by(ExchangeRate.rate_date.desc())
    )
    rate = result.scalar_one_or_none()

    if rate:
        return rate.rate

    # Default fallback rates
    fallback_rates = {
        ("USD", "KES"): Decimal("129.50"),
        ("EUR", "KES"): Decimal("140.20"),
        ("GBP", "KES"): Decimal("165.80"),
        ("UGX", "KES"): Decimal("0.035"),
        ("TZS", "KES"): Decimal("0.052"),
        ("NGN", "KES"): Decimal("0.082"),
        ("GHS", "KES"): Decimal("11.20"),
        ("BTC", "KES"): Decimal("8_450_000.00"),
        ("USDT", "KES"): Decimal("129.50"),
    }

    return fallback_rates.get((from_currency, to_currency), Decimal("1.0"))

@router.post("/journal", response_model=JournalEntryResponse, status_code=status.HTTP_201_CREATED)
async def create_journal_entry(
    request: Request,
    entry: JournalEntryCreate,
    current_user: User = Depends(require_role(["OWNER", "ACCOUNTANT", "STAFF"])),
    db: AsyncSession = Depends(get_db)
):
    """Create and post a journal entry to the General Ledger."""

    # Validate accounts exist and belong to org
    for line in entry.lines:
        result = await db.execute(
            select(Account).where(
                Account.id == line.account_id,
                Account.organisation_id == current_user.organisation_id,
                Account.status == "ACTIVE",
                Account.is_postable == True
            )
        )
        account = result.scalar_one_or_none()
        if not account:
            raise HTTPException(
                status_code=400,
                detail=f"Account {line.account_id} not found or not postable"
            )

    # Get exchange rate
    exchange_rate = await get_exchange_rate(db, entry.currency, "KES", entry.posting_date)

    # Generate reference
    reference = await generate_reference(db, str(current_user.organisation_id))

    # Create journal entry
    journal = JournalEntry(
        organisation_id=current_user.organisation_id,
        reference=reference,
        journal_type="MANUAL",
        posting_date=entry.posting_date,
        posting_period=entry.posting_date.strftime("%Y-%m"),
        narration=entry.narration,
        source_document=entry.source_document,
        source_type="MANUAL",
        currency=entry.currency,
        exchange_rate=exchange_rate,
        total_debit_kes=sum(
            line.amount * exchange_rate for line in entry.lines if line.line_type == "DEBIT"
        ),
        total_credit_kes=sum(
            line.amount * exchange_rate for line in entry.lines if line.line_type == "CREDIT"
        ),
        posted_at=datetime.now(timezone.utc),
        posted_by=current_user.id,
        created_by=current_user.id,
        status="POSTED"
    )
    db.add(journal)
    await db.flush()

    # Create journal lines
    for line in entry.lines:
        amount_kes = line.amount * exchange_rate
        journal_line = JournalLine(
            organisation_id=current_user.organisation_id,
            journal_entry_id=journal.id,
            account_id=line.account_id,
            line_type=line.line_type,
            amount_original=line.amount,
            currency=entry.currency,
            exchange_rate=exchange_rate,
            amount_kes=amount_kes,
            description=line.description,
            tags=line.tags or [],
            created_by=current_user.id
        )
        db.add(journal_line)

    # Audit log
    audit = AuditLog(
        organisation_id=current_user.organisation_id,
        user_id=current_user.id,
        action="JOURNAL_POSTED",
        entity_type="JournalEntry",
        entity_id=journal.id,
        after_state={
            "reference": reference,
            "total_debit": str(journal.total_debit_kes),
            "total_credit": str(journal.total_credit_kes),
            "currency": entry.currency
        },
        ip_address=request.client.host,
    )
    db.add(audit)

    await db.commit()
    await db.refresh(journal)

    logger.info(
        "journal_posted",
        journal_id=str(journal.id),
        reference=reference,
        org_id=str(current_user.organisation_id)
    )

    return journal

@router.get("/journal", response_model=List[JournalEntryResponse])
async def list_journal_entries(
    skip: int = 0,
    limit: int = 100,
    period: str = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """List journal entries with pagination."""
    query = select(JournalEntry).where(
        JournalEntry.organisation_id == current_user.organisation_id,
        JournalEntry.deleted_at.is_(None)
    ).order_by(JournalEntry.posting_date.desc())

    if period:
        query = query.where(JournalEntry.posting_period == period)

    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()

@router.get("/journal/{entry_id}", response_model=JournalEntryResponse)
async def get_journal_entry(
    entry_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """Get single journal entry with lines."""
    result = await db.execute(
        select(JournalEntry).where(
            JournalEntry.id == entry_id,
            JournalEntry.organisation_id == current_user.organisation_id
        ).options(selectinload(JournalEntry.lines))
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Journal entry not found")
    return entry

@router.post("/simple", response_model=APIResponse)
async def create_simple_transaction(
    request: Request,
    tx_type: str,  # "in" | "out" | "transfer"
    amount: Decimal,
    currency: str = "KES",
    category_id: str,
    description: str = "",
    date: date = None,
    reference: str = None,
    current_user: User = Depends(require_role(["OWNER", "ACCOUNTANT", "STAFF"])),
    db: AsyncSession = Depends(get_db)
):
    """Simple mode transaction — under 10 seconds entry."""

    if date is None:
        date = date.today()

    # Get cash account
    result = await db.execute(
        select(Account).where(
            Account.organisation_id == current_user.organisation_id,
            Account.code == "1000",
            Account.status == "ACTIVE"
        )
    )
    cash_account = result.scalar_one_or_none()
    if not cash_account:
        raise HTTPException(status_code=500, detail="Cash account not configured")

    # Get category account
    result = await db.execute(
        select(Account).where(
            Account.id == category_id,
            Account.organisation_id == current_user.organisation_id,
            Account.status == "ACTIVE"
        )
    )
    category_account = result.scalar_one_or_none()
    if not category_account:
        raise HTTPException(status_code=400, detail="Category not found")

    # Build double-entry based on type
    if tx_type == "in":  # Money In
        lines = [
            JournalLineCreate(account_id=cash_account.id, line_type="DEBIT", amount=amount),
            JournalLineCreate(account_id=category_account.id, line_type="CREDIT", amount=amount)
        ]
        narration = f"Income: {description or category_account.name}"
    elif tx_type == "out":  # Money Out
        lines = [
            JournalLineCreate(account_id=category_account.id, line_type="DEBIT", amount=amount),
            JournalLineCreate(account_id=cash_account.id, line_type="CREDIT", amount=amount)
        ]
        narration = f"Expense: {description or category_account.name}"
    else:  # transfer
        lines = [
            JournalLineCreate(account_id=category_account.id, line_type="DEBIT", amount=amount),
            JournalLineCreate(account_id=cash_account.id, line_type="CREDIT", amount=amount)
        ]
        narration = f"Transfer: {description or category_account.name}"

    entry = JournalEntryCreate(
        posting_date=date,
        narration=narration,
        currency=currency,
        lines=lines,
        source_document=reference
    )

    journal = await create_journal_entry(request, entry, current_user, db)

    return APIResponse(
        success=True,
        message=f"Transaction posted: {journal.reference}",
        data={"journal_id": str(journal.id), "reference": journal.reference}
    )

@router.get("/accounts", response_model=List[dict])
async def list_accounts(
    account_type: str = None,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """List chart of accounts."""
    query = select(Account).where(
        Account.organisation_id == current_user.organisation_id,
        Account.deleted_at.is_(None),
        Account.status == "ACTIVE"
    ).order_by(Account.code)

    if account_type:
        query = query.where(Account.account_type == account_type.upper())

    result = await db.execute(query)
    accounts = result.scalars().all()

    return [{
        "id": str(a.id),
        "code": a.code,
        "name": a.name,
        "type": a.account_type,
        "normal_balance": a.normal_balance,
        "is_system": a.is_system,
        "currency": a.currency
    } for a in accounts]
