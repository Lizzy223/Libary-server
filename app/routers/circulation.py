from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import STAFF_ROLES, Copy, Hold, Loan, Member, SyncReceipt, Title
from ..security import current_member, require_roles
from ..serializers import hold_out, iso, loan_out, member_summary
from ..services import circulation as circ
from ..services import settings_store
from ..timeutil import utcnow

router = APIRouter(tags=["circulation"])
DESK = require_roles("circulation", "head_librarian")


class IssueIn(BaseModel):
    member_id_number: str
    barcode: str
    override_reason: str | None = Field(default=None, max_length=300)


class ReturnIn(BaseModel):
    barcode: str
    condition: str = "ok"  # ok | damaged


class RenewIn(BaseModel):
    loan_id: int


class SyncTx(BaseModel):
    client_id: str = Field(min_length=8, max_length=64)
    type: str  # issue | return
    barcode: str
    member_id_number: str | None = None
    occurred_at: datetime
    override_reason: str | None = None
    condition: str = "ok"


class SyncIn(BaseModel):
    transactions: list[SyncTx] = Field(max_length=500)


def _fail(e: circ.RuleError):
    raise HTTPException(e.status, {"message": e.message, "code": e.code, "blocks": e.blocks})


def _member(db: Session, id_number: str) -> Member:
    m = db.scalar(select(Member).where(Member.id_number == id_number.strip().upper()))
    if not m:
        raise HTTPException(404, "No member found with that ID number.")
    return m


def _copy(db: Session, barcode: str) -> Copy:
    c = db.scalar(select(Copy).where(Copy.barcode == barcode.strip().upper()))
    if not c:
        raise HTTPException(404, f"No copy found with barcode {barcode.strip()}.")
    return c


def _return_payload(res: dict, now: datetime) -> dict:
    hold = res["hold"]
    instruction = "Place on the shelf"
    if hold:
        instruction = f"Place on the HOLD SHELF for {hold.member.name}"
    elif res["copy_status"] == "damaged":
        instruction = "Send to repair / damage workflow"
    return {"loan": loan_out(res["loan"], now), "fine": res["fine"], "instruction": instruction,
            "hold_for": hold.member.name if hold else None, "copy_status": res["copy_status"]}


@router.post("/circulation/issue")
def issue(body: IssueIn, officer: Member = Depends(DESK), db: Session = Depends(get_db)):
    member, copy = _member(db, body.member_id_number), _copy(db, body.barcode)
    now = utcnow()
    try:
        loan = circ.issue(db, member, copy, officer, override_reason=body.override_reason, now=now)
    except circ.RuleError as e:
        _fail(e)
    db.commit()
    return {"loan": loan_out(loan, now), "summary": member_summary(db, member, now)}


@router.post("/circulation/return")
def return_item(body: ReturnIn, officer: Member = Depends(DESK), db: Session = Depends(get_db)):
    if body.condition not in ("ok", "damaged"):
        raise HTTPException(422, "condition must be 'ok' or 'damaged'")
    copy, now = _copy(db, body.barcode), utcnow()
    try:
        res = circ.return_copy(db, copy, officer, condition=body.condition, now=now)
    except circ.RuleError as e:
        _fail(e)
    db.commit()
    return _return_payload(res, now)


