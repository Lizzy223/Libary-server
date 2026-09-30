"""Dict serialisers. Datetimes go out as ISO-8601 UTC with a Z suffix; the UI renders them in WAT."""
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Copy, Hold, LedgerEntry, Loan, Member, Notification, Title
from .services import circulation as circ
from .services import settings_store


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def member_out(m: Member) -> dict:
    return {
        "id": m.id, "id_number": m.id_number, "name": m.name, "member_type": m.member_type, "role": m.role,
        "programme": m.programme, "level": m.level, "department": m.department, "phone": m.phone,
        "email": m.email, "status": m.status, "expires_on": iso(m.expires_on),
        "must_change_password": m.must_change_password, "consented": m.consent_at is not None,
        "activated": m.password_hash is not None,
    }


def availability_map(db: Session, title_ids: list[int]) -> dict[int, dict]:
    """Copy counts and shelf locations per title, in two grouped queries."""
    out = {tid: {"total_copies": 0, "available_copies": 0, "locations": [], "holds": 0} for tid in title_ids}
    if not title_ids:
        return out
    rows = db.execute(
        select(Copy.title_id, Copy.status, Copy.shelf_location, Copy.collection).where(Copy.title_id.in_(title_ids))
    ).all()
    for tid, status, shelf, collection in rows:
        if status in ("lost", "withdrawn"):
            continue
        info = out[tid]
        info["total_copies"] += 1
        if status == "available" and collection != "reference_only":
            info["available_copies"] += 1
        if status == "available" and shelf and shelf not in info["locations"]:
            info["locations"].append(shelf)
    holds = db.execute(
        select(Hold.title_id, func.count()).where(Hold.title_id.in_(title_ids), Hold.status.in_(["queued", "ready"]))
        .group_by(Hold.title_id)
    ).all()
    for tid, n in holds:
        out[tid]["holds"] = n
    return out


def title_out(t: Title, avail: dict | None = None) -> dict:
    data = {
        "id": t.id, "title": t.title, "subtitle": t.subtitle, "authors": t.authors, "edition": t.edition,
        "publisher": t.publisher, "year": t.year, "isbn": t.isbn, "subjects": t.subjects,
        "subject_area": t.subject_area, "call_number": t.call_number, "language": t.language,
        "department": t.department, "format": t.format, "description": t.description, "retired": t.retired,
    }
    data.update(avail or {"total_copies": 0, "available_copies": 0, "locations": [], "holds": 0})
    return data


def copy_out(c: Copy) -> dict:
    return {"id": c.id, "title_id": c.title_id, "barcode": c.barcode, "status": c.status,
            "shelf_location": c.shelf_location, "collection": c.collection, "cost": c.cost}


def loan_out(loan: Loan, now: datetime, cfg: dict | None = None) -> dict:
    late = circ.days_overdue(loan, now)
    is_overdue = loan.returned_at is None and now > loan.due_at
    out = {
        "id": loan.id, "title": loan.copy.title.title, "title_id": loan.copy.title_id,
        "authors": loan.copy.title.authors, "barcode": loan.copy.barcode, "collection": loan.copy.collection,
        "issued_at": iso(loan.issued_at), "due_at": iso(loan.due_at), "returned_at": iso(loan.returned_at),
        "renewals": loan.renewals, "is_overdue": is_overdue, "days_overdue": late,
        "member": {"id": loan.member.id, "id_number": loan.member.id_number, "name": loan.member.name},
    }
    if cfg and is_overdue:
        out["fine_estimate"] = circ.fine_for(loan, cfg, now)
    return out


def hold_out(db: Session, h: Hold) -> dict:
    return {
        "id": h.id, "status": h.status, "title_id": h.title_id, "title": h.title.title, "authors": h.title.authors,
        "queued_at": iso(h.queued_at), "ready_at": iso(h.ready_at), "expires_at": iso(h.expires_at),
        "position": circ.queue_position(db, h),
        "member": {"id": h.member.id, "id_number": h.member.id_number, "name": h.member.name},
    }


def ledger_out(e: LedgerEntry) -> dict:
    return {"id": e.id, "kind": e.kind, "amount": e.amount, "note": e.note, "method": e.method,
            "receipt_no": e.receipt_no, "loan_id": e.loan_id, "created_at": iso(e.created_at)}


def notification_out(n: Notification) -> dict:
    return {"id": n.id, "template": n.template, "subject": n.subject, "body": n.body,
            "read": n.read_at is not None, "created_at": iso(n.created_at)}


def member_summary(db: Session, m: Member, now: datetime) -> dict:
    """Everything the desk needs after scanning a member ID (UC-01 step 2)."""
    cfg = settings_store.get_all(db)
    loans = circ.open_loans(db, m.id)
    holds = db.scalars(select(Hold).where(Hold.member_id == m.id, Hold.status.in_(["queued", "ready"]))).all()
    return {
        "member": member_out(m),
        "loans": [loan_out(l, now, cfg) for l in loans],
        "holds": [hold_out(db, h) for h in holds],
        "fines_owed": max(0, circ.member_balance(db, m.id)),
        "blocks": circ.compute_blocks(db, m, cfg, now),
        "loan_limit": cfg["max_items"].get(m.member_type, 1),
    }
