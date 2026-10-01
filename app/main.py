import logging
import threading
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from .config import get_config
from .database import Base, SessionLocal, engine
from .models import Member, Title
from .routers import admin, ai, auth, catalogue, circulation, fines, members, reports
from .security import client_ip, hash_password
from .services import vector_store
from .services.audit import request_ip
from .services.notifications import dispatch_pending_emails

log = logging.getLogger("library")
cfg = get_config()


def bootstrap_admin() -> None:
    """Creates the first admin only if none exists. The password must be changed at first sign-in (AUTH-7)."""
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(Member).where(Member.role == "admin")):
            return
        db.add(Member(id_number=cfg.bootstrap_admin_id.upper(), name="System Administrator",
                      member_type="non_academic_staff", role="admin", email=cfg.bootstrap_admin_email or None,
                      password_hash=hash_password(cfg.bootstrap_admin_password), must_change_password=True))
        db.commit()
        log.warning("Created bootstrap admin %s. Change the password at first sign-in.", cfg.bootstrap_admin_id)


def _email_tick() -> None:
    with SessionLocal() as db:
        dispatch_pending_emails(db)
        db.commit()


def _pinecone_sync() -> None:
    """Populate the Pinecone index on startup if it is empty or stale."""
    try:
        with SessionLocal() as db:
            titles = list(db.scalars(select(Title).where(Title.retired.is_(False))))
        if not titles:
            return
        stats = vector_store.get_index().describe_index_stats()
        if stats.total_vector_count >= len(titles) * 0.9:
            log.info("Pinecone index already populated (%d vectors).", stats.total_vector_count)
            return
        log.info("Pinecone index has %d vectors, DB has %d titles — syncing.",
                 stats.total_vector_count, len(titles))
        vector_store.upsert_batch(titles)
    except Exception as exc:
        log.warning("Pinecone startup sync failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)  # fine for a prototype; adopt Alembic before the schema changes in production
    bootstrap_admin()
    if cfg.seed_demo_data:
        from .seed import seed_demo
        seed_demo()
    if vector_store.is_configured():
        threading.Thread(target=_pinecone_sync, daemon=True).start()
    scheduler = None
    if cfg.run_scheduler:
        scheduler = BackgroundScheduler(timezone="Africa/Lagos")
        scheduler.add_job(admin.run_jobs_once, "interval", hours=3, id="daily-jobs")
        scheduler.add_job(_email_tick, "interval", minutes=2, id="email-dispatch")
        scheduler.start()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="NITT Library Management & Book Tracking System", version="1.0.0", lifespan=lifespan,
              description="REST API for the NITT library (PRD/SRS v1.0). Interactive docs at /docs.")

app.add_middleware(CORSMiddleware, allow_origins=cfg.origins, allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"], expose_headers=["X-New-Token"])


@app.middleware("http")
async def remember_ip(request: Request, call_next):
    token = request_ip.set(client_ip(request))  # audit rows record the source IP (ADM-2)
    try:
        return await call_next(request)
    finally:
        request_ip.reset(token)


for module in (auth, members, catalogue, circulation, fines, reports, ai, admin):
    app.include_router(module.router, prefix="/api")


@app.get("/api/health", tags=["system"])
def health():
    with SessionLocal() as db:
        db.execute(select(1))
    return {"status": "ok"}
