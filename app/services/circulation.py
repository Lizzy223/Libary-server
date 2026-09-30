"""Circulation business rules: issue, return, renew, holds and fines.

Design rule: every function validates first and mutates afterwards, so a RuleError never leaves
half-applied changes behind. This is what keeps offline sync safe without nested transactions."""
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import CHARGE_KINDS, CREDIT_KINDS, Copy, Hold, LedgerEntry, Loan, Member, Title
from ..timeutil import parse_hhmm, to_wat, utcnow, wat_date, wat_to_utc
from . import settings_store
from .audit import audit
from .notifications import notify


class RuleError(Exception):
    def __init__(self, message: str, code: str = "rule", blocks: list | None = None, status: int = 409):
        super().__init__(message)
        self.message, self.code, self.blocks, self.status = message, code, blocks or [], status


def fmt_date(dt: datetime) -> str:
    return to_wat(dt).strftime("%d/%m/%Y")


def fmt_naira(amount: int) -> str:
    return f"₦{amount:,}"


# ------------------------------------------------------------------ dates and fines
def is_closed(d: date, cfg: dict) -> bool:
    return d.weekday() in cfg["closure_weekdays"] or d.isoformat() in cfg["closure_dates"]


def compute_due(now: datetime, member_type: str, collection: str, cfg: dict) -> datetime:
    close = parse_hhmm(cfg["closing_time"])
    local = to_wat(now)
    if collection == "short_loan":  # BR-7: same day, one hour before closing
        due = datetime.combine(local.date(), close) - timedelta(hours=cfg["short_loan_hours_before_close"])
        if due <= local.replace(tzinfo=None):
            due = datetime.combine(local.date(), close)
        return wat_to_utc(due)
    d = local.date() + timedelta(days=cfg["loan_days"].get(member_type, 14))
    while is_closed(d, cfg):  # CIR-3: roll forward past closure days
        d += timedelta(days=1)
    return wat_to_utc(datetime.combine(d, close))


def days_overdue(loan: Loan, now: datetime) -> int:
    end = loan.returned_at or now
    if end <= loan.due_at:
        return 0
    return max(0, (wat_date(end) - wat_date(loan.due_at)).days)


def fine_for(loan: Loan, cfg: dict, now: datetime) -> int:
    cap = loan.copy.cost or cfg["fine_cap_default"]  # BR-9: capped at replacement cost
    return min(days_overdue(loan, now) * cfg["fine_per_day"], cap)


def upsert_fine(db: Session, loan: Loan, cfg: dict, now: datetime) -> int:
    """One fine entry per loan, updated daily while overdue and frozen on return (FIN-1)."""
    amount = fine_for(loan, cfg, now)
    if amount <= 0:
        return 0
    entry = db.scalar(select(LedgerEntry).where(LedgerEntry.loan_id == loan.id, LedgerEntry.kind == "fine"))
    if entry:
        if entry.amount != amount:
            entry.amount, entry.updated_at = amount, now
    else:
        db.add(LedgerEntry(member_id=loan.member_id, loan_id=loan.id, kind="fine", amount=amount,
                           note=f"Overdue: {days_overdue(loan, now)} day(s)", created_at=now, updated_at=now))
        notify(db, loan.member, "fine_posted",
               {"title": loan.copy.title.title, "amount": fmt_naira(amount)}, dedupe_key=f"fine:{loan.id}")
    return amount


def member_balance(db: Session, member_id: int) -> int:
    def total(kinds):
        return db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount), 0))
                         .where(LedgerEntry.member_id == member_id, LedgerEntry.kind.in_(kinds))) or 0
    return total(CHARGE_KINDS) - total(CREDIT_KINDS)


def open_loans(db: Session, member_id: int) -> list[Loan]:
    return list(db.scalars(select(Loan).where(Loan.member_id == member_id, Loan.returned_at.is_(None))
                           .order_by(Loan.due_at)))


