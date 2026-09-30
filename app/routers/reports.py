from collections import Counter
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CHARGE_KINDS, CREDIT_KINDS, Copy, Hold, LedgerEntry, Loan, Member, SearchLog, Title
from ..security import any_staff, require_roles
from ..services import circulation as circ
from ..services import settings_store
from ..timeutil import day_bounds_utc, utcnow, wat_date
from .files import csv_response_body

router = APIRouter(prefix="/reports", tags=["reports"])
HEAD = require_roles("head_librarian")
DESK_OR_HEAD = require_roles("circulation", "head_librarian")


def _range(date_from: date | None, date_to: date | None) -> tuple[datetime, datetime, date, date]:
    end_d = date_to or wat_date(utcnow())
    start_d = date_from or end_d - timedelta(days=30)
    return day_bounds_utc(start_d)[0], day_bounds_utc(end_d)[1], start_d, end_d


def _out(title: str, columns: list[str], rows: list[list], fmt: str, extra: dict | None = None):
    """RPT-3: every report is available as JSON (for the screen) or CSV (for Excel). PDF = browser print."""
    if fmt == "csv":
        slug = title.lower().replace(" ", "_")
        return Response(csv_response_body(columns, rows), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{slug}.csv"'})
    return {"title": title, "columns": columns, "rows": rows, **(extra or {})}


@router.get("/dashboard")
def dashboard(_: Member = Depends(any_staff), db: Session = Depends(get_db)):
    now = utcnow()
    today_start, today_end = day_bounds_utc(wat_date(now))
    scalar = lambda stmt: db.scalar(stmt) or 0  # noqa: E731
    top = db.execute(
        select(Title.title, func.count(Loan.id).label("n")).join(Copy, Copy.title_id == Title.id)
        .join(Loan, Loan.copy_id == Copy.id).where(Loan.issued_at >= now - timedelta(days=30))
        .group_by(Title.id, Title.title).order_by(func.count(Loan.id).desc()).limit(5)
    ).all()
    charges = scalar(select(func.sum(LedgerEntry.amount)).where(LedgerEntry.kind.in_(CHARGE_KINDS)))
    credits = scalar(select(func.sum(LedgerEntry.amount)).where(LedgerEntry.kind.in_(CREDIT_KINDS)))
    return {
        "loans_today": scalar(select(func.count()).select_from(Loan).where(Loan.issued_at >= today_start, Loan.issued_at < today_end)),
        "active_loans": scalar(select(func.count()).select_from(Loan).where(Loan.returned_at.is_(None))),
        "overdue": scalar(select(func.count()).select_from(Loan).where(Loan.returned_at.is_(None), Loan.due_at < now)),
        "holds": scalar(select(func.count()).select_from(Hold).where(Hold.status.in_(["queued", "ready"]))),
        "active_members": scalar(select(func.count()).select_from(Member).where(Member.status == "active")),
        "fines_collected_today": scalar(select(func.sum(LedgerEntry.amount)).where(
            LedgerEntry.kind == "payment", LedgerEntry.created_at >= today_start, LedgerEntry.created_at < today_end)),
        "fines_outstanding": max(0, charges - credits),
        "top_titles": [{"title": t, "loans": n} for t, n in top],
        "titles": scalar(select(func.count()).select_from(Title).where(Title.retired.is_(False))),
        "copies": scalar(select(func.count()).select_from(Copy)),
    }


@router.get("/circulation")
def circulation_report(date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),
                       group_by: str = Query("department", pattern="^(department|programme|day)$"),
                       format: str = "json", _: Member = Depends(HEAD), db: Session = Depends(get_db)):
    start, end, sd, ed = _range(date_from, date_to)
    if group_by == "day":
        counts = Counter(wat_date(d).isoformat() for (d,) in db.execute(
            select(Loan.issued_at).where(Loan.issued_at >= start, Loan.issued_at < end)).all())
        rows = [[k, v] for k, v in sorted(counts.items())]
    else:
        col = Member.department if group_by == "department" else Member.programme
        rows = [[g or "Unassigned", n] for g, n in db.execute(
            select(col, func.count(Loan.id)).join(Member, Member.id == Loan.member_id)
            .where(Loan.issued_at >= start, Loan.issued_at < end).group_by(col).order_by(func.count(Loan.id).desc())).all()]
    return _out(f"Circulation by {group_by}", [group_by.title(), "Loans"], rows, format, {"from": sd.isoformat(), "to": ed.isoformat()})


@router.get("/overdue")
def overdue_report(format: str = "json", _: Member = Depends(DESK_OR_HEAD), db: Session = Depends(get_db)):
    now, cfg = utcnow(), settings_store.get_all(db)
    loans = db.scalars(select(Loan).where(Loan.returned_at.is_(None), Loan.due_at < now).order_by(Loan.due_at)).all()
    rows = [[l.member.id_number, l.member.name, l.copy.title.title, l.copy.barcode, circ.fmt_date(l.due_at),
             circ.days_overdue(l, now), circ.fine_for(l, cfg, now)] for l in loans]
    return _out("Overdue items", ["ID number", "Member", "Title", "Barcode", "Due date", "Days overdue", "Fine (NGN)"], rows, format)


