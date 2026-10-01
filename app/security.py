from datetime import timedelta, datetime, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import get_config
from .database import get_db
from .models import STAFF_ROLES, Member
from .timeutil import utcnow

bearer = HTTPBearer(auto_error=False)
ALGO = "HS256"
STAFF_SESSION = timedelta(minutes=30)    # AUTH-5
MEMBER_SESSION = timedelta(minutes=60)   # AUTH-5
REFRESH_AFTER = timedelta(minutes=5)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


def make_token(member: Member) -> str:
    now = datetime.now(timezone.utc)
    ttl = STAFF_SESSION if member.role in STAFF_ROLES else MEMBER_SESSION
    payload = {"sub": str(member.id), "role": member.role, "iat": int(now.timestamp()),
               "exp": int((now + ttl).timestamp())}
    return jwt.encode(payload, get_config().secret_key, algorithm=ALGO)


def _decode(token: str) -> dict:
    try:
        return jwt.decode(token, get_config().secret_key, algorithms=[ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Session expired. Please sign in again.") from None
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid session. Please sign in again.") from None


def current_member(response: Response, creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                   db: Session = Depends(get_db)) -> Member:
    if not creds:
        raise HTTPException(401, "Please sign in.")
    payload = _decode(creds.credentials)
    member = db.get(Member, int(payload["sub"]))
    if not member:
        raise HTTPException(401, "Account not found.")
    # Sliding session: hand back a fresh token so the timeout counts inactivity, not total time
    if utcnow().timestamp() - payload["iat"] > REFRESH_AFTER.total_seconds():
        response.headers["X-New-Token"] = make_token(member)
    return member


def optional_member(creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                    db: Session = Depends(get_db)) -> Member | None:
    if not creds:
        return None
    try:
        return db.get(Member, int(_decode(creds.credentials)["sub"]))
    except HTTPException:
        return None


def require_roles(*roles: str):
    def dep(member: Member = Depends(current_member)) -> Member:
        if member.must_change_password:
            raise HTTPException(403, "You must change your password before continuing.")
        if member.role not in roles:
            raise HTTPException(403, "You do not have permission to do this.")
        return member
    return dep


def any_staff(member: Member = Depends(current_member)) -> Member:
    if member.must_change_password:
        raise HTTPException(403, "You must change your password before continuing.")
    if member.role not in STAFF_ROLES:
        raise HTTPException(403, "Staff access only.")
    return member


def client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)
