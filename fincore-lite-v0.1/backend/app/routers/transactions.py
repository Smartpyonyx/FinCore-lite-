"""FinCore Lite v0.1 - Transactions Router"""
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, text
from sqlalchemy.orm import selectinload
from datetime import datetime, timezone, date as date_class
from decimal import Decimal
from typing import List
import uuid
import structlog

from app.core.database import get_db
from app.core.config import get_settings
from app.routers.auth import get_current_active_user, require_role
from app.core.utils import (
    get_exchange_rate, generate_reference, create_journal_entry, get_client_ip
)
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


@router.post("/journal", response_model=JournalEntryResponse, status_code=status.HTTP_201_CREATED)
async def create_journal_entry_endpoint(
    request: Request,
    entry: JournalEntryCreate,
    current_user: User = Depends(require_role(["OWNER", "ACCOUNTANT", "STAFF"])),
    db: AsyncSession = Depends(get_db)
):
    """Create and post a journal entry to the General Ledger."""
    return await create_journal_entry(request, entry, current_user, db)


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
    category_id: str,
    currency: str = "KES",
    description: str = "",
    posting_date: date_class = None,
    reference: str = None,
    current_user: User = Depends(require_role(["OWNER", "ACCOUNTANT", "STAFF"])),
    db: AsyncSession = Depends(get_db)
):
    """Simple mode transaction — under 10 seconds entry."""

    if posting_date is None:
        posting_date = date_class.today()

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
        posting_date=posting_date,
        narration=narration,
        currency=currency,
        lines=lines,
        source_document=reference
    )

    # Call the internal logic directly instead of via the endpoint
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