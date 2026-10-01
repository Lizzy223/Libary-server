from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import COLLECTIONS, COPY_STATUSES, FORMATS, SUBJECT_AREAS, AuditLog, Copy, Loan, Member, Title
from ..security import any_staff, optional_member, require_roles
from ..serializers import availability_map, copy_out, iso, title_out
from ..services import search as search_service, vector_store
from ..services.audit import audit, snap
from .files import csv_response_body, read_rows

router = APIRouter(prefix="/catalogue", tags=["catalogue"])
CATALOGUER = require_roles("cataloguer", "head_librarian")
TITLE_FIELDS = ["title", "subtitle", "authors", "edition", "publisher", "year", "isbn", "subjects", "subject_area",
                "call_number", "language", "department", "format", "description"]

# SRS 8.2 - allowed manual status changes. on_loan/reserved/available flow through circulation.
MANUAL_TRANSITIONS = {
    "available": {"damaged", "in_repair", "withdrawn", "lost"},
    "damaged": {"in_repair", "available", "withdrawn", "lost"},
    "in_repair": {"available", "withdrawn", "lost"},
    "in_transit": {"available", "lost"},
    "lost": {"available"},          # found again
    "withdrawn": set(),
    "on_loan": set(),               # return it through circulation first
    "reserved": {"lost"},
}


class TitleIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    subtitle: str | None = None
    authors: str | None = None
    edition: str | None = None
    publisher: str | None = None
    year: int | None = Field(default=None, ge=1400, le=2100)
    isbn: str | None = None
    subjects: str | None = None
    subject_area: str = "other"
    call_number: str | None = None
    language: str = "English"
    department: str | None = None
    format: str = "print_book"
    description: str | None = None
    copies: int = Field(default=1, ge=0, le=50)
    shelf_location: str | None = None
    collection: str = "general"
    cost: int | None = Field(default=None, ge=0)


class TitlePatch(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    authors: str | None = None
    edition: str | None = None
    publisher: str | None = None
    year: int | None = None
    isbn: str | None = None
    subjects: str | None = None
    subject_area: str | None = None
    call_number: str | None = None
    language: str | None = None
    department: str | None = None
    format: str | None = None
    description: str | None = None


class CopiesIn(BaseModel):
    count: int = Field(default=1, ge=1, le=50)
    shelf_location: str | None = None
    collection: str = "general"
    cost: int | None = Field(default=None, ge=0)


class CopyPatch(BaseModel):
    status: str | None = None
    shelf_location: str | None = None
    collection: str | None = None
    cost: int | None = None


def _validate(data: dict) -> None:
    if data.get("subject_area") and data["subject_area"] not in SUBJECT_AREAS:
        raise HTTPException(422, f"subject_area must be one of {', '.join(SUBJECT_AREAS)}")
    if data.get("format") and data["format"] not in FORMATS:
        raise HTTPException(422, f"format must be one of {', '.join(FORMATS)}")
    if data.get("collection") and data["collection"] not in COLLECTIONS:
        raise HTTPException(422, f"collection must be one of {', '.join(COLLECTIONS)}")


def _norm_isbn(isbn: str | None) -> str | None:
    return "".join(ch for ch in isbn if ch.isalnum()).upper() or None if isbn else None


def _find_duplicate(db: Session, isbn: str | None, edition: str | None, exclude_id: int | None = None):
    if not isbn:
        return None
    stmt = select(Title).where(Title.isbn == isbn, func.coalesce(Title.edition, "") == (edition or ""))
    if exclude_id:
        stmt = stmt.where(Title.id != exclude_id)
    return db.scalar(stmt)


def _add_copies(db: Session, title: Title, count: int, shelf: str | None, collection: str, cost: int | None) -> list[Copy]:
    made = []
    for _ in range(count):
        c = Copy(title_id=title.id, barcode="PENDING", shelf_location=shelf, collection=collection, cost=cost)
        db.add(c)
        db.flush()
        c.barcode = f"NITT{c.id:07d}"  # CAT-2: unique, system-generated
        made.append(c)
    return made


# -------------------------------------------------------------- public discovery
@router.get("/search")
def search(q: str | None = None, subject_area: str | None = None, department: str | None = None,
           format: str | None = None, language: str | None = None, year: int | None = None,
           available_only: bool = False, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50),
           member: Member | None = Depends(optional_member), db: Session = Depends(get_db)):
    filters = {"subject_area": subject_area, "department": department, "format": format, "language": language,
               "year": year, "available_only": available_only}
    return search_service.search_titles(db, q, filters, page, page_size, member.id if member else None)