@router.get("/top-titles")
def top_titles(date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),
               unused: bool = False, format: str = "json", _: Member = Depends(HEAD), db: Session = Depends(get_db)):
    start, end, sd, ed = _range(date_from, date_to)
    loan_counts = dict(db.execute(
        select(Copy.title_id, func.count(Loan.id)).join(Loan, Loan.copy_id == Copy.id)
        .where(Loan.issued_at >= start, Loan.issued_at < end).group_by(Copy.title_id)).all())
    titles = db.scalars(select(Title).where(Title.retired.is_(False))).all()
    span = {"from": sd.isoformat(), "to": ed.isoformat()}
    if unused:
        rows = [[t.title, t.authors or "", t.department or "", t.year or ""] for t in titles if t.id not in loan_counts][:200]
        return _out("Unused titles", ["Title", "Authors", "Department", "Year"], rows, format, span)
    by_id = {t.id: t for t in titles}
    ranked = sorted(loan_counts.items(), key=lambda kv: -kv[1])[:50]
    rows = [[by_id[i].title, by_id[i].authors or "", n] for i, n in ranked if i in by_id]
    return _out("Top titles", ["Title", "Authors", "Loans"], rows, format, span)


@router.get("/lost")
def lost_items(format: str = "json", _: Member = Depends(HEAD), db: Session = Depends(get_db)):
    rows = [[c.barcode, c.title.title, c.shelf_location or "", c.cost or ""] for c in db.scalars(
        select(Copy).where(Copy.status == "lost").order_by(Copy.id))]
    return _out("Lost items", ["Barcode", "Title", "Last shelf", "Cost (NGN)"], rows, format)


@router.get("/fines")
def fines_report(date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),
                 format: str = "json", _: Member = Depends(HEAD), db: Session = Depends(get_db)):
    start, end, sd, ed = _range(date_from, date_to)
    rows = [[k, n, t or 0] for k, n, t in db.execute(
        select(LedgerEntry.kind, func.count(), func.sum(LedgerEntry.amount))
        .where(LedgerEntry.created_at >= start, LedgerEntry.created_at < end).group_by(LedgerEntry.kind)).all()]
    return _out("Fines and payments", ["Type", "Entries", "Total (NGN)"], rows, format, {"from": sd.isoformat(), "to": ed.isoformat()})


@router.get("/member-activity")
def member_activity(date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),
                    format: str = "json", _: Member = Depends(HEAD), db: Session = Depends(get_db)):
    start, end, sd, ed = _range(date_from, date_to)
    rows = [[i, n, d or "", c] for i, n, d, c in db.execute(
        select(Member.id_number, Member.name, Member.department, func.count(Loan.id)).join(Loan, Loan.member_id == Member.id)
        .where(Loan.issued_at >= start, Loan.issued_at < end).group_by(Member.id).order_by(func.count(Loan.id).desc()).limit(100)).all()]
    return _out("Member activity", ["ID number", "Name", "Department", "Loans"], rows, format, {"from": sd.isoformat(), "to": ed.isoformat()})


def demand_data(db: Session) -> dict:
    """Evidence for purchasing decisions (FR-7.3): waitlists, failed searches, heavily used short-stock titles."""
    since = utcnow() - timedelta(days=90)
    copies = dict(db.execute(select(Copy.title_id, func.count()).where(Copy.status.notin_(["lost", "withdrawn"])).group_by(Copy.title_id)).all())
    waitlists = db.execute(
        select(Title.id, Title.title, func.count(Hold.id)).join(Hold, Hold.title_id == Title.id)
        .where(Hold.status.in_(["queued", "ready"])).group_by(Title.id, Title.title).order_by(func.count(Hold.id).desc()).limit(10)).all()
    failed = db.execute(
        select(SearchLog.query, func.count()).where(SearchLog.result_count == 0, SearchLog.created_at >= since)
        .group_by(SearchLog.query).order_by(func.count().desc()).limit(10)).all()
    usage = db.execute(
        select(Title.id, Title.title, func.count(Loan.id)).join(Copy, Copy.title_id == Title.id).join(Loan, Loan.copy_id == Copy.id)
        .where(Loan.issued_at >= since).group_by(Title.id, Title.title).order_by(func.count(Loan.id).desc()).limit(25)).all()
    heavy = [{"title": t, "loans_90d": n, "copies": copies.get(i, 0), "loans_per_copy": round(n / max(1, copies.get(i, 0)), 1)}
             for i, t, n in usage]
    heavy.sort(key=lambda r: -r["loans_per_copy"])
    return {
        "waitlists": [{"title": t, "waiting": n, "copies": copies.get(i, 0)} for i, t, n in waitlists],
        "failed_searches": [{"query": q, "times": n} for q, n in failed],
        "heavily_used": heavy[:10],
        "window_days": 90,
    }


@router.get("/demand")
def demand_report(format: str = "json", _: Member = Depends(HEAD), db: Session = Depends(get_db)):
    d = demand_data(db)
    if format == "csv":
        rows = ([["waitlist", r["title"], r["waiting"], r["copies"]] for r in d["waitlists"]]
                + [["failed_search", r["query"], r["times"], ""] for r in d["failed_searches"]]
                + [["heavily_used", r["title"], r["loans_90d"], r["copies"]] for r in d["heavily_used"]])
        return _out("Demand report", ["Signal", "Item", "Count", "Copies"], rows, "csv")
    return {"title": "Demand report", **d}
