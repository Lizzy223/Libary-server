"""Database models. Names follow SRS section 8.1. Money is stored as whole naira (integers)."""
from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text,
                        event)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .timeutil import utcnow

# ---- Enumerations kept as plain strings so SQLite and Postgres behave the same ----
ROLES = ["student", "lecturer", "circulation", "cataloguer", "head_librarian", "admin"]
STAFF_ROLES = {"circulation", "cataloguer", "head_librarian", "admin"}
MEMBER_TYPES = ["student", "academic_staff", "non_academic_staff", "visitor"]
MEMBER_STATUSES = ["active", "suspended", "expired"]

COPY_STATUSES = ["available", "on_loan", "reserved", "in_transit", "lost", "damaged", "in_repair", "withdrawn"]
COLLECTIONS = ["general", "reserve", "short_loan", "reference_only", "journals"]
FORMATS = ["print_book", "journal", "report", "past_question", "standard", "digital"]
SUBJECT_AREAS = ["maritime", "aviation", "rail", "road", "logistics", "policy", "other"]


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)


class Member(Base):
    __tablename__ = "members"
    id: Mapped[int] = mapped_column(primary_key=True)
    id_number: Mapped[str] = mapped_column(String(50), unique=True, index=True)  # matric or staff number
    name: Mapped[str] = mapped_column(String(150))
    member_type: Mapped[str] = mapped_column(String(30), default="student")
    role: Mapped[str] = mapped_column(String(30), default="student")
    programme: Mapped[str | None] = mapped_column(String(120))
    level: Mapped[str | None] = mapped_column(String(30))
    department: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(30))
    email: Mapped[str | None] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(20), default="active")
    expires_on: Mapped[datetime | None] = mapped_column(DateTime)
    password_hash: Mapped[str | None] = mapped_column(String(200))  # None = not yet activated
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    reset_code_hash: Mapped[str | None] = mapped_column(String(200))
    reset_expires: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Title(Base):
    __tablename__ = "titles"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    subtitle: Mapped[str | None] = mapped_column(String(300))
    authors: Mapped[str | None] = mapped_column(String(400))  # "A; B; C"
    edition: Mapped[str | None] = mapped_column(String(40))
    publisher: Mapped[str | None] = mapped_column(String(200))
    year: Mapped[int | None] = mapped_column(Integer, index=True)
    isbn: Mapped[str | None] = mapped_column(String(20), index=True)
    subjects: Mapped[str | None] = mapped_column(String(400))  # "A; B; C"
    subject_area: Mapped[str] = mapped_column(String(20), default="other", index=True)
    call_number: Mapped[str | None] = mapped_column(String(60), index=True)
    language: Mapped[str] = mapped_column(String(30), default="English")
    department: Mapped[str | None] = mapped_column(String(120), index=True)
    format: Mapped[str] = mapped_column(String(20), default="print_book", index=True)
    description: Mapped[str | None] = mapped_column(Text)
    retired: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    search_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    copies: Mapped[list["Copy"]] = relationship(back_populates="title")

    def refresh_search_text(self) -> None:
        parts = [self.title, self.subtitle, self.authors, self.subjects, self.isbn, self.call_number,
                 self.subject_area, self.department, self.publisher]
        self.search_text = " ".join(p for p in parts if p).lower()


class Copy(Base):
    __tablename__ = "copies"
    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id"), index=True)
    barcode: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="available", index=True)
    shelf_location: Mapped[str | None] = mapped_column(String(80))
    collection: Mapped[str] = mapped_column(String(20), default="general")
    acquired_on: Mapped[datetime | None] = mapped_column(DateTime, default=utcnow)
    cost: Mapped[int | None] = mapped_column(Integer)  # naira, replacement cost

    title: Mapped[Title] = relationship(back_populates="copies")


class Loan(Base):
    __tablename__ = "loans"
    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    copy_id: Mapped[int] = mapped_column(ForeignKey("copies.id"), index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    renewals: Mapped[int] = mapped_column(Integer, default=0)
    officer_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    override_reason: Mapped[str | None] = mapped_column(String(300))

    member: Mapped[Member] = relationship(foreign_keys=[member_id])
    copy: Mapped[Copy] = relationship()

    __table_args__ = (Index("ix_loans_open", "copy_id", "returned_at"),)


class Hold(Base):
    """Reservation. status: queued | ready | fulfilled | cancelled | expired"""
    __tablename__ = "holds"
    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    copy_id: Mapped[int | None] = mapped_column(ForeignKey("copies.id"))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    title: Mapped[Title] = relationship()
    member: Mapped[Member] = relationship()


class LedgerEntry(Base):
    """One ledger for charges, payments, waivers and adjustments (covers SRS Fine + Payment).
    kind: fine | replacement | payment | waiver | adjustment. amount is always positive;
    fine/replacement/adjustment add to what is owed, payment/waiver reduce it."""
    __tablename__ = "ledger"
    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    amount: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(String(300))
    method: Mapped[str | None] = mapped_column(String(30))  # cash | bank_transfer | pos
    receipt_no: Mapped[str | None] = mapped_column(String(60))
    officer_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    member: Mapped[Member] = relationship(foreign_keys=[member_id])


CHARGE_KINDS = ("fine", "replacement", "adjustment")
CREDIT_KINDS = ("payment", "waiver")


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    channel: Mapped[str] = mapped_column(String(10))  # in_app | email
    template: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(15), default="pending")  # pending | sent | failed | logged
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    dedupe_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"), index=True)
    actor_label: Mapped[str | None] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity: Mapped[str | None] = mapped_column(String(40), index=True)
    entity_id: Mapped[str | None] = mapped_column(String(40))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# The audit log is append-only (ADM-2, DR-3). Block edits and deletes at ORM level.
@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def _audit_is_immutable(*_):
    raise RuntimeError("Audit log entries cannot be modified or deleted")


class SearchLog(Base):
    __tablename__ = "search_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    query: Mapped[str] = mapped_column(String(300))
    result_count: Mapped[int] = mapped_column(Integer)
    member_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SyncReceipt(Base):
    """Idempotency record for offline desk transactions: the same client_id is never applied twice."""
    __tablename__ = "sync_receipts"
    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
