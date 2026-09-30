from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_config
from ..database import SessionLocal, get_db, is_sqlite
from ..models import AuditLog, Member
from ..security import require_roles
from ..serializers import iso
from ..services import circulation as circ
from ..services import grok, settings_store
from ..services.audit import audit
from ..timeutil import day_bounds_utc, utcnow

router = APIRouter(prefix="/admin", tags=["admin"])
ADMINS = require_roles("head_librarian", "admin")

_last_job: dict = {"at": None, "result": None}


class SettingsIn(BaseModel):
    values: dict


@router.get("/settings")
def get_settings(_: Member = Depends(ADMINS), db: Session = Depends(get_db)):
    return settings_store.get_all(db)


@router.put("/settings")
def put_settings(body: SettingsIn, actor: Member = Depends(ADMINS), db: Session = Depends(get_db)):
    try:
        changed = settings_store.update(db, body.values)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if changed:
        audit(db, actor, "settings.update", "settings", None, after=changed)
    db.commit()
    return settings_store.get_all(db)


@router.get("/audit")
def audit_log(actor: str | None = None, action: str | None = None,
              date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),
              page: int = Query(1, ge=1), page_size: int = Query(50, le=200),
              _: Member = Depends(ADMINS), db: Session = Depends(get_db)):
    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if actor:
        stmt = stmt.where(AuditLog.actor_label.ilike(f"%{actor}%"))
    if action:
        stmt = stmt.where(AuditLog.action.ilike(f"%{action}%"))
    if date_from:
        stmt = stmt.where(AuditLog.created_at >= day_bounds_utc(date_from)[0])
    if date_to:
        stmt = stmt.where(AuditLog.created_at < day_bounds_utc(date_to)[1])
    rows = db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).all()
    return [{"id": r.id, "at": iso(r.created_at), "actor": r.actor_label, "action": r.action, "entity": r.entity,
             "entity_id": r.entity_id, "before": r.before, "after": r.after, "ip": r.ip} for r in rows]


def run_jobs_once() -> dict:
    """Used by the scheduler and by the manual 'Run jobs' button."""
    with SessionLocal() as db:
        result = circ.run_daily_jobs(db)
    _last_job.update(at=iso(utcnow()), result=result)
    return result


@router.post("/jobs/run")
def run_jobs(_: Member = Depends(ADMINS)):
    return run_jobs_once()


@router.get("/system")
def system(_: Member = Depends(ADMINS)):
    cfg = get_config()
    return {
        "environment": cfg.app_env,
        "database": "sqlite (local file)" if is_sqlite else "postgresql",
        "ai_configured": grok.is_configured(), "ai_model": cfg.grok_model,
        "smtp_configured": bool(cfg.smtp_host), "scheduler_enabled": cfg.run_scheduler,
        "last_job_run": _last_job,
        "backups": ("Managed by your database host (for example Neon or Supabase point-in-time restore). "
                    "For SQLite, copy the .db file daily."),
    }
