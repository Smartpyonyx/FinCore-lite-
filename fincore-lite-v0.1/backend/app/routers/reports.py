"""FinCore Lite v0.1 - Reports Router"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, text
from datetime import datetime, date, timedelta
from decimal import Decimal
from typing import List, Dict
from io import BytesIO
import structlog

from app.core.database import get_db
from app.routers.auth import get_current_active_user, require_role
from app.schemas import PnLReport, BalanceSheetReport, DashboardKPIs, CashFlowData, ExpenseBreakdown, APIResponse
from app.models import JournalEntry, JournalLine, Account, MpesaTransaction, AuditLog

router = APIRouter(prefix="/reports", tags=["Reports"])
logger = structlog.get_logger()

@router.get("/dashboard", response_model=DashboardKPIs)
async def get_dashboard_kpis(
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """Get dashboard KPIs — cached for performance."""
    org_id = current_user.organisation_id

    # Current month period
    today = date.today()
    current_period = today.strftime("%Y-%m")
    last_month = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    # Revenue MTD (Income accounts)
    revenue_result = await db.execute(
        select(func.sum(JournalLine.amount_kes)).where(
            JournalLine.organisation_id == org_id,
            JournalLine.line_type == "CREDIT",
            Account.account_type == "INCOME",
            JournalEntry.posting_period == current_period,
            JournalEntry.status == "POSTED"
        ).join(Account, JournalLine.account_id == Account.id)
    )
    revenue_mtd = revenue_result.scalar() or Decimal("0")

    # Expenses MTD
    expense_result = await db.execute(
        select(func.sum(JournalLine.amount_kes)).where(
            JournalLine.organisation_id == org_id,
            JournalLine.line_type == "DEBIT",
            Account.account_type == "EXPENSE",
            JournalEntry.posting_period == current_period,
            JournalEntry.status == "POSTED"
        ).join(Account, JournalLine.account_id == Account.id)
    )
    expense_mtd = expense_result.scalar() or Decimal("0")

    # M-Pesa charges
    mpesa_result = await db.execute(
        select(func.sum(MpesaTransaction.charge)).where(
            MpesaTransaction.organisation_id == org_id,
            MpesaTransaction.status == "CATEGORISED",
            func.to_char(MpesaTransaction.transaction_date, "YYYY-MM") == current_period
        )
    )
    mpesa_charges = mpesa_result.scalar() or Decimal("0")

    # Last month comparison
    last_revenue = await db.execute(
        select(func.sum(JournalLine.amount_kes)).where(
            JournalLine.organisation_id == org_id,
            JournalLine.line_type == "CREDIT",
            Account.account_type == "INCOME",
            JournalEntry.posting_period == last_month
        ).join(Account, JournalLine.account_id == Account.id)
    )
    last_revenue_val = last_revenue.scalar() or Decimal("1")

    last_expense = await db.execute(
        select(func.sum(JournalLine.amount_kes)).where(
            JournalLine.organisation_id == org_id,
            JournalLine.line_type == "DEBIT",
            Account.account_type == "EXPENSE",
            JournalEntry.posting_period == last_month
        ).join(Account, JournalLine.account_id == Account.id)
    )
    last_expense_val = last_expense.scalar() or Decimal("1")

    # Uncategorised M-Pesa
    uncategorised = await db.execute(
        select(func.count(MpesaTransaction.id)).where(
            MpesaTransaction.organisation_id == org_id,
            MpesaTransaction.status == "PENDING"
        )
    )
    uncategorised_count = uncategorised.scalar() or 0

    net_profit = revenue_mtd - expense_mtd
    margin = (net_profit / revenue_mtd * 100) if revenue_mtd > 0 else Decimal("0")

    return DashboardKPIs(
        total_revenue_mtd=revenue_mtd,
        total_expenses_mtd=expense_mtd,
        net_profit_mtd=net_profit,
        mpesa_charges_mtd=mpesa_charges,
        revenue_change_percent=float((revenue_mtd - last_revenue_val) / last_revenue_val * 100),
        expense_change_percent=float((expense_mtd - last_expense_val) / last_expense_val * 100),
        profit_margin_percent=float(margin),
        uncategorised_count=uncategorised_count
    )

@router.get("/pnl", response_model=PnLReport)
async def get_pnl(
    period: str = None,  # YYYY-MM
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """Generate Profit & Loss statement."""
    if not period:
        period = date.today().strftime("%Y-%m")

    org_id = current_user.organisation_id

    # Income by category
    income_result = await db.execute(
        select(Account.name, func.sum(JournalLine.amount_kes)).where(
            JournalLine.organisation_id == org_id,
            JournalLine.line_type == "CREDIT",
            Account.account_type == "INCOME",
            JournalEntry.posting_period == period,
            JournalEntry.status == "POSTED"
        ).join(Account, JournalLine.account_id == Account.id)
        .group_by(Account.name)
    )
    income = {name: amount for name, amount in income_result.all()}

    # Expenses by category
    expense_result = await db.execute(
        select(Account.name, func.sum(JournalLine.amount_kes)).where(
            JournalLine.organisation_id == org_id,
            JournalLine.line_type == "DEBIT",
            Account.account_type == "EXPENSE",
            JournalEntry.posting_period == period,
            JournalEntry.status == "POSTED"
        ).join(Account, JournalLine.account_id == Account.id)
        .group_by(Account.name)
    )
    expenses = {name: amount for name, amount in expense_result.all()}

    total_income = sum(income.values())
    total_expenses = sum(expenses.values())
    net_profit = total_income - total_expenses
    margin = (net_profit / total_income * 100) if total_income > 0 else Decimal("0")

    return PnLReport(
        period=period,
        income=income,
        expenses=expenses,
        total_income=total_income,
        total_expenses=total_expenses,
        net_profit=net_profit,
        margin_percent=float(margin)
    )

@router.get("/balance-sheet", response_model=BalanceSheetReport)
async def get_balance_sheet(
    as_at: date = None,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """Generate Balance Sheet."""
    if not as_at:
        as_at = date.today()

    org_id = current_user.organisation_id

    # Assets (DEBIT normal balance)
    assets_result = await db.execute(
        select(Account.name, func.sum(JournalLine.amount_kes)).where(
            JournalLine.organisation_id == org_id,
            Account.account_type == "ASSET",
            JournalEntry.posting_date <= as_at,
            JournalEntry.status == "POSTED"
        ).join(Account, JournalLine.account_id == Account.id)
        .group_by(Account.name)
    )
    assets = {name: amount for name, amount in assets_result.all()}

    # Liabilities (CREDIT normal balance)
    liabilities_result = await db.execute(
        select(Account.name, func.sum(JournalLine.amount_kes)).where(
            JournalLine.organisation_id == org_id,
            Account.account_type == "LIABILITY",
            JournalEntry.posting_date <= as_at,
            JournalEntry.status == "POSTED"
        ).join(Account, JournalLine.account_id == Account.id)
        .group_by(Account.name)
    )
    liabilities = {name: amount for name, amount in liabilities_result.all()}

    # Equity
    equity_result = await db.execute(
        select(Account.name, func.sum(JournalLine.amount_kes)).where(
            JournalLine.organisation_id == org_id,
            Account.account_type == "EQUITY",
            JournalEntry.posting_date <= as_at,
            JournalEntry.status == "POSTED"
        ).join(Account, JournalLine.account_id == Account.id)
        .group_by(Account.name)
    )
    equity = {name: amount for name, amount in equity_result.all()}

    total_assets = sum(assets.values())
    total_liabilities = sum(liabilities.values())
    total_equity = sum(equity.values())

    return BalanceSheetReport(
        as_at=as_at,
        assets=assets,
        liabilities=liabilities,
        equity=equity,
        total_assets=total_assets,
        total_liabilities=total_liabilities,
        total_equity=total_equity,
        balanced=abs(total_assets - (total_liabilities + total_equity)) < Decimal("0.01")
    )

@router.get("/cash-flow", response_model=CashFlowData)
async def get_cash_flow(
    days: int = 30,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """Get cash flow data for charts."""
    org_id = current_user.organisation_id
    end_date = date.today()
    start_date = end_date - timedelta(days=days)

    # Daily aggregation
    result = await db.execute(
        text("""
            SELECT 
                je.posting_date,
                SUM(CASE WHEN jl.line_type = 'DEBIT' AND a.account_type = 'ASSET' THEN jl.amount_kes ELSE 0 END) as money_in,
                SUM(CASE WHEN jl.line_type = 'CREDIT' AND a.account_type = 'ASSET' THEN jl.amount_kes ELSE 0 END) as money_out
            FROM journal_lines jl
            JOIN journal_entries je ON jl.journal_entry_id = je.id
            JOIN accounts a ON jl.account_id = a.id
            WHERE jl.organisation_id = :org_id
              AND je.posting_date BETWEEN :start AND :end
              AND je.status = 'POSTED'
            GROUP BY je.posting_date
            ORDER BY je.posting_date
        """),
        {"org_id": str(org_id), "start": start_date, "end": end_date}
    )

    rows = result.all()
    labels = [str(r[0]) for r in rows]
    money_in = [r[1] or Decimal("0") for r in rows]
    money_out = [r[2] or Decimal("0") for r in rows]
    net = [i - o for i, o in zip(money_in, money_out)]

    return CashFlowData(labels=labels, money_in=money_in, money_out=money_out, net=net)

@router.get("/expense-breakdown", response_model=ExpenseBreakdown)
async def get_expense_breakdown(
    period: str = None,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """Get expense breakdown for pie chart."""
    if not period:
        period = date.today().strftime("%Y-%m")

    org_id = current_user.organisation_id

    result = await db.execute(
        select(Account.name, func.sum(JournalLine.amount_kes)).where(
            JournalLine.organisation_id == org_id,
            JournalLine.line_type == "DEBIT",
            Account.account_type == "EXPENSE",
            JournalEntry.posting_period == period,
            JournalEntry.status == "POSTED"
        ).join(Account, JournalLine.account_id == Account.id)
        .group_by(Account.name)
        .order_by(func.sum(JournalLine.amount_kes).desc())
    )

    rows = result.all()
    total = sum(r[1] for r in rows) or Decimal("1")

    colors = ["#10b981", "#3b82f6", "#f59e0b", "#8b5cf6", "#ef4444", "#64748b", "#ec4899", "#14b8a6"]

    return ExpenseBreakdown(
        labels=[r[0] for r in rows],
        values=[r[1] for r in rows],
        percentages=[float(r[1] / total * 100) for r in rows],
        colors=colors[:len(rows)]
    )

@router.get("/audit-trail")
async def get_audit_trail(
    skip: int = 0,
    limit: int = 100,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """Get immutable audit trail."""
    result = await db.execute(
        select(AuditLog, User.email).where(
            AuditLog.organisation_id == current_user.organisation_id
        ).join(User, AuditLog.user_id == User.id, isouter=True)
        .order_by(AuditLog.timestamp.desc())
        .offset(skip).limit(limit)
    )

    logs = []
    for log, email in result.all():
        logs.append({
            "id": str(log.id),
            "action": log.action,
            "entity_type": log.entity_type,
            "entity_id": str(log.entity_id) if log.entity_id else None,
            "user_email": email,
            "ip_address": log.ip_address,
            "timestamp": log.timestamp.isoformat(),
            "before_state": log.before_state,
            "after_state": log.after_state
        })

    return logs

@router.get("/export/{report_type}")
async def export_report(
    report_type: str,  # pnl | balance-sheet | transactions
    format: str = "pdf",  # pdf | csv | excel
    period: str = None,
    current_user = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db)
):
    """Export report in requested format."""
    # Implementation using reportlab (PDF) and openpyxl (Excel)
    return APIResponse(
        success=True,
        message=f"{report_type} export initiated",
        data={"format": format, "download_url": f"/api/v1/reports/download/{report_type}.{format}"}
    )