@router.get("/facets")
def facets(db: Session = Depends(get_db)):
    return search_service.facets(db)


@router.get("/new-arrivals")
def new_arrivals(limit: int = Query(8, le=30), db: Session = Depends(get_db)):
    titles = db.scalars(select(Title).where(Title.retired.is_(False)).order_by(Title.created_at.desc(), Title.id.desc())
                        .limit(limit)).all()
    avail = availability_map(db, [t.id for t in titles])
    return [title_out(t, avail[t.id]) for t in titles]


@router.get("/titles/{title_id}")
def get_title(title_id: int, member: Member | None = Depends(optional_member), db: Session = Depends(get_db)):
    t = db.get(Title, title_id)
    if not t or (t.retired and not (member and member.role in ("cataloguer", "head_librarian", "admin"))):
        raise HTTPException(404, "Title not found.")
    out = title_out(t, availability_map(db, [t.id])[t.id])
    if member and member.role in ("circulation", "cataloguer", "head_librarian", "admin"):
        out["copies"] = [copy_out(c) for c in db.scalars(select(Copy).where(Copy.title_id == t.id).order_by(Copy.id))]
    return out


# -------------------------------------------------------------- cataloguing
@router.post("/titles")
def create_title(body: TitleIn, background_tasks: BackgroundTasks,
                 actor: Member = Depends(CATALOGUER), db: Session = Depends(get_db)):
    data = body.model_dump()
    _validate(data)
    copies_n = data.pop("copies")
    shelf, collection, cost = data.pop("shelf_location"), data.pop("collection"), data.pop("cost")
    data["isbn"] = _norm_isbn(data["isbn"])
    dup = _find_duplicate(db, data["isbn"], data["edition"])
    if dup:
        raise HTTPException(409, f"A record with this ISBN and edition already exists (#{dup.id}: {dup.title}). "
                                 "Add copies to it instead.")
    t = Title(**data)
    t.refresh_search_text()
    db.add(t)
    db.flush()
    made = _add_copies(db, t, copies_n, shelf, collection, cost)
    audit(db, actor, "title.create", "title", t.id, after={**snap(t, TITLE_FIELDS), "copies": len(made)})
    db.commit()
    background_tasks.add_task(vector_store.upsert_title, t)
    return {**title_out(t, availability_map(db, [t.id])[t.id]), "copies": [copy_out(c) for c in made]}


@router.patch("/titles/{title_id}")
def update_title(title_id: int, body: TitlePatch, background_tasks: BackgroundTasks,
                 actor: Member = Depends(CATALOGUER), db: Session = Depends(get_db)):
    t = db.get(Title, title_id)
    if not t:
        raise HTTPException(404, "Title not found.")
    data = body.model_dump(exclude_unset=True)
    _validate(data)
    if "isbn" in data:
        data["isbn"] = _norm_isbn(data["isbn"])
    dup = _find_duplicate(db, data.get("isbn", t.isbn), data.get("edition", t.edition), exclude_id=t.id)
    if dup:
        raise HTTPException(409, f"Another record already has this ISBN and edition (#{dup.id}).")
    before = snap(t, TITLE_FIELDS)
    for k, v in data.items():
        setattr(t, k, v)
    t.refresh_search_text()
    audit(db, actor, "title.update", "title", t.id, before=before, after=snap(t, TITLE_FIELDS))  # CAT-7
    db.commit()
    background_tasks.add_task(vector_store.upsert_title, t)
    return title_out(t, availability_map(db, [t.id])[t.id])


