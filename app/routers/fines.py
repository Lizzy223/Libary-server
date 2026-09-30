from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import LedgerEntry, Member
from ..security import current_member, require_roles
from ..serializers import ledger_out
from ..services import circulation as circ
from ..services.audit import audit
from ..timeutil import day_bounds_utc, to_wat, utcnow

router = APIRouter(prefix="/fines", tags=["fines"])
DESK = require_roles("circulation", "head_librarian")


class PaymentIn(BaseModel):
    member_id_number: str
    amount: int = Field(gt=0)
    method: str = Field(pattern="^(cash|bank_transfer|pos)$")
    receipt_no: str = Field(min_length=1, max_length=60)


class WaiverIn(BaseModel):
    member_id_number: str
    amount: int = Field(gt=0)
    reason: str = Field(min_length=5, max_length=300)


def _ledger(db: Session, m: Member) -> dict:
    entries = db.scalars(select(LedgerEntry).where(LedgerEntry.member_id == m.id).order_by(LedgerEntry.id.desc())).all()
    return {"owed": max(0, circ.member_balance(db, m.id)), "entries": [ledger_out(e) for e in entries]}


def _find(db: Session, id_number: str) -> Member:
    m = db.scalar(select(Member).where(Member.id_number == id_number.strip().upper()))
    if not m:
        raise HTTPException(404, "No member found with that ID number.")
    return m


@router.get("/me")
def my_ledger(actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    return _ledger(db, actor)


@router.get("/member/{id_number:path}")
def member_ledger(id_number: str, _: Member = Depends(DESK), db: Session = Depends(get_db)):
    return _ledger(db, _find(db, id_number))


@router.post("/payments")
def record_payment(body: PaymentIn, officer: Member = Depends(DESK), db: Session = Depends(get_db)):
    m = _find(db, body.member_id_number)
    owed = max(0, circ.member_balance(db, m.id))
    if body.amount > owed:
        raise HTTPException(409, f"Payment is more than the amount owed ({circ.fmt_naira(owed)}).")
    if db.scalar(select(LedgerEntry.id).where(LedgerEntry.kind == "payment", LedgerEntry.receipt_no == body.receipt_no)):
        raise HTTPException(409, "This receipt number has already been recorded.")
    e = LedgerEntry(member_id=m.id, kind="payment", amount=body.amount, method=body.method,
                    receipt_no=body.receipt_no, officer_id=officer.id, note="Payment received")
    db.add(e)
    db.flush()
    audit(db, officer, "fine.payment", "ledger", e.id,
          after={"member": m.id_number, "amount": body.amount, "method": body.method, "receipt": body.receipt_no})
    db.commit()
    return _ledger(db, m)


@router.post("/waivers")
def waive(body: WaiverIn, officer: Member = Depends(require_roles("head_librarian")), db: Session = Depends(get_db)):
    """FR-6.2: waivers are Head Librarian only and need a reason. Both are written to the audit log."""
    m = _find(db, body.member_id_number)
    owed = max(0, circ.member_balance(db, m.id))
    if body.amount > owed:
        raise HTTPException(409, f"Waiver is more than the amount owed ({circ.fmt_naira(owed)}).")
    e = LedgerEntry(member_id=m.id, kind="waiver", amount=body.amount, note=body.reason, officer_id=officer.id)
    db.add(e)
    db.flush()
    audit(db, officer, "fine.waiver", "ledger", e.id,
          after={"member": m.id_number, "amount": body.amount, "reason": body.reason})
    db.commit()
    return _ledger(db, m)


@router.get("/reconciliation")
def reconciliation(day: date | None = None, _: Member = Depends(DESK), db: Session = Depends(get_db)):
    """FIN-5: daily cash reconciliation by officer and payment method (WAT day)."""
    day = day or to_wat(utcnow()).date()
    start, end = day_bounds_utc(day)
    rows = db.execute(
        select(Member.name, LedgerEntry.method, func.count(), func.sum(LedgerEntry.amount))
        .join(Member, Member.id == LedgerEntry.officer_id)
        .where(LedgerEntry.kind == "payment", LedgerEntry.created_at >= start, LedgerEntry.created_at < end)
        .group_by(Member.name, LedgerEntry.method).order_by(Member.name)
    ).all()
    lines = [{"officer": n, "method": m, "receipts": c, "total": t} for n, m, c, t in rows]
    return {"date": day.isoformat(), "lines": lines, "grand_total": sum(l["total"] for l in lines)}