@router.post("/circulation/renew")
def renew(body: RenewIn, actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    loan = db.get(Loan, body.loan_id)
    if not loan or (actor.role not in STAFF_ROLES and loan.member_id != actor.id):
        raise HTTPException(404, "Loan not found.")
    if actor.role in STAFF_ROLES and actor.role not in ("circulation", "head_librarian"):
        raise HTTPException(403, "You do not have permission to do this.")
    try:
        circ.renew(db, loan, actor)
    except circ.RuleError as e:
        _fail(e)
    db.commit()
    return loan_out(loan, utcnow(), settings_store.get_all(db))


@router.get("/circulation/loans")
def loans(member_id_number: str | None = None, overdue: bool = False, history: bool = False,
          page: int = Query(1, ge=1), page_size: int = Query(50, le=200),
          actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    now, cfg = utcnow(), settings_store.get_all(db)
    stmt = select(Loan).order_by(Loan.due_at if not history else Loan.issued_at.desc())
    if actor.role in STAFF_ROLES:
        if actor.role not in ("circulation", "head_librarian"):
            raise HTTPException(403, "You do not have permission to do this.")
        if member_id_number:
            stmt = stmt.where(Loan.member_id == _member(db, member_id_number).id)
    else:
        stmt = stmt.where(Loan.member_id == actor.id)  # own records only
    if not history:
        stmt = stmt.where(Loan.returned_at.is_(None))
    if overdue:
        stmt = stmt.where(Loan.returned_at.is_(None), Loan.due_at < now)
    rows = db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    return [loan_out(l, now, cfg) for l in rows]


# ------------------------------------------------------------------ holds
@router.post("/holds/{title_id}")
def place_hold(title_id: int, actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    title = db.get(Title, title_id)
    if not title:
        raise HTTPException(404, "Title not found.")
    try:
        hold = circ.place_hold(db, actor, title)
    except circ.RuleError as e:
        _fail(e)
    db.commit()
    return hold_out(db, hold)


@router.get("/holds/mine")
def my_holds(actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    rows = db.scalars(select(Hold).where(Hold.member_id == actor.id, Hold.status.in_(["queued", "ready"]))
                      .order_by(Hold.queued_at)).all()
    return [hold_out(db, h) for h in rows]


@router.get("/holds")
def all_holds(status: str = "active", _: Member = Depends(DESK), db: Session = Depends(get_db)):
    stmt = select(Hold).order_by(Hold.queued_at)
    stmt = stmt.where(Hold.status.in_(["queued", "ready"])) if status == "active" else stmt.where(Hold.status == status)
    return [hold_out(db, h) for h in db.scalars(stmt.limit(200))]


@router.delete("/holds/{hold_id}")
def cancel_hold(hold_id: int, actor: Member = Depends(current_member), db: Session = Depends(get_db)):
    hold = db.get(Hold, hold_id)
    if not hold or (actor.role not in STAFF_ROLES and hold.member_id != actor.id):
        raise HTTPException(404, "Hold not found.")
    if actor.role in STAFF_ROLES and actor.role not in ("circulation", "head_librarian"):
        raise HTTPException(403, "You do not have permission to do this.")
    try:
        circ.cancel_hold(db, hold, actor)
    except circ.RuleError as e:
        _fail(e)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ offline sync (OFF-2, OFF-3, NFR-8)
@router.post("/circulation/sync")
def sync(body: SyncIn, officer: Member = Depends(DESK), db: Session = Depends(get_db)):
    """Replays desk transactions captured offline, oldest first. Each client_id is applied at most once,
    so a retry after a dropped connection never duplicates a loan. Failures come back as conflicts."""
    results = []
    for tx in sorted(body.transactions, key=lambda t: t.occurred_at):
        seen = db.get(SyncReceipt, tx.client_id)
        if seen:
            results.append({**seen.result, "client_id": tx.client_id, "status": "duplicate"})
            continue
        when = tx.occurred_at
        when = when.astimezone(timezone.utc).replace(tzinfo=None) if when.tzinfo else when
        when = min(when, utcnow())  # never trust a desk clock that is ahead of the server
        try:
            copy = _copy(db, tx.barcode)
            if tx.type == "issue":
                if not tx.member_id_number:
                    raise circ.RuleError("member_id_number is required for issue", "invalid")
                loan = circ.issue(db, _member(db, tx.member_id_number), copy, officer,
                                  override_reason=tx.override_reason, now=when)
                result = {"status": "ok", "message": f"Issued {copy.title.title} to {tx.member_id_number}",
                          "due_at": iso(loan.due_at)}
            elif tx.type == "return":
                res = circ.return_copy(db, copy, officer, condition=tx.condition, now=when)
                result = {"status": "ok", "message": f"Returned {copy.title.title}", "fine": res["fine"]}
            else:
                raise circ.RuleError("type must be 'issue' or 'return'", "invalid")
        except circ.RuleError as e:
            result = {"status": "conflict", "message": e.message, "code": e.code}
        except HTTPException as e:
            result = {"status": "conflict", "message": str(e.detail), "code": "not_found"}
        db.add(SyncReceipt(client_id=tx.client_id, result=result))
        db.commit()
        results.append({**result, "client_id": tx.client_id})
    return {"results": results}
