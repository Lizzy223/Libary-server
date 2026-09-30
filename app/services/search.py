"""Typo-tolerant catalogue search (SRCH-1..4). SQL applies the filters, RapidFuzz ranks the text.
For 100,000 records this stays fast because only (id, search_text) pairs are scored, in C++."""
from rapidfuzz import fuzz, process
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from ..models import Copy, SearchLog, Title
from ..serializers import availability_map, title_out

FUZZY_CUTOFF = 68


def _filtered_ids_stmt(f: dict):
    stmt = select(Title.id, Title.search_text).where(Title.retired.is_(False))
    if f.get("subject_area"):
        stmt = stmt.where(Title.subject_area == f["subject_area"])
    if f.get("department"):
        stmt = stmt.where(Title.department == f["department"])
    if f.get("format"):
        stmt = stmt.where(Title.format == f["format"])
    if f.get("language"):
        stmt = stmt.where(Title.language == f["language"])
    if f.get("year"):
        stmt = stmt.where(Title.year == f["year"])
    if f.get("available_only"):
        stmt = stmt.where(exists().where(Copy.title_id == Title.id, Copy.status == "available",
                                         Copy.collection != "reference_only"))
    return stmt


def search_titles(db: Session, q: str | None, filters: dict, page: int, page_size: int,
                  member_id: int | None = None) -> dict:
    q = (q or "").strip().lower()
    stmt = _filtered_ids_stmt(filters)

    if q:
        rows = db.execute(stmt).all()
        texts = {tid: text for tid, text in rows}
        hits = process.extract(q, texts, scorer=fuzz.WRatio, score_cutoff=FUZZY_CUTOFF, limit=300)
        ranked = []
        for text, score, tid in hits:
            bonus = 20 if q in text else 0  # exact phrase / ISBN / call-number matches rise to the top
            ranked.append((score + bonus, tid))
        ranked.sort(key=lambda x: -x[0])
        ids = [tid for _, tid in ranked]
    else:
        ids = [tid for (tid, _) in db.execute(stmt.order_by(Title.title)).all()]

    total = len(ids)
    if q and page == 1:  # SRCH-5: keep a record of searches, especially empty ones
        db.add(SearchLog(query=q[:300], result_count=total, member_id=member_id))
        db.commit()

    page_ids = ids[(page - 1) * page_size: page * page_size]
    titles = {t.id: t for t in db.scalars(select(Title).where(Title.id.in_(page_ids)))}
    avail = availability_map(db, page_ids)
    return {
        "total": total, "page": page, "page_size": page_size,
        "results": [title_out(titles[i], avail[i]) for i in page_ids if i in titles],
    }


def facets(db: Session) -> dict:
    def distinct(col):
        return [v for (v,) in db.execute(select(col).where(Title.retired.is_(False), col.isnot(None))
                                         .group_by(col).order_by(col)).all() if v]
    return {"departments": distinct(Title.department), "languages": distinct(Title.language),
            "formats": distinct(Title.format), "subject_areas": distinct(Title.subject_area)}
