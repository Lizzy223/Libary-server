"""Notifications: every message is stored in-app; email rows are dispatched by a background job so
desk transactions stay fast. Without SMTP settings, emails are written to the server log instead."""
import logging
import smtplib
import string
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..models import Member, Notification
from ..timeutil import utcnow
from . import settings_store

log = logging.getLogger("library.notify")


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(db: Session, template: str, ctx: dict) -> tuple[str, str]:
    tpl = settings_store.get_all(db)["templates"][template]
    fmt = string.Formatter()
    return fmt.vformat(tpl["subject"], (), _Safe(ctx)), fmt.vformat(tpl["body"], (), _Safe(ctx))


def notify(db: Session, member: Member, template: str, ctx: dict, dedupe_key: str | None = None,
           in_app: bool = True) -> bool:
    """Queue notifications for a member. Returns False if this dedupe_key was already used."""
    if dedupe_key and db.scalar(select(Notification.id).where(Notification.dedupe_key == dedupe_key)):
        return False
    subject, body = render(db, template, {"name": member.name.split()[0], **ctx})
    if in_app:
        db.add(Notification(member_id=member.id, channel="in_app", template=template, subject=subject,
                            body=body, status="sent", sent_at=utcnow(), dedupe_key=dedupe_key))
    if member.email:
        db.add(Notification(member_id=member.id, channel="email", template=template, subject=subject,
                            body=body, status="pending",
                            dedupe_key=(dedupe_key + ":email") if dedupe_key else None))
    return True


def dispatch_pending_emails(db: Session, limit: int = 100) -> dict:
    """Send pending emails, retrying failures up to 3 times (NOT-6)."""
    cfg = get_config()
    rows = db.scalars(
        select(Notification)
        .where(Notification.channel == "email", Notification.status.in_(["pending", "failed"]),
               Notification.attempts < 3)
        .order_by(Notification.id).limit(limit)
    ).all()
    sent = failed = logged = 0
    for row in rows:
        member = db.get(Member, row.member_id)
        row.attempts += 1
        if not cfg.smtp_host:
            log.info("EMAIL (no SMTP configured) to=%s subject=%s body=%s", member.email, row.subject, row.body)
            row.status, row.sent_at = "logged", utcnow()
            logged += 1
            continue
        try:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = cfg.smtp_from, member.email, row.subject
            msg.set_content(row.body)
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as smtp:
                smtp.starttls()
                if cfg.smtp_user:
                    smtp.login(cfg.smtp_user, cfg.smtp_password)
                smtp.send_message(msg)
            row.status, row.sent_at = "sent", utcnow()
            sent += 1
        except Exception as exc:  # noqa: BLE001 - delivery failures must never crash the job
            log.warning("Email to %s failed: %s", member.email, exc)
            row.status = "failed"
            failed += 1
    return {"sent": sent, "logged": logged, "failed": failed}