# ------------------------------------------------------------------ blocks
def compute_blocks(db: Session, member: Member, cfg: dict, now: datetime, *, for_issue: bool = True) -> list[dict]:
    """Reasons a member cannot borrow. `overridable` False means no override is allowed."""
    blocks: list[dict] = []
    if member.status != "active":
        blocks.append({"code": "inactive", "message": f"Membership is {member.status}", "overridable": False})
    if member.expires_on and member.expires_on < now:
        blocks.append({"code": "expired", "message": "Membership has expired", "overridable": False})
    owed = max(0, member_balance(db, member.id))
    if owed > cfg["fine_block_threshold"]:
        blocks.append({"code": "fines", "overridable": True,
                       "message": f"Unpaid fines of {fmt_naira(owed)} exceed the limit of {fmt_naira(cfg['fine_block_threshold'])}"})
    loans = open_loans(db, member.id)
    if any(days_overdue(l, now) > cfg["overdue_block_days"] for l in loans):
        blocks.append({"code": "overdue", "overridable": True,
                       "message": f"An item is overdue by more than {cfg['overdue_block_days']} days"})
    limit = cfg["max_items"].get(member.member_type, 1)
    if for_issue and len(loans) >= limit:
        blocks.append({"code": "limit", "overridable": True, "message": f"Item limit reached ({limit})"})
    return blocks


# ------------------------------------------------------------------ issue / return / renew
def issue(db: Session, member: Member, copy: Copy, officer: Member, *, override_reason: str | None = None,
          now: datetime | None = None) -> Loan:
    now = now or utcnow()
    cfg = settings_store.get_all(db)

    if copy.status == "on_loan":
        raise RuleError("This copy is already on loan", "already_on_loan")
    if copy.collection == "reference_only":  # BR-8
        raise RuleError("Reference-only items cannot be borrowed", "reference_only")
    hold = None
    if copy.status == "reserved":
        hold = db.scalar(select(Hold).where(Hold.copy_id == copy.id, Hold.status == "ready"))
        if not hold or hold.member_id != member.id:
            raise RuleError("This copy is reserved for another member", "reserved_other")
    elif copy.status != "available":
        raise RuleError(f"This copy is {copy.status.replace('_', ' ')} and cannot be issued", "not_available")

    blocks = compute_blocks(db, member, cfg, now)
    if blocks:
        if any(not b["overridable"] for b in blocks):
            raise RuleError(next(b["message"] for b in blocks if not b["overridable"]), "blocked", blocks)
        if not override_reason:
            raise RuleError("Member is blocked from borrowing", "blocked", blocks)
        if officer.role not in ("circulation", "head_librarian"):
            raise RuleError("You are not authorised to override a block", "forbidden", status=403)

    loan = Loan(member_id=member.id, copy_id=copy.id, issued_at=now, officer_id=officer.id,
                due_at=compute_due(now, member.member_type, copy.collection, cfg),
                override_reason=override_reason if blocks else None)
    db.add(loan)
    copy.status = "on_loan"
    if hold:
        hold.status = "fulfilled"
    db.flush()
    if blocks:
        audit(db, officer, "loan.override", "loan", loan.id,
              after={"member": member.id_number, "barcode": copy.barcode, "reason": override_reason,
                     "blocks": [b["code"] for b in blocks]})
    audit(db, officer, "loan.issue", "loan", loan.id,
          after={"member": member.id_number, "barcode": copy.barcode, "due_at": loan.due_at})
    return loan


def return_copy(db: Session, copy: Copy, officer: Member, *, condition: str = "ok",
                now: datetime | None = None) -> dict:
    now = now or utcnow()
    cfg = settings_store.get_all(db)
    loan = db.scalar(select(Loan).where(Loan.copy_id == copy.id, Loan.returned_at.is_(None)))
    if not loan:
        raise RuleError("This copy is not on loan", "not_on_loan")

    loan.returned_at = now
    fine = upsert_fine(db, loan, cfg, now)
    hold = None
    if condition == "damaged":
        copy.status = "damaged"  # damage workflow: no hold routing until the copy is repaired
    else:
        hold = assign_copy_to_next_hold(db, copy, now, cfg)
        if not hold:
            copy.status = "available"
    audit(db, officer, "loan.return", "loan", loan.id,
          after={"member": loan.member.id_number, "barcode": copy.barcode, "fine": fine, "condition": condition})
    return {"loan": loan, "fine": fine, "hold": hold, "copy_status": copy.status}