@router.post("/titles/{title_id}/retire")
def retire_title(title_id: int, background_tasks: BackgroundTasks,
                 actor: Member = Depends(CATALOGUER), db: Session = Depends(get_db)):
    """CAT-8: records are never deleted; titles with loan history are retired instead."""
    t = db.get(Title, title_id)
    if not t:
        raise HTTPException(404, "Title not found.")
    on_loan = db.scalar(select(func.count()).select_from(Loan).join(Copy, Loan.copy_id == Copy.id)
                        .where(Copy.title_id == t.id, Loan.returned_at.is_(None)))
    if on_loan:
        raise HTTPException(409, "Some copies are still on loan. Retire the title after they are returned.")
    t.retired = True
    audit(db, actor, "title.retire", "title", t.id)
    db.commit()
    background_tasks.add_task(vector_store.delete_title, title_id)
    return {"ok": True}


@router.post("/titles/{title_id}/copies")
def add_copies(title_id: int, body: CopiesIn, actor: Member = Depends(CATALOGUER), db: Session = Depends(get_db)):
    t = db.get(Title, title_id)
    if not t or t.retired:
        raise HTTPException(404, "Title not found.")
    _validate({"collection": body.collection})
    made = _add_copies(db, t, body.count, body.shelf_location, body.collection, body.cost)
    audit(db, actor, "copy.create", "title", t.id, after={"barcodes": [c.barcode for c in made]})
    db.commit()
    return [copy_out(c) for c in made]


@router.patch("/copies/{copy_id}")
def update_copy(copy_id: int, body: CopyPatch, actor: Member = Depends(CATALOGUER), db: Session = Depends(get_db)):
    c = db.get(Copy, copy_id)
    if not c:
        raise HTTPException(404, "Copy not found.")
    data = body.model_dump(exclude_unset=True)
    _validate(data)
    before = copy_out(c)
    if "status" in data and data["status"] != c.status:
        if data["status"] not in COPY_STATUSES:
            raise HTTPException(422, "Unknown status.")
        if data["status"] not in MANUAL_TRANSITIONS.get(c.status, set()):
            raise HTTPException(409, f"A copy cannot move from '{c.status}' to '{data['status']}' by hand.")
        if data["status"] in ("lost", "withdrawn") and actor.role != "head_librarian":
            raise HTTPException(403, "Only the Head Librarian can mark copies lost or withdrawn.")
    for k, v in data.items():
        setattr(c, k, v)
    audit(db, actor, "copy.update", "copy", c.id, before=before, after=copy_out(c))
    db.commit()
    return copy_out(c)


@router.get("/copies/by-barcode/{barcode}")
def copy_by_barcode(barcode: str, _: Member = Depends(any_staff), db: Session = Depends(get_db)):
    c = db.scalar(select(Copy).where(Copy.barcode == barcode.strip().upper()))
    if not c:
        raise HTTPException(404, "No copy with that barcode.")
    return {**copy_out(c), "title": title_out(c.title)}


@router.get("/titles/{title_id}/history")
def title_history(title_id: int, _: Member = Depends(CATALOGUER), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).where(AuditLog.entity == "title", AuditLog.entity_id == str(title_id))
                      .order_by(AuditLog.id.desc())).all()
    return [{"action": r.action, "by": r.actor_label, "at": iso(r.created_at), "before": r.before, "after": r.after}
            for r in rows]


@router.get("/labels")
def labels(title_id: int | None = None, copy_ids: str | None = None, _: Member = Depends(CATALOGUER),
           db: Session = Depends(get_db)):
    """CAT-5: data for printable barcode sheets. The browser renders and prints the sheet."""
    stmt = select(Copy).join(Title, Copy.title_id == Title.id)
    if title_id:
        stmt = stmt.where(Copy.title_id == title_id)
    elif copy_ids:
        try:
            stmt = stmt.where(Copy.id.in_([int(x) for x in copy_ids.split(",") if x]))
        except ValueError:
            raise HTTPException(422, "copy_ids must be comma-separated numbers.") from None
    else:
        raise HTTPException(422, "Provide title_id or copy_ids.")
    return [{"barcode": c.barcode, "title": c.title.title, "call_number": c.title.call_number,
             "shelf_location": c.shelf_location} for c in db.scalars(stmt.order_by(Copy.id).limit(500))]


