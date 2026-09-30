from contextvars import ContextVar
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..models import AuditLog, Member

request_ip: ContextVar[str | None] = ContextVar("request_ip", default=None)


def _clean(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def snap(obj: Any, fields: list[str]) -> dict:
    return {f: _clean(getattr(obj, f, None)) for f in fields}


def audit(db: Session, actor: Member | None, action: str, entity: str | None = None,
          entity_id: Any = None, before: dict | None = None, after: dict | None = None,
          actor_label: str | None = None) -> None:
    db.add(AuditLog(
        actor_id=actor.id if actor else None,
        actor_label=(actor.id_number if actor else actor_label) or "system",
        action=action, entity=entity,
        entity_id=str(entity_id) if entity_id is not None else None,
        before=_clean(before) if before else None,
        after=_clean(after) if after else None,
        ip=request_ip.get(),
    ))
