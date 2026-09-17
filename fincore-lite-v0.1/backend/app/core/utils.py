"""FinCore Lite v0.1 - Shared Utilities"""
from datetime import datetime, date as date_class, timezone
from decimal import Decimal
from typing import Optional
import structlog

from fastapi import Request
from app.core.database import get_db
from app.models import JournalEntry, Account, ExchangeRate, JournalLine, User, AuditLog
from app.schemas import JournalEntryCreate, JournalLineCreate, APIResponse

logger = structlog.get_logger()


async def get_exchange_rate(
    db,
    from_currency: str,
    to_currency: str = "KES",
    rate_date: date_class = None
) -> Decimal:
    """Get exchange rate for currency conversion."""
    if from_currency == to_currency:
        return Decimal("1.0")

    if rate_date is None:
        rate_date = date_class.today()

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

    result = await db.execute(
        select(ExchangeRate).where(
            ExchangeRate.from_currency == from_currency,
            ExchangeRate.to_currency == to_currency
        ).order_by(ExchangeRate.rate_date.desc())
    )
    rate = result.scalar_one_or_none()

    if rate:
        return rate.rate

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


async def generate_reference(db, org_id: str) -> str:
    """Generate unique journal entry reference: JE-YYYY-XXXX."""
    from sqlalchemy import func, select
    today = date_class.today()
    year = today.year

    result = await db.execute(
        select(func.count(JournalEntry.id)).where(
            JournalEntry.organisation_id == org_id,
            JournalEntry.posting_period.like(f"{year}-%")
        ).with_for_update()
    )
    count = result.scalar() + 1
    return f"JE-{year}-{count:04d}"


async def create_journal_entry(
    request: Request,
    entry: JournalEntryCreate,
    current_user: User,
    db,
    journal_type: str = "MANUAL"
) -> JournalEntry:
    """Create and post a journal entry to the General Ledger."""
    from sqlalchemy import select
    from fastapi import HTTPException, status

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

    exchange_rate = await get_exchange_rate(db, entry.currency, "KES", entry.posting_date)
    reference = await generate_reference(db, str(current_user.organisation_id))

    journal = JournalEntry(
        organisation_id=current_user.organisation_id,
        reference=reference,
        journal_type=journal_type,
        posting_date=entry.posting_date,
        posting_period=entry.posting_date.strftime("%Y-%m"),
        narration=entry.narration,
        source_document=entry.source_document,
        source_type=journal_type,
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

    client_ip = request.client.host if request.client else "unknown"
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()

    audit = AuditLog(
        organisation_id=current_user.organisation_id,
        user_id=current_user.id,
        action=f"{journal_type}_JOURNAL_POSTED",
        entity_type="JournalEntry",
        entity_id=journal.id,
        after_state={
            "reference": reference,
            "total_debit": str(journal.total_debit_kes),
            "total_credit": str(journal.total_credit_kes),
            "currency": entry.currency
        },
        ip_address=client_ip,
    )
    db.add(audit)

    await db.commit()
    await db.refresh(journal)

    logger.info(
        "journal_posted",
        journal_id=str(journal.id),
        reference=reference,
        org_id=str(current_user.organisation_id),
        journal_type=journal_type
    )

    return journal


def get_client_ip(request: Request) -> str:
    """Extract client IP from request, handling proxies."""
    client_ip = request.client.host if request.client else "unknown"
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()
    return client_ip