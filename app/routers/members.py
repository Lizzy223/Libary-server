from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import MEMBER_STATUSES, MEMBER_TYPES, ROLES, STAFF_ROLES, Member
from ..security import any_staff, current_member, hash_password, require_roles
from ..serializers import member_out, member_summary
from ..services import circulation as circ
from ..services.audit import audit, snap
from ..timeutil import utcnow
from .files import read_rows

router = APIRouter(prefix="/members", tags=["members"])

TYPE_TO_ROLE = {"student": "student", "academic_staff": "lecturer", "non_academic_staff": "circulation",
                "visitor": "student"}
AUDIT_FIELDS = ["id_number", "name", "member_type", "role", "status", "department", "email", "phone"]


class MemberIn(BaseModel):
    id_number: str = Field(min_length=2, max_length=50)
    name: str = Field(min_length=2, max_length=150)
    member_type: str = "student"
    role: str | None = None
    programme: str | None = None
    level: str | None = None
    department: str | None = None
    phone: str | None = None
    email: str | None = None
    temporary_password: str | None = Field(default=None, min_length=8)
    expires_on: str | None = None  # ISO date, for visitors (MEM-3)


class MemberPatch(BaseModel):
    name: str | None = None
    member_type: str | None = None
    role: str | None = None
    status: str | None = None
    programme: str | None = None
    level: str | None = None
    department: str | None = None
    phone: str | None = None
    email: str | None = None


class SelfPatch(BaseModel):
    phone: str | None = None
    email: str | None = None


@router.get("")
def list_members(q: str = "", role: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(25, le=100),
                 _: Member = Depends(any_staff), db: Session = Depends(get_db)):
    stmt = select(Member)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Member.name.ilike(like), Member.id_number.ilike(like)))
    if role:
        stmt = stmt.where(Member.role == role)
    rows = db.scalars(stmt.order_by(Member.name).offset((page - 1) * page_size).limit(page_size)).all()
    return {"results": [member_out(m) for m in rows]}


@router.post("")
def create_member(body: MemberIn, actor: Member = Depends(require_roles("circulation", "head_librarian", "admin")),
                  db: Session = Depends(get_db)):
    if body.member_type not in MEMBER_TYPES:
        raise HTTPException(422, "Unknown member type.")
    role = TYPE_TO_ROLE[body.member_type]
    if body.role and body.role != role:
        if actor.role != "admin":
            raise HTTPException(403, "Only an administrator can assign staff roles.")
        if body.role not in ROLES:
            raise HTTPException(422, "Unknown role.")
        role = body.role
    idn = body.id_number.strip().upper()
    if db.scalar(select(Member.id).where(Member.id_number == idn)):
        raise HTTPException(409, "A member with this ID number already exists.")
    m = Member(id_number=idn, name=body.name.strip(), member_type=body.member_type, role=role,
               programme=body.programme, level=body.level, department=body.department, phone=body.phone,
               email=body.email, must_change_password=role in STAFF_ROLES or bool(body.temporary_password))
    if body.temporary_password:
        m.password_hash = hash_password(body.temporary_password)
    if body.expires_on:
        try:
            m.expires_on = datetime.fromisoformat(body.expires_on)
        except ValueError:
            raise HTTPException(422, "expires_on must be an ISO date (YYYY-MM-DD).") from None
    db.add(m)
    db.flush()
    audit(db, actor, "member.create", "member", m.id, after=snap(m, AUDIT_FIELDS))
    db.commit()
    return member_out(m)