def renew(db: Session, loan: Loan, actor: Member, now: datetime | None = None) -> Loan:
    now = now or utcnow()
    cfg = settings_store.get_all(db)
    member, copy = loan.member, loan.copy
    if loan.returned_at:
        raise RuleError("This loan is already closed", "closed")
    if copy.collection in ("short_loan", "reference_only"):
        raise RuleError("Short-loan items cannot be renewed", "no_renew")
    if loan.renewals >= cfg["max_renewals"].get(member.member_type, 0):
        raise RuleError("No renewals left for this item", "renew_limit")
    if now > loan.due_at:
        raise RuleError("Overdue items must be returned before they can be renewed", "overdue")
    waiting = db.scalar(select(func.count()).select_from(Hold)
                        .where(Hold.title_id == copy.title_id, Hold.status.in_(["queued", "ready"])))
    if waiting:
        raise RuleError("Another member is waiting for this title", "reserved")
    if any(b["code"] in ("fines", "inactive", "expired") for b in compute_blocks(db, member, cfg, now, for_issue=False)):
        raise RuleError("Renewal blocked: please clear fines or contact the library", "blocked")

    before = {"due_at": loan.due_at, "renewals": loan.renewals}
    loan.due_at = compute_due(now, member.member_type, copy.collection, cfg)
    loan.renewals += 1
    audit(db, actor, "loan.renew", "loan", loan.id, before=before, after={"due_at": loan.due_at, "renewals": loan.renewals})
    notify(db, member, "renewed", {"title": copy.title.title, "due_date": fmt_date(loan.due_at)})
    return loan


# ------------------------------------------------------------------ holds
def assign_copy_to_next_hold(db: Session, copy: Copy, now: datetime, cfg: dict) -> Hold | None:
    hold = db.scalar(select(Hold).where(Hold.title_id == copy.title_id, Hold.status == "queued")
                     .order_by(Hold.queued_at, Hold.id).limit(1))
    if not hold:
        return None
    hold.status, hold.copy_id, hold.ready_at = "ready", copy.id, now
    hold.expires_at = now + timedelta(hours=cfg["hold_pickup_hours"])
    copy.status = "reserved"
    notify(db, hold.member, "hold_ready", {"title": copy.title.title, "expires": fmt_date(hold.expires_at)},
           dedupe_key=f"hold-ready:{hold.id}")
    return hold


def release_copy(db: Session, copy: Copy, now: datetime, cfg: dict) -> None:
    if copy.status != "reserved":
        return
    if not assign_copy_to_next_hold(db, copy, now, cfg):
        copy.status = "available"


def borrowable_copies(db: Session, title_id: int) -> list[Copy]:
    return list(db.scalars(select(Copy).where(
        Copy.title_id == title_id, Copy.collection != "reference_only",
        Copy.status.notin_(["lost", "withdrawn"]))))


