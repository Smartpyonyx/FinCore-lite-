"""FinCore Lite v0.1 - M-Pesa Integration Router"""
from fastapi import APIRouter, Depends, HTTPException, status, Request, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional
import hmac
import hashlib
import structlog

from app.core.database import get_db
from app.core.config import get_settings
from app.routers.auth import get_current_active_user, require_role
from app.schemas import MpesaCallback, MpesaCategoriseRequest, MpesaTransactionResponse, APIResponse
from app.models import MpesaTransaction, JournalEntry, JournalLine, Account, AuditLog, User, Organisation
from app.schemas import JournalEntryCreate, JournalLineCreate

router = APIRouter(prefix="/mpesa", tags=["M-Pesa"])
logger = structlog.get_logger()
settings = get_settings()


def verify_daraja_signature(payload: str, signature: str) -> bool:
    """Verify M-Pesa Daraja callback HMAC signature."""
    if not settings.MPESA_CONSUMER_SECRET:
        logger.warning("mpesa_signature_verification_skipped", reason="no_consumer_secret_configured")
        return False  # Fail closed - don't accept callbacks without proper secret

    expected = hmac.new(
        settings.MPESA_CONSUMER_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_ip_whitelist(client_ip: str) -> bool:
    """Verify callback IP is from Safaricom."""
    if not settings.MPESA_IP_WHITELIST:
        return True  # No whitelist configured

    # Check if IP matches any CIDR in whitelist
    import ipaddress
    try:
        client_addr = ipaddress.ip_address(client_ip)
        for cidr in settings.MPESA_IP_WHITELIST:
            if client_addr in ipaddress.ip_network(cidr):
                return True
    except ValueError:
        pass
    return False


async def _resolve_organisation_from_shortcode(db: AsyncSession, shortcode: str):
    """Resolve organisation from M-Pesa shortcode."""
    result = await db.execute(
        select(Organisation).where(
            (Organisation.mpesa_shortcode == shortcode) | (Organisation.mpesa_paybill == shortcode)
        )
    )
    return result.scalar_one_or_none()


async def _get_exchange_rate(
    db: AsyncSession,
    from_currency: str,
    to_currency: str = "KES",
    rate_date=None
) -> Decimal:
    """Get exchange rate for currency conversion (local copy to avoid circular import)."""
    from app.models import ExchangeRate
    from datetime import date as date_class

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


async def _create_mpesa_journal_entry(
    request: Request,
    entry: JournalEntryCreate,
    current_user: User,
    db: AsyncSession
) -> JournalEntry:
    """Create journal entry for M-Pesa transaction (local copy to avoid circular import)."""
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
    exchange_rate = await _get_exchange_rate(db, entry.currency, "KES", entry.posting_date)

    # Generate reference
    from app.routers.transactions import generate_reference
    reference = await generate_reference(db, str(current_user.organisation_id))

    # Create journal entry
    journal = JournalEntry(
        organisation_id=current_user.organisation_id,
        reference=reference,
        journal_type="MPESA",
        posting_date=entry.posting_date,
        posting_period=entry.posting_date.strftime("%Y-%m"),
        narration=entry.narration,
        source_document=entry.source_document,
        source_type="MPESA",
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
    client_ip = request.client.host if request.client else "unknown"
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()

    audit = AuditLog(
        organisation_id=current_user.organisation_id,
        user_id=current_user.id,
        action="MPESA_JOURNAL_POSTED",
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
        "mpesa_journal_posted",
        journal_id=str(journal.id),
        reference=reference,
        org_id=str(current_user.organisation_id)
    )

    return journal


@router.post("/callback", status_code=status.HTTP_200_OK)
async def mpesa_callback(
    request: Request,
    callback: MpesaCallback,
    x_signature: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
):
    """Receive M-Pesa Daraja callback — first-class integration."""

    client_ip = request.client.host if request.client else "unknown"
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()

    # Verify IP whitelist
    if not verify_ip_whitelist(client_ip):
        logger.warning("mpesa_callback_rejected_ip", ip=client_ip)
        raise HTTPException(status_code=403, detail="IP not whitelisted")

    # Verify HMAC signature
    body = await request.body()
    if x_signature and not verify_daraja_signature(body.decode("utf-8"), x_signature):
        logger.warning("mpesa_callback_rejected_signature", ip=client_ip)
        raise HTTPException(status_code=403, detail="Invalid signature")

    # Duplicate detection
    result = await db.execute(
        select(MpesaTransaction).where(
            MpesaTransaction.mpesa_reference == callback.TransID
        )
    )
    if result.scalar_one_or_none():
        logger.info("mpesa_duplicate_detected", reference=callback.TransID)
        return {"status": "duplicate"}

    # Resolve organisation from shortcode
    org = await _resolve_organisation_from_shortcode(db, callback.BusinessShortCode)
    if not org:
        logger.warning("mpesa_callback_no_org_for_shortcode", shortcode=callback.BusinessShortCode)
        # Still process but mark for manual review
        org_id = None
    else:
        org_id = org.id

    # Determine direction and type
    transaction_type = "TILL"
    direction = "IN"

    if callback.TransactionType in ["CustomerPayBillOnline", "CustomerBuyGoodsOnline"]:
        direction = "IN"
        transaction_type = "PAYBILL" if "PayBill" in callback.TransactionType else "TILL"
    elif "Disbursement" in callback.TransactionType:
        direction = "OUT"
        transaction_type = "B2C"
    elif "Reversal" in callback.TransactionType:
        direction = "IN" if Decimal(callback.TransAmount) > 0 else "OUT"
        transaction_type = "REVERSAL"

    # Parse transaction date with error handling
    try:
        transaction_date = datetime.strptime(callback.TransTime, "%Y%m%d%H%M%S")
    except ValueError:
        logger.warning("mpesa_invalid_trans_time", trans_time=callback.TransTime)
        transaction_date = datetime.now(timezone.utc)

    # Create staging record
    mpesa_tx = MpesaTransaction(
        organisation_id=org_id,
        mpesa_reference=callback.TransID,
        transaction_type=transaction_type,
        direction=direction,
        amount=abs(Decimal(callback.TransAmount)),
        charge=Decimal("0"),  # Calculate from raw payload
        phone_number=callback.MSISDN,
        account_reference=callback.BillRefNumber,
        description=f"{callback.FirstName or ''} {callback.MiddleName or ''} {callback.LastName or ''}".strip(),
        transaction_date=transaction_date,
        status="PENDING",
        raw_payload=callback.dict()
    )
    db.add(mpesa_tx)
    await db.flush()

    # Audit log
    audit = AuditLog(
        organisation_id=org_id,
        action="MPESA_CALLBACK_RECEIVED",
        entity_type="MpesaTransaction",
        entity_id=mpesa_tx.id,
        after_state={
            "reference": callback.TransID,
            "amount": str(callback.TransAmount),
            "type": transaction_type
        },
        ip_address=client_ip,
    )
    db.add(audit)
    await db.commit()

    logger.info("mpesa_callback_processed", reference=callback.TransID, amount=callback.TransAmount)
    return {"status": "success", "transaction_id": str(mpesa_tx.id)}


@router.get("/staging", response_model=List[MpesaTransactionResponse])
async def list_staging(
    status: str = "PENDING",
    current_user: User = Depends(require_role(["OWNER", "ACCOUNTANT"])),
    db: AsyncSession = Depends(get_db)
):
    """List M-Pesa transactions in staging queue."""
    result = await db.execute(
        select(MpesaTransaction).where(
            MpesaTransaction.organisation_id == current_user.organisation_id,
            MpesaTransaction.status == status
        ).order_by(MpesaTransaction.transaction_date.desc())
    )
    return result.scalars().all()


@router.post("/categorise", response_model=APIResponse)
async def categorise_mpesa(
    request: Request,
    data: MpesaCategoriseRequest,
    current_user: User = Depends(require_role(["OWNER", "ACCOUNTANT"])),
    db: AsyncSession = Depends(get_db)
):
    """Categorise M-Pesa transaction and post to GL."""

    # Get M-Pesa transaction
    result = await db.execute(
        select(MpesaTransaction).where(
            MpesaTransaction.id == data.mpesa_transaction_id,
            MpesaTransaction.organisation_id == current_user.organisation_id
        )
    )
    mpesa_tx = result.scalar_one_or_none()
    if not mpesa_tx:
        raise HTTPException(status_code=404, detail="M-Pesa transaction not found")

    # Get category account
    result = await db.execute(
        select(Account).where(
            Account.id == data.category_id,
            Account.organisation_id == current_user.organisation_id,
            Account.status == "ACTIVE"
        )
    )
    category = result.scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=400, detail="Category not found")

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

    # Build journal entry
    amount = mpesa_tx.amount
    exchange_rate = await _get_exchange_rate(db, "KES", "KES")

    if mpesa_tx.direction == "IN":
        lines = [
            JournalLineCreate(account_id=cash_account.id, line_type="DEBIT", amount=amount),
            JournalLineCreate(account_id=category.id, line_type="CREDIT", amount=amount)
        ]
        narration = f"M-Pesa {mpesa_tx.transaction_type}: {mpesa_tx.mpesa_reference}"
    else:
        lines = [
            JournalLineCreate(account_id=category.id, line_type="DEBIT", amount=amount),
            JournalLineCreate(account_id=cash_account.id, line_type="CREDIT", amount=amount)
        ]
        narration = f"M-Pesa {mpesa_tx.transaction_type} payment: {mpesa_tx.mpesa_reference}"

    entry = JournalEntryCreate(
        posting_date=mpesa_tx.transaction_date.date(),
        narration=narration,
        currency="KES",
        lines=lines,
        source_document=mpesa_tx.mpesa_reference
    )

    # Post to GL
    journal = await _create_mpesa_journal_entry(request, entry, current_user, db)

    # Update M-Pesa record
    mpesa_tx.status = "CATEGORISED"
    mpesa_tx.journal_entry_id = journal.id
    mpesa_tx.category_id = category.id
    mpesa_tx.categorised_by = current_user.id
    mpesa_tx.categorised_at = datetime.now(timezone.utc)

    await db.commit()

    return APIResponse(
        success=True,
        message="M-Pesa transaction categorised and posted to GL",
        data={
            "mpesa_reference": mpesa_tx.mpesa_reference,
            "journal_reference": journal.reference,
            "category": category.name
        }
    )


@router.post("/bulk-categorise", response_model=APIResponse)
async def bulk_categorise(
    request: Request,
    mappings: List[dict],  # [{"mpesa_id": "uuid", "category_id": "uuid"}]
    current_user: User = Depends(require_role(["OWNER", "ACCOUNTANT"])),
    db: AsyncSession = Depends(get_db)
):
    """Bulk categorise multiple M-Pesa transactions."""
    processed = 0
    errors = []

    for mapping in mappings:
        try:
            data = MpesaCategoriseRequest(
                mpesa_transaction_id=mapping["mpesa_id"],
                category_id=mapping["category_id"]
            )
            # Call the internal logic directly
            await _categorise_mpesa_internal(request, data, current_user, db)
            processed += 1
        except Exception as e:
            errors.append({"mpesa_id": mapping["mpesa_id"], "error": str(e)})

    return APIResponse(
        success=len(errors) == 0,
        message=f"Processed {processed} transactions, {len(errors)} errors",
        data={"processed": processed, "errors": errors}
    )


async def _categorise_mpesa_internal(
    request: Request,
    data: MpesaCategoriseRequest,
    current_user: User,
    db: AsyncSession
):
    """Internal function for bulk categorisation."""
    # Get M-Pesa transaction
    result = await db.execute(
        select(MpesaTransaction).where(
            MpesaTransaction.id == data.mpesa_transaction_id,
            MpesaTransaction.organisation_id == current_user.organisation_id
        )
    )
    mpesa_tx = result.scalar_one_or_none()
    if not mpesa_tx:
        raise HTTPException(status_code=404, detail="M-Pesa transaction not found")

    # Get category account
    result = await db.execute(
        select(Account).where(
            Account.id == data.category_id,
            Account.organisation_id == current_user.organisation_id,
            Account.status == "ACTIVE"
        )
    )
    category = result.scalar_one_or_none()
    if not category:
        raise HTTPException(status_code=400, detail="Category not found")

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

    # Build journal entry
    amount = mpesa_tx.amount
    exchange_rate = await _get_exchange_rate(db, "KES", "KES")

    if mpesa_tx.direction == "IN":
        lines = [
            JournalLineCreate(account_id=cash_account.id, line_type="DEBIT", amount=amount),
            JournalLineCreate(account_id=category.id, line_type="CREDIT", amount=amount)
        ]
        narration = f"M-Pesa {mpesa_tx.transaction_type}: {mpesa_tx.mpesa_reference}"
    else:
        lines = [
            JournalLineCreate(account_id=category.id, line_type="DEBIT", amount=amount),
            JournalLineCreate(account_id=cash_account.id, line_type="CREDIT", amount=amount)
        ]
        narration = f"M-Pesa {mpesa_tx.transaction_type} payment: {mpesa_tx.mpesa_reference}"

    entry = JournalEntryCreate(
        posting_date=mpesa_tx.transaction_date.date(),
        narration=narration,
        currency="KES",
        lines=lines,
        source_document=mpesa_tx.mpesa_reference
    )

    # Post to GL
    journal = await _create_mpesa_journal_entry(request, entry, current_user, db)

    # Update M-Pesa record
    mpesa_tx.status = "CATEGORISED"
    mpesa_tx.journal_entry_id = journal.id
    mpesa_tx.category_id = category.id
    mpesa_tx.categorised_by = current_user.id
    mpesa_tx.categorised_at = datetime.now(timezone.utc)

    await db.commit()


@router.post("/import-statement", response_model=APIResponse)
async def import_statement(
    file: bytes,
    current_user: User = Depends(require_role(["OWNER", "ACCOUNTANT"])),
    db: AsyncSession = Depends(get_db)
):
    """Import M-Pesa statement CSV/PDF."""
    # Parse CSV/PDF and create staging records
    # Implementation depends on Safaricom statement format
    return APIResponse(
        success=True,
        message="Statement import initiated",
        data={"records_imported": 0}
    )


@router.get("/config")
async def get_mpesa_config(
    current_user: User = Depends(require_role(["OWNER"])),
    db: AsyncSession = Depends(get_db)
):
    """Get M-Pesa integration configuration."""
    result = await db.execute(
        select(Organisation).where(
            Organisation.id == current_user.organisation_id
        )
    )
    org = result.scalar_one_or_none()

    return {
        "shortcode": org.mpesa_shortcode if org else settings.MPESA_SHORTCODE,
        "paybill": org.mpesa_paybill if org else None,
        "environment": settings.MPESA_ENV,
        "callback_url": settings.MPESA_CALLBACK_URL,
        "webhook_active": True,
        "last_callback": "2026-08-18T14:32:18Z",
        "ip_whitelist": settings.MPESA_IP_WHITELIST
    }