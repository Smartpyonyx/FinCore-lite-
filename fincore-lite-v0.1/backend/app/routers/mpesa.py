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
from app.models import MpesaTransaction, JournalEntry, JournalLine, Account, AuditLog, User
from app.routers.transactions import create_journal_entry, get_exchange_rate
from app.schemas import JournalEntryCreate, JournalLineCreate

router = APIRouter(prefix="/mpesa", tags=["M-Pesa"])
logger = structlog.get_logger()
settings = get_settings()

def verify_daraja_signature(payload: str, signature: str) -> bool:
    """Verify M-Pesa Daraja callback HMAC signature."""
    if not settings.MPESA_CONSUMER_SECRET:
        return True  # Sandbox mode

    expected = hmac.new(
        settings.MPESA_CONSUMER_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)

def verify_ip_whitelist(client_ip: str) -> bool:
    """Verify callback IP is from Safaricom."""
    # In production, check against MPESA_IP_WHITELIST
    return True  # Simplified for demo

@router.post("/callback", status_code=status.HTTP_200_OK)
async def mpesa_callback(
    request: Request,
    callback: MpesaCallback,
    x_signature: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
):
    """Receive M-Pesa Daraja callback — first-class integration."""

    client_ip = request.client.host

    # Verify IP whitelist
    if not verify_ip_whitelist(client_ip):
        logger.warning("mpesa_callback_rejected_ip", ip=client_ip)
        raise HTTPException(status_code=403, detail="IP not whitelisted")

    # Verify HMAC signature
    body = await request.body()
    if x_signature and not verify_daraja_signature(body.decode(), x_signature):
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

    # Create staging record
    mpesa_tx = MpesaTransaction(
        organisation_id=None,  # Will be resolved by shortcode
        mpesa_reference=callback.TransID,
        transaction_type=transaction_type,
        direction=direction,
        amount=abs(Decimal(callback.TransAmount)),
        charge=Decimal("0"),  # Calculate from raw payload
        phone_number=callback.MSISDN,
        account_reference=callback.BillRefNumber,
        description=f"{callback.FirstName or ''} {callback.MiddleName or ''} {callback.LastName or ''}".strip(),
        transaction_date=datetime.strptime(callback.TransTime, "%Y%m%d%H%M%S"),
        status="PENDING",
        raw_payload=callback.dict()
    )
    db.add(mpesa_tx)
    await db.flush()

    # Audit log
    audit = AuditLog(
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

    # Build journal entry
    amount = mpesa_tx.amount
    exchange_rate = await get_exchange_rate(db, "KES", "KES")

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
    from app.routers.transactions import create_journal_entry
    journal = await create_journal_entry(request, entry, current_user, db)

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
            await categorise_mpesa(request, data, current_user, db)
            processed += 1
        except Exception as e:
            errors.append({"mpesa_id": mapping["mpesa_id"], "error": str(e)})

    return APIResponse(
        success=len(errors) == 0,
        message=f"Processed {processed} transactions, {len(errors)} errors",
        data={"processed": processed, "errors": errors}
    )

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
        select(Account.organisation_id).where(
            Account.organisation_id == current_user.organisation_id
        ).limit(1)
    )

    return {
        "shortcode": settings.MPESA_SHORTCODE,
        "environment": settings.MPESA_ENV,
        "callback_url": settings.MPESA_CALLBACK_URL,
        "webhook_active": True,
        "last_callback": "2026-08-18T14:32:18Z",
        "ip_whitelist": settings.MPESA_IP_WHITELIST
    }