def place_hold(db: Session, member: Member, title: Title, now: datetime | None = None) -> Hold:
    now = now or utcnow()
    cfg = settings_store.get_all(db)
    if title.retired:
        raise RuleError("This title has been retired", "retired")
    copies = borrowable_copies(db, title.id)
    if not copies:
        raise RuleError("No borrowable copies exist for this title (reference-only or withdrawn)", "no_copies")
    shelf = next((c for c in copies if c.status == "available"), None)
    if shelf:
        where = f" at {shelf.shelf_location}" if shelf.shelf_location else ""
        raise RuleError(f"A copy is available on the shelf{where}. Please borrow it at the desk.", "available")
    blocks = [b for b in compute_blocks(db, member, cfg, now, for_issue=False)]
    if blocks:
        raise RuleError(blocks[0]["message"], "blocked", blocks)
    if db.scalar(select(Hold.id).where(Hold.title_id == title.id, Hold.member_id == member.id,
                                       Hold.status.in_(["queued", "ready"]))):
        raise RuleError("You already have a hold on this title", "duplicate")
    if any(l.copy.title_id == title.id for l in open_loans(db, member.id)):
        raise RuleError("You already have this title on loan", "on_loan")
    active = db.scalar(select(func.count()).select_from(Hold)
                       .where(Hold.member_id == member.id, Hold.status.in_(["queued", "ready"])))
    if active >= cfg["max_holds"]:
        raise RuleError(f"You can hold up to {cfg['max_holds']} titles at a time", "hold_limit")

    hold = Hold(title_id=title.id, member_id=member.id, queued_at=now, status="queued")
    db.add(hold)
    db.flush()
    audit(db, member, "hold.place", "hold", hold.id, after={"title_id": title.id})
    return hold


def cancel_hold(db: Session, hold: Hold, actor: Member, now: datetime | None = None) -> None:
    now = now or utcnow()
    if hold.status not in ("queued", "ready"):
        raise RuleError("This hold is no longer active", "closed")
    cfg = settings_store.get_all(db)
    was_ready, copy_id = hold.status == "ready", hold.copy_id
    hold.status = "cancelled"
    if was_ready and copy_id:
        release_copy(db, db.get(Copy, copy_id), now, cfg)
    audit(db, actor, "hold.cancel", "hold", hold.id)


def queue_position(db: Session, hold: Hold) -> int:
    if hold.status == "ready":
        return 0
    if hold.status != "queued":
        return -1
    ahead = db.scalar(select(func.count()).select_from(Hold).where(
        Hold.title_id == hold.title_id, Hold.status == "queued",
        (Hold.queued_at < hold.queued_at) | ((Hold.queued_at == hold.queued_at) & (Hold.id < hold.id))))
    return ahead + 1


# ------------------------------------------------------------------ background jobs
def expire_holds(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    cfg = settings_store.get_all(db)
    stale = db.scalars(select(Hold).where(Hold.status == "ready", Hold.expires_at < now)).all()
    for hold in stale:
        copy = db.get(Copy, hold.copy_id)
        hold.status = "expired"
        notify(db, hold.member, "hold_expired", {"title": copy.title.title}, dedupe_key=f"hold-expired:{hold.id}")
        release_copy(db, copy, now, cfg)
        audit(db, None, "hold.expire", "hold", hold.id, actor_label="system")
    return len(stale)


def accrue_fines(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    cfg = settings_store.get_all(db)
    count = 0
    for loan in db.scalars(select(Loan).where(Loan.returned_at.is_(None), Loan.due_at < now)):
        if upsert_fine(db, loan, cfg, now):
            count += 1
    return count


def send_reminders(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    cfg = settings_store.get_all(db)
    offsets = set(cfg["reminder_days_after_due"])
    sent = 0
    for loan in db.scalars(select(Loan).where(Loan.returned_at.is_(None))):
        if loan.copy.collection == "short_loan":
            continue
        after = (wat_date(now) - wat_date(loan.due_at)).days
        if after not in offsets:
            continue
        template = "due_soon" if after < 0 else "due_today" if after == 0 else "overdue"
        ctx = {"title": loan.copy.title.title, "due_date": fmt_date(loan.due_at), "days": after}
        if notify(db, loan.member, template, ctx, dedupe_key=f"loan:{loan.id}:{after}"):
            sent += 1
    return sent


def run_daily_jobs(db: Session) -> dict:
    from .notifications import dispatch_pending_emails
    result = {
        "holds_expired": expire_holds(db),
        "fines_updated": accrue_fines(db),
        "reminders_queued": send_reminders(db),
    }
    db.flush()
    result["email"] = dispatch_pending_emails(db)
    db.commit()
    return result
