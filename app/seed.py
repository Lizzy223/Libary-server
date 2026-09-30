"""Illustrative demo data so the prototype opens with something to explore.
Titles are real, widely used transport and logistics textbooks; editions, years, shelf marks and prices are
placeholders. Replace everything through the import tool (Catalogue > Import) before real use."""
from datetime import timedelta

from sqlalchemy import func, select

from .config import get_config
from .database import SessionLocal
from .models import Copy, Hold, Member, Title
from .security import hash_password
from .services import circulation as circ
from .timeutil import utcnow

PEOPLE = [
    # id_number, name, member_type, role, programme, level, department, email
    ("STF001", "Chidi Okeke", "non_academic_staff", "admin", None, None, "ICT Unit", "chidi@example.com"),
    ("STF002", "Dr. Musa Ibrahim", "academic_staff", "head_librarian", None, None, "Library", "musa@example.com"),
    ("STF003", "Mrs. Hadiza Yusuf", "non_academic_staff", "circulation", None, None, "Library", "hadiza@example.com"),
    ("STF004", "Zainab Lawal", "non_academic_staff", "cataloguer", None, None, "Library", "zainab@example.com"),
    ("STF010", "Engr. Bello Garba", "academic_staff", "lecturer", None, None, "Maritime Studies", "bello@example.com"),
    ("NITT/HND/24/001", "Amina Sani", "student", "student", "HND Transport Management", "HND II", "Transport Management", "amina@example.com"),
    ("NITT/HND/24/002", "Emeka Nwosu", "student", "student", "HND Logistics and Supply Chain", "HND I", "Logistics and Supply Chain", "emeka@example.com"),
    ("NITT/ND/25/014", "Fatima Abdullahi", "student", "student", "ND Maritime Transport", "ND II", "Maritime Studies", "fatima@example.com"),
    ("VIS001", "Hajiya Rakiya Danladi", "visitor", "student", None, None, None, "rakiya@example.com"),
]

# title, authors, subject_area, department, format, call_number, subjects, year, copies, shelf, collection, cost
TITLES = [
    ("Maritime Economics", "Martin Stopford", "maritime", "Maritime Studies", "print_book", "HE571", "Shipping; Maritime economics", 2009, 3, "Section A / Shelf 2", "general", 45000),
    ("Ship Stability for Masters and Mates", "Bryan Barrass; D. R. Derrett", "maritime", "Maritime Studies", "print_book", "VM159", "Ship stability; Naval architecture", 2012, 2, "Section A / Shelf 4", "general", 38000),
    ("Port Economics, Management and Policy", "Theo Notteboom; Athanasios Pallis; Jean-Paul Rodrigue", "maritime", "Maritime Studies", "print_book", "HE551", "Ports; Terminal management", 2022, 1, "Section A / Shelf 5", "general", 60000),
    ("Supply Chain Management: Strategy, Planning, and Operation", "Sunil Chopra; Peter Meindl", "logistics", "Logistics and Supply Chain", "print_book", "HD38.5", "Supply chain; Logistics", 2016, 4, "Section B / Shelf 1", "general", 42000),
    ("Logistics and Supply Chain Management", "Martin Christopher", "logistics", "Logistics and Supply Chain", "print_book", "HD38.5", "Logistics; Supply chain", 2016, 3, "Section B / Shelf 1", "general", 40000),
    ("Transportation: A Global Supply Chain Perspective", "John J. Coyle; Robert A. Novack; Brian Gibson; Edward J. Bardi", "logistics", "Transport Management", "print_book", "HE151", "Transport management; Freight", 2011, 2, "Section B / Shelf 3", "general", 41000),
    ("Introduction to Transportation Engineering", "James H. Banks", "road", "Transport Management", "print_book", "TA1145", "Transport engineering; Highways", 2002, 2, "Section C / Shelf 2", "general", 36000),
    ("Principles of Highway Engineering and Traffic Analysis", "Fred L. Mannering; Scott S. Washburn", "road", "Transport Management", "print_book", "TE145", "Highway design; Traffic analysis", 2012, 2, "Section C / Shelf 2", "general", 39000),
    ("Traffic Engineering", "Roger P. Roess; Elena S. Prassas; William R. McShane", "road", "Transport Management", "print_book", "HE333", "Traffic engineering", 2010, 1, "Section C / Shelf 3", "reserve", 44000),
    ("Introduction to Air Transport Economics: From Theory to Applications", "Bijan Vasigh; Ken Fleming; Thomas Tacker", "aviation", "Aviation Studies", "print_book", "HE9776", "Air transport; Airline economics", 2013, 2, "Section D / Shelf 1", "general", 43000),
    ("Modern Railway Track", "Coenraad Esveld", "rail", "Transport Management", "print_book", "TF200", "Railway engineering; Track", 2001, 1, "Section D / Shelf 4", "reference_only", 70000),
    ("Transport Policy Handbook (sample record)", "Library sample", "policy", "Transport Management", "standard", "HE193", "Transport policy; Regulation", 2020, 1, "Section E / Shelf 1", "reference_only", None),
]


def seed_demo() -> None:
    cfg = get_config()
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(Title)):
            return  # already seeded
        pw = hash_password(cfg.demo_password)
        now = utcnow()
        members = {}
        for idn, name, mtype, role, prog, level, dept, email in PEOPLE:
            m = Member(id_number=idn, name=name, member_type=mtype, role=role, programme=prog, level=level,
                       department=dept, email=email, password_hash=pw, must_change_password=False, consent_at=now)
            db.add(m)
            members[idn] = m
        db.flush()

        titles: dict[str, Title] = {}
        for i, (title, authors, area, dept, fmt, call, subjects, year, n, shelf, coll, cost) in enumerate(TITLES):
            t = Title(title=title, authors=authors, subject_area=area, department=dept, format=fmt, call_number=call,
                      subjects=subjects, year=year, created_at=now - timedelta(days=len(TITLES) - i))
            t.refresh_search_text()
            db.add(t)
            db.flush()
            for _ in range(n):
                c = Copy(title_id=t.id, barcode="PENDING", shelf_location=shelf, collection=coll, cost=cost)
                db.add(c)
                db.flush()
                c.barcode = f"NITT{c.id:07d}"
            titles[title] = t
        db.flush()

        officer = members["STF003"]

        def first_free(title: str) -> Copy:
            return db.scalar(select(Copy).where(Copy.title_id == titles[title].id, Copy.status == "available").order_by(Copy.id))

        # An overdue loan (fine accrues), a current loan, and a title that is fully out with a waiting hold
        circ.issue(db, members["NITT/HND/24/002"], first_free("Logistics and Supply Chain Management"), officer, now=now - timedelta(days=24))
        circ.issue(db, members["NITT/HND/24/001"], first_free("Maritime Economics"), officer, now=now - timedelta(days=3))
        circ.issue(db, members["NITT/ND/25/014"], first_free("Port Economics, Management and Policy"), officer, now=now - timedelta(days=2))
        db.flush()
        db.add(Hold(title_id=titles["Port Economics, Management and Policy"].id, member_id=members["NITT/HND/24/001"].id,
                    status="queued", queued_at=now - timedelta(days=1)))
        circ.accrue_fines(db, now)
        db.commit()
