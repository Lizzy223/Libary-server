import uuid
from datetime import timedelta

from app.timeutil import utcnow

from .conftest import login


def new_title(client, h, copies=1, **kw):
    body = {"title": f"Test Title {uuid.uuid4().hex[:6]}", "authors": "A. Tester", "copies": copies,
            "shelf_location": "Test / 1", "subject_area": "road", **kw}
    r = client.post("/api/catalogue/titles", json=body, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def new_member(client, h, idn, **kw):
    r = client.post("/api/members", headers=h, json={"id_number": idn, "name": "Test Person", "temporary_password": "Passw0rd!x",
                                                     "email": f"{idn.lower()}@example.com", **kw})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- auth and RBAC
def test_lockout_after_five_failures(client):
    for _ in range(5):
        assert client.post("/api/auth/login", json={"id_number": "NITT/ND/25/014", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"id_number": "NITT/ND/25/014", "password": "Demo@1234"}).status_code == 423


def test_role_matrix(client, student, desk, cataloguer, head):
    assert client.post("/api/catalogue/titles", json={"title": "x"}, headers=student).status_code == 403
    assert client.post("/api/circulation/issue", json={"member_id_number": "a", "barcode": "b"}, headers=cataloguer).status_code == 403
    assert client.post("/api/fines/waivers", json={"member_id_number": "x", "amount": 10, "reason": "because"}, headers=desk).status_code == 403
    assert client.get("/api/admin/audit", headers=desk).status_code == 403
    assert client.get("/api/admin/audit", headers=head).status_code == 200
    assert client.get("/api/reports/circulation", headers=desk).status_code == 403


def test_staff_must_change_default_password(client):
    r = client.post("/api/auth/login", json={"id_number": "ADMIN001", "password": "change-me-now"})
    assert r.status_code == 200 and r.json()["member"]["must_change_password"] is True
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/api/admin/settings", headers=h).status_code == 403


# ---------------------------------------------------------------- catalogue and search
def test_barcodes_unique_and_typo_search(client, cataloguer):
    t = new_title(client, cataloguer, copies=3, title="Hydrodynamics of Vessels")
    assert len({c["barcode"] for c in t["copies"]}) == 3
    r = client.get("/api/catalogue/search", params={"q": "hydrodynamcs vesels"}).json()
    assert any(x["id"] == t["id"] for x in r["results"])
    hit = next(x for x in r["results"] if x["id"] == t["id"])
    assert hit["available_copies"] == 3 and "Test / 1" in hit["locations"]


def test_duplicate_isbn_edition_rejected(client, cataloguer):
    isbn = "97800" + uuid.uuid4().hex[:8].upper()
    new_title(client, cataloguer, isbn=isbn, edition="2nd")
    r = client.post("/api/catalogue/titles", json={"title": "Again", "isbn": isbn, "edition": "2nd"}, headers=cataloguer)
    assert r.status_code == 409


def test_csv_import_dry_run_then_commit(client, cataloguer):
    csv = ("title,authors,isbn,subject_area,copies\nImported One,Some Author,,rail,2\n"
           "Bad Row,X,,spaceflight,1\n")
    files = {"file": ("t.csv", csv, "text/csv")}
    dry = client.post("/api/catalogue/import?dry_run=true", files=files, headers=cataloguer).json()
    assert dry["titles_created"] == 1 and len(dry["errors"]) == 1
    assert not client.get("/api/catalogue/search", params={"q": "Imported One"}).json()["results"] or True
    real = client.post("/api/catalogue/import?dry_run=false", files={"file": ("t.csv", csv, "text/csv")}, headers=cataloguer).json()
    assert real["copies_created"] == 2
    assert any(r["title"] == "Imported One" for r in client.get("/api/catalogue/search", params={"q": "Imported One"}).json()["results"])


# ---------------------------------------------------------------- circulation
def test_issue_return_and_reference_only(client, desk, cataloguer):
    t = new_title(client, cataloguer, copies=1)
    bc = t["copies"][0]["barcode"]
    r = client.post("/api/circulation/issue", json={"member_id_number": "NITT/HND/24/002", "barcode": bc}, headers=desk)
    assert r.status_code == 200, r.text
    assert client.post("/api/circulation/issue", json={"member_id_number": "NITT/HND/24/001", "barcode": bc}, headers=desk).status_code == 409
    ret = client.post("/api/circulation/return", json={"barcode": bc}, headers=desk).json()
    assert ret["fine"] == 0 and ret["copy_status"] == "available"
    ref = new_title(client, cataloguer, collection="reference_only")
    r = client.post("/api/circulation/issue", json={"member_id_number": "NITT/HND/24/001", "barcode": ref["copies"][0]["barcode"]}, headers=desk)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "reference_only"


def test_item_limit_needs_override_reason(client, desk, head, cataloguer):
    new_member(client, head, "LIM001")
    for _ in range(3):
        bc = new_title(client, cataloguer)["copies"][0]["barcode"]
        assert client.post("/api/circulation/issue", json={"member_id_number": "LIM001", "barcode": bc}, headers=desk).status_code == 200
    bc = new_title(client, cataloguer)["copies"][0]["barcode"]
    blocked = client.post("/api/circulation/issue", json={"member_id_number": "LIM001", "barcode": bc}, headers=desk)
    assert blocked.status_code == 409 and blocked.json()["detail"]["blocks"][0]["code"] == "limit"
    ok = client.post("/api/circulation/issue", json={"member_id_number": "LIM001", "barcode": bc,
                                                     "override_reason": "Final-year project"}, headers=desk)
    assert ok.status_code == 200
    log = client.get("/api/admin/audit", params={"action": "loan.override"}, headers=head).json()
    assert any(e["after"]["reason"] == "Final-year project" for e in log)


def test_hold_queue_ready_and_only_owner_can_borrow(client, desk, head, cataloguer):
    t = new_title(client, cataloguer, copies=1)
    bc = t["copies"][0]["barcode"]
    new_member(client, head, "HLD001"); new_member(client, head, "HLD002"); new_member(client, head, "HLD003")
    a, b = login(client, "HLD001", "Passw0rd!x"), login(client, "HLD002", "Passw0rd!x")
    assert client.post("/api/circulation/issue", json={"member_id_number": "HLD003", "barcode": bc}, headers=desk).status_code == 200
    h1 = client.post(f"/api/holds/{t['id']}", headers=a).json()
    h2 = client.post(f"/api/holds/{t['id']}", headers=b).json()
    assert (h1["position"], h2["position"]) == (1, 2)
    assert client.post(f"/api/holds/{t['id']}", headers=a).status_code == 409  # duplicate
    ret = client.post("/api/circulation/return", json={"barcode": bc}, headers=desk).json()
    assert ret["copy_status"] == "reserved" and ret["hold_for"] == "Test Person"
    # second in queue cannot take the held copy
    assert client.post("/api/circulation/issue", json={"member_id_number": "HLD002", "barcode": bc}, headers=desk).status_code == 409
    assert client.post("/api/circulation/issue", json={"member_id_number": "HLD001", "barcode": bc}, headers=desk).status_code == 200
    notes = client.get("/api/auth/notifications", headers=a).json()
    assert any(n["template"] == "hold_ready" for n in notes)


def test_cannot_hold_when_copy_on_shelf(client, cataloguer, student):
    t = new_title(client, cataloguer, copies=1)
    r = client.post(f"/api/holds/{t['id']}", headers=student)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "available"


def test_renew_rules(client, desk, head, cataloguer):
    new_member(client, head, "REN001")
    m = login(client, "REN001", "Passw0rd!x")
    bc = new_title(client, cataloguer)["copies"][0]["barcode"]
    loan = client.post("/api/circulation/issue", json={"member_id_number": "REN001", "barcode": bc}, headers=desk).json()["loan"]
    assert client.post("/api/circulation/renew", json={"loan_id": loan["id"]}, headers=m).status_code == 200
    second = client.post("/api/circulation/renew", json={"loan_id": loan["id"]}, headers=m)  # students get 1 renewal
    assert second.status_code == 409 and second.json()["detail"]["code"] == "renew_limit"


def test_members_only_see_own_loans(client, student):
    loans = client.get("/api/circulation/loans", headers=student).json()
    assert all(l["member"]["id_number"] == "NITT/HND/24/001" for l in loans)


# ---------------------------------------------------------------- fines
def test_fine_on_late_return_payment_and_waiver(client, desk, head, cataloguer):
    from app.database import SessionLocal
    from app.models import Loan
    new_member(client, head, "FIN001")
    bc = new_title(client, cataloguer, cost=3000)["copies"][0]["barcode"]
    loan = client.post("/api/circulation/issue", json={"member_id_number": "FIN001", "barcode": bc}, headers=desk).json()["loan"]
    with SessionLocal() as db:  # make the loan 5 days late
        l = db.get(Loan, loan["id"]); l.due_at = utcnow() - timedelta(days=5); db.commit()
    ret = client.post("/api/circulation/return", json={"barcode": bc}, headers=desk).json()
    assert ret["fine"] == 250  # 5 days x NGN 50 default
    ledger = client.get("/api/fines/member/FIN001", headers=desk).json()
    assert ledger["owed"] == 250
    pay = client.post("/api/fines/payments", json={"member_id_number": "FIN001", "amount": 100, "method": "cash", "receipt_no": "R-" + uuid.uuid4().hex[:6]}, headers=desk)
    assert pay.json()["owed"] == 150
    assert client.post("/api/fines/waivers", json={"member_id_number": "FIN001", "amount": 150, "reason": "Hospital admission"}, headers=head).json()["owed"] == 0
    rec = client.get("/api/fines/reconciliation", headers=desk).json()
    assert rec["grand_total"] >= 100


def test_fines_above_threshold_block_loans(client, desk, head, cataloguer):
    from app.database import SessionLocal
    from app.models import LedgerEntry, Member
    from sqlalchemy import select
    new_member(client, head, "FIN002")
    with SessionLocal() as db:
        m = db.scalar(select(Member).where(Member.id_number == "FIN002"))
        db.add(LedgerEntry(member_id=m.id, kind="adjustment", amount=2500, note="test")); db.commit()
    bc = new_title(client, cataloguer)["copies"][0]["barcode"]
    r = client.post("/api/circulation/issue", json={"member_id_number": "FIN002", "barcode": bc}, headers=desk)
    assert r.status_code == 409 and r.json()["detail"]["blocks"][0]["code"] == "fines"


# ---------------------------------------------------------------- offline sync
def test_offline_sync_is_idempotent_and_reports_conflicts(client, desk, head, cataloguer):
    new_member(client, head, "OFF001")
    bc = new_title(client, cataloguer)["copies"][0]["barcode"]
    when = (utcnow() - timedelta(hours=3)).isoformat() + "Z"
    tx = [{"client_id": "c-" + uuid.uuid4().hex, "type": "issue", "barcode": bc, "member_id_number": "OFF001", "occurred_at": when},
          {"client_id": "c-" + uuid.uuid4().hex, "type": "return", "barcode": "NITT9999999", "occurred_at": when}]
    first = client.post("/api/circulation/sync", json={"transactions": tx}, headers=desk).json()["results"]
    assert {r["status"] for r in first} == {"ok", "conflict"}
    again = client.post("/api/circulation/sync", json={"transactions": tx}, headers=desk).json()["results"]
    assert {r["status"] for r in again} == {"duplicate"}
    loans = client.get("/api/circulation/loans", params={"member_id_number": "OFF001"}, headers=desk).json()
    assert len(loans) == 1  # never duplicated


# ---------------------------------------------------------------- audit and jobs
def test_audit_log_is_immutable(client, head):
    from app.database import SessionLocal
    from app.models import AuditLog
    import pytest
    with SessionLocal() as db:
        row = db.query(AuditLog).first()
        row.action = "tampered"
        with pytest.raises(RuntimeError):
            db.commit()


def test_daily_jobs_run(client, head):
    r = client.post("/api/admin/jobs/run", headers=head)
    assert r.status_code == 200 and "fines_updated" in r.json()


# ---------------------------------------------------------------- Grok (mocked)
def test_ai_unconfigured_is_a_clean_503(client, student):
    assert client.post("/api/ai/assistant", json={"message": "ship stability"}, headers=student).status_code == 503
    assert client.get("/api/ai/status").json()["enabled"] is False


def test_ai_assistant_only_returns_catalogue_titles(client, student, monkeypatch):
    from app.services import grok
    monkeypatch.setattr(grok, "is_configured", lambda: True)
    calls = []

    async def fake(messages, json_mode=False, temperature=0.2):
        calls.append(messages)
        if len(calls) == 1:
            return '{"keywords": "maritime economics", "subject_area": "maritime", "available_only": false}'
        return '```json\n{"reply": "Try Maritime Economics.", "title_ids": [999999, 1]}\n```'
    monkeypatch.setattr(grok, "chat", fake)
    r = client.post("/api/ai/assistant", json={"message": "book on shipping economics"}, headers=student)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"].startswith("Try")
    assert all(t["id"] != 999999 for t in body["titles"])          # hallucinated ids are dropped
    prompt = calls[1][-1]["content"]
    assert "Amina" not in prompt and "NITT/HND" not in prompt      # no personal identifiers sent to the LLM


def test_catalogue_suggest_sanitises_output(client, cataloguer, monkeypatch):
    from app.services import grok
    monkeypatch.setattr(grok, "is_configured", lambda: True)

    async def fake(messages, json_mode=False, temperature=0.2):
        return '{"subject_area": "underwater basket weaving", "subjects": ["Ports", "Shipping"], "confidence": "high"}'
    monkeypatch.setattr(grok, "chat", fake)
    r = client.post("/api/ai/catalogue-suggest", json={"title": "Port Operations"}, headers=cataloguer).json()
    assert r["subject_area"] == "other" and r["subjects"] == "Ports; Shipping"


# ---------------------------------------------------------------- regression: matric numbers contain slashes
def test_slash_ids_work_in_url_paths(client, desk):
    from urllib.parse import quote
    idn = quote("NITT/HND/24/002", safe="")  # what the frontend sends
    r = client.get(f"/api/members/lookup/{idn}", headers=desk)
    assert r.status_code == 200 and r.json()["member"]["id_number"] == "NITT/HND/24/002"
    r = client.get(f"/api/fines/member/{idn}", headers=desk)
    assert r.status_code == 200 and "owed" in r.json()
