import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Member, Notification
from ..security import current_member, hash_password, make_token, verify_password
from ..serializers import member_out, notification_out
from ..services.audit import audit
from ..services.notifications import notify
from ..timeutil import utcnow

router = APIRouter(prefix="/auth", tags=["auth"])

MAX_ATTEMPTS = 5              # AUTH-4
LOCK_FOR = timedelta(minutes=15)


class LoginIn(BaseModel):
    id_number: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=100)


class ForgotIn(BaseModel):
    id_number: str


class ResetIn(BaseModel):
    id_number: str
    code: str
    new_password: str = Field(min_length=8, max_length=100)


def _find(db: Session, id_number: str) -> Member | None:
    return db.scalar(select(Member).where(Member.id_number == id_number.strip().upper()))


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    member = _find(db, body.id_number)
    generic = HTTPException(401, "Incorrect ID number or password.")
    if not member:
        raise generic
    now = utcnow()
    if member.locked_until and member.locked_until > now:
        mins = int((member.locked_until - now).total_seconds() // 60) + 1
        raise HTTPException(423, f"Too many failed attempts. Try again in {mins} minute(s).")
    if member.password_hash is None:
        raise HTTPException(403, "This account is not activated yet. Use 'Forgot password' to set a password.")
    if not verify_password(body.password, member.password_hash):
        member.failed_attempts += 1
        if member.failed_attempts >= MAX_ATTEMPTS:
            member.locked_until, member.failed_attempts = now + LOCK_FOR, 0
            audit(db, None, "auth.locked", "member", member.id, actor_label=member.id_number)
        db.commit()
        raise generic
    member.failed_attempts, member.locked_until = 0, None
    audit(db, member, "auth.login", "member", member.id)
    db.commit()
    return {"access_token": make_token(member), "member": member_out(member)}


@router.get("/me")
def me(member: Member = Depends(current_member)):
    return member_out(member)


@router.post("/consent")
def consent(member: Member = Depends(current_member), db: Session = Depends(get_db)):
    """MEM-7 / NDPA: record consent to data processing at first login."""
    member.consent_at = utcnow()
    audit(db, member, "privacy.consent", "member", member.id)
    db.commit()
    return member_out(member)


@router.post("/change-password")
def change_password(body: ChangePasswordIn, member: Member = Depends(current_member), db: Session = Depends(get_db)):
    if not verify_password(body.current_password, member.password_hash):
        raise HTTPException(400, "Current password is incorrect.")
    if body.new_password == body.current_password:
        raise HTTPException(400, "Choose a different password from the current one.")
    member.password_hash = hash_password(body.new_password)
    member.must_change_password = False
    audit(db, member, "auth.password_change", "member", member.id)
    db.commit()
    return {"access_token": make_token(member), "member": member_out(member)}


@router.post("/forgot-password")
def forgot_password(body: ForgotIn, db: Session = Depends(get_db)):
    """AUTH-2. Always answers the same way so IDs cannot be probed."""
    member = _find(db, body.id_number)
    if member and member.email:
        code = f"{secrets.randbelow(10**6):06d}"
        member.reset_code_hash = hash_password(code)
        member.reset_expires = utcnow() + timedelta(minutes=15)
        notify(db, member, "password_reset", {"code": code}, in_app=False)
        audit(db, None, "auth.reset_requested", "member", member.id, actor_label=member.id_number)
        db.commit()
    return {"message": "If the account exists and has an email address, a reset code has been sent."}


@router.post("/reset-password")
def reset_password(body: ResetIn, db: Session = Depends(get_db)):
    member = _find(db, body.id_number)
    bad = HTTPException(400, "The code is invalid or has expired.")
    if not member or not member.reset_code_hash or not member.reset_expires or member.reset_expires < utcnow():
        raise bad
    if not verify_password(body.code.strip(), member.reset_code_hash):
        raise bad
    member.password_hash = hash_password(body.new_password)
    member.reset_code_hash = member.reset_expires = None
    member.failed_attempts, member.locked_until = 0, None
    member.must_change_password = False
    audit(db, None, "auth.reset_done", "member", member.id, actor_label=member.id_number)
    db.commit()
    return {"message": "Password updated. You can now sign in."}


@router.get("/notifications")
def my_notifications(member: Member = Depends(current_member), db: Session = Depends(get_db)):
    rows = db.scalars(select(Notification).where(Notification.member_id == member.id, Notification.channel == "in_app")
                      .order_by(Notification.id.desc()).limit(50)).all()
    return [notification_out(n) for n in rows]


@router.post("/notifications/read-all")
def read_all(member: Member = Depends(current_member), db: Session = Depends(get_db)):
    for n in db.scalars(select(Notification).where(Notification.member_id == member.id,
                                                   Notification.channel == "in_app", Notification.read_at.is_(None))):
        n.read_at = utcnow()
    db.commit()
    return {"ok": True}