# -------------------------------------------------------------- bulk import
IMPORT_COLUMNS = ["title", "subtitle", "authors", "edition", "publisher", "year", "isbn", "subjects", "subject_area",
                  "call_number", "language", "department", "format", "copies", "shelf_location", "collection", "cost"]


@router.get("/import-template")
def import_template(_: Member = Depends(CATALOGUER)):
    sample = ["Sample Title", "", "Author One; Author Two", "2nd", "Publisher", "2015", "9780000000000",
              "Shipping; Economics", "maritime", "HE571", "English", "Maritime Studies", "print_book", "2",
              "Section A / Shelf 3", "general", "25000"]
    body = csv_response_body(IMPORT_COLUMNS, [sample])
    return Response(body, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="titles_template.csv"'})


@router.post("/import")
async def import_titles(dry_run: bool = True, file: UploadFile = File(...), background_tasks: BackgroundTasks = None,
                        actor: Member = Depends(CATALOGUER), db: Session = Depends(get_db)):
    """CAT-4 / DR-4: validate every row, detect duplicates on ISBN + edition, then commit only when dry_run=false."""
    rows = await read_rows(file)
    created = copies_made = 0
    errors, duplicates = [], []
    seen: set[tuple] = set()
    new_titles: list[Title] = []
    for i, row in enumerate(rows, start=2):
        title = row.get("title", "")
        isbn = _norm_isbn(row.get("isbn"))
        edition = row.get("edition") or None
        try:
            year = int(float(row["year"])) if row.get("year") else None
            n_copies = int(float(row["copies"])) if row.get("copies") else 1
            cost = int(float(row["cost"])) if row.get("cost") else None
        except ValueError:
            errors.append({"row": i, "title": title, "error": "year, copies and cost must be numbers"})
            continue
        area = (row.get("subject_area") or "other").lower()
        fmt = (row.get("format") or "print_book").lower()
        collection = (row.get("collection") or "general").lower()
        problem = None
        if not title:
            problem = "title is required"
        elif area not in SUBJECT_AREAS:
            problem = f"subject_area must be one of {', '.join(SUBJECT_AREAS)}"
        elif fmt not in FORMATS:
            problem = f"format must be one of {', '.join(FORMATS)}"
        elif collection not in COLLECTIONS:
            problem = f"collection must be one of {', '.join(COLLECTIONS)}"
        elif not 0 <= n_copies <= 50:
            problem = "copies must be between 0 and 50"
        if problem:
            errors.append({"row": i, "title": title, "error": problem})
            continue
        key = (isbn, edition or "")
        if isbn and (key in seen or _find_duplicate(db, isbn, edition)):
            duplicates.append({"row": i, "title": title, "isbn": isbn})
            continue
        seen.add(key)
        t = Title(title=title, subtitle=row.get("subtitle") or None, authors=row.get("authors") or None,
                  edition=edition, publisher=row.get("publisher") or None, year=year, isbn=isbn,
                  subjects=row.get("subjects") or None, subject_area=area, call_number=row.get("call_number") or None,
                  language=row.get("language") or "English", department=row.get("department") or None, format=fmt)
        t.refresh_search_text()
        db.add(t)
        db.flush()
        copies_made += len(_add_copies(db, t, n_copies, row.get("shelf_location") or None, collection, cost))
        new_titles.append(t)
        created += 1
    if dry_run:
        db.rollback()
    else:
        audit(db, actor, "title.import", "title", None,
              after={"titles": created, "copies": copies_made, "duplicates": len(duplicates), "errors": len(errors)})
        db.commit()
        if background_tasks is not None:
            background_tasks.add_task(vector_store.upsert_batch, new_titles)
    return {"dry_run": dry_run, "titles_created": created, "copies_created": copies_made,
            "duplicates": duplicates, "errors": errors, "rows_read": len(rows)}