@router.post("/import")
async def import_members(dry_run: bool = True, file: UploadFile = File(...),
                         actor: Member = Depends(require_roles("head_librarian", "admin")),
                         db: Session = Depends(get_db)):
    """MEM-2 / DR-4. Columns: id_number, name, member_type, programme, level, department, phone, email, status.
    Imported members have no password; they activate through 'Forgot password' using their email."""
    rows = await read_rows(file)
    created = updated = 0
    errors: list[dict] = []
    seen: set[str] = set()
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        idn = (row.get("id_number") or row.get("matric_number") or "").strip().upper()
        name = (row.get("name") or "").strip()
        mtype = (row.get("member_type") or "student").strip().lower()
        status = (row.get("status") or "active").strip().lower()
        problem = None
        if not idn or not name:
            problem = "id_number and name are required"
        elif mtype not in MEMBER_TYPES:
            problem = f"member_type must be one of {', '.join(MEMBER_TYPES)}"
        elif status not in MEMBER_STATUSES:
            problem = f"status must be one of {', '.join(MEMBER_STATUSES)}"
        elif idn in seen:
            problem = "duplicate id_number inside this file"
        if problem:
            errors.append({"row": i, "id_number": idn, "error": problem})
            continue
        seen.add(idn)
        existing = db.scalar(select(Member).where(Member.id_number == idn))
        fields = dict(name=name, member_type=mtype, programme=row.get("programme") or None,
                      level=row.get("level") or None, department=row.get("department") or None,
                      phone=row.get("phone") or None, email=row.get("email") or None, status=status)
        if existing:
            if existing.role in STAFF_ROLES:
                errors.append({"row": i, "id_number": idn, "error": "staff accounts cannot be changed by import"})
                continue
            for k, v in fields.items():
                setattr(existing, k, v)
            updated += 1
        else:
            db.add(Member(id_number=idn, role=TYPE_TO_ROLE[mtype], **fields))
            created += 1
    if dry_run:
        db.rollback()
    else:
        audit(db, actor, "member.import", "member", None, after={"created": created, "updated": updated, "errors": len(errors)})
        db.commit()
    return {"dry_run": dry_run, "created": created, "updated": updated, "errors": errors, "rows_read": len(rows)}


@router.patch("/me")
def update_me(body: SelfPatch, member: Member = Depends(current_member), db: Session = Depends(get_db)):
    """MEM-6: members correct their own contact details."""
    before = snap(member, ["phone", "email"])
    if body.phone is not None:
        member.phone = body.phone.strip() or None
    if body.email is not None:
        member.email = body.email.strip() or None
    audit(db, member, "member.self_update", "member", member.id, before=before, after=snap(member, ["phone", "email"]))
    db.commit()
    return member_out(member)


@router.get("/lookup/{id_number:path}")
def lookup(id_number: str, _: Member = Depends(require_roles("circulation", "head_librarian")),
           db: Session = Depends(get_db)):
    m = db.scalar(select(Member).where(Member.id_number == id_number.strip().upper()))
    if not m:
        raise HTTPException(404, "No member found with that ID number.")
    return member_summary(db, m, utcnow())


@router.patch("/{member_id}")
def update_member(member_id: int, body: MemberPatch, actor: Member = Depends(require_roles("head_librarian", "admin")),
                  db: Session = Depends(get_db)):
    m = db.get(Member, member_id)
    if not m:
        raise HTTPException(404, "Member not found.")
    data = body.model_dump(exclude_unset=True)
    if ("role" in data or (m.role in STAFF_ROLES)) and actor.role != "admin":
        raise HTTPException(403, "Only an administrator can change roles or staff accounts.")
    if "role" in data and data["role"] not in ROLES:
        raise HTTPException(422, "Unknown role.")
    if "member_type" in data and data["member_type"] not in MEMBER_TYPES:
        raise HTTPException(422, "Unknown member type.")
    if "status" in data and data["status"] not in MEMBER_STATUSES:
        raise HTTPException(422, "Unknown status.")
    if data.get("status") in ("suspended", "expired") and circ.open_loans(db, m.id):
        # MEM-4: suspension only after clearance checks - the desk must resolve open loans first
        raise HTTPException(409, "Member still has items on loan. Resolve them before suspending or expiring.")
    if data.get("role") and data["role"] in STAFF_ROLES and m.role not in STAFF_ROLES:
        m.must_change_password = True
    before = snap(m, AUDIT_FIELDS)
    for k, v in data.items():
        setattr(m, k, v)
    audit(db, actor, "member.update", "member", m.id, before=before, after=snap(m, AUDIT_FIELDS))
    db.commit()
    return member_out(m)
