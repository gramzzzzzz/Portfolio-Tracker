"""End-to-end flow through the HTTP API with the real sample files."""
import io

import openpyxl


def login(client):
    assert client.get("/api/auth/status").json() == {"configured": False, "authenticated": False}
    assert client.post("/api/auth/setup", json={"password": "correct-horse"}).status_code == 200


def upload_sheet(client, sheet_bytes):
    r = client.post("/api/versions/upload", files={"file": ("Invt.xlsx", sheet_bytes, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def stage(client, files, password=None):
    data = {"use_saved_password": "true", "save_password": "false"}
    if password:
        data["password"] = password
    r = client.post("/api/imports", files=[("files", (n, b, "application/pdf")) for n, b in files], data=data)
    assert r.status_code == 200, r.text
    return r.json()


def test_auth_required(app_client):
    assert app_client.get("/api/versions").status_code == 401
    login(app_client)
    assert app_client.get("/api/versions").status_code == 200
    r = app_client.post("/api/versions/1/restore", headers={"X-Requested-With": ""})
    assert r.status_code == 403  # CSRF header enforced
    app_client.post("/api/auth/logout")
    assert app_client.get("/api/versions").status_code == 401
    assert app_client.post("/api/auth/login", json={"password": "nope-nope"}).status_code == 401
    assert app_client.post("/api/auth/login", json={"password": "correct-horse"}).status_code == 200


def test_full_import_flow(app_client, sheet_bytes, note_bytes, encrypted_note):
    c = app_client
    login(c)
    assert c.get("/api/portfolio/overview").status_code == 409  # no sheet yet
    v1 = upload_sheet(c, sheet_bytes)
    assert v1["source"] == "initial"

    # wrong password -> file flagged, retry with right password works
    b = stage(c, [("a.pdf", encrypted_note)], password="bad")
    assert b["files"][0]["status"] == "password_error"
    b = c.post(f"/api/imports/{b['id']}/retry", json={"password": "EHJPG1234", "save_password": True}).json()
    assert b["files"][0]["status"] == "parsed"
    assert c.get("/api/settings").json()["has_pdf_password"] is True
    assert b["summary"]["new"] == 2
    items = b["items"]
    assert {i["proposal"]["mode"] for i in items} == {"new"}

    # commit without sector -> validation error, nothing written
    r = c.post(f"/api/imports/{b['id']}/commit", json={"decisions": []})
    assert r.status_code == 422 and "sector" in r.json()["detail"]

    decisions = [
        {"item_id": items[0]["id"], "include": True, "mode": "new", "name": "ITC", "sector": "FMCG", "code": "ITC"},
        {"item_id": items[1]["id"], "include": True, "mode": "new", "name": "TATA MOTORS PV", "sector": "Automobile"},
    ]
    p = c.post(f"/api/imports/{b['id']}/preview", json={"decisions": decisions}).json()
    assert all(ch["passed"] for ch in p["checks"]) and len([r for r in p["rows"] if r["kind"] == "lot"]) == 2
    assert len(c.get("/api/versions").json()) == 1  # preview writes nothing
    done = c.post(f"/api/imports/{b['id']}/commit", json={"decisions": decisions}).json()
    assert done["status"] == "committed"
    vers = c.get("/api/versions").json()
    assert len(vers) == 2 and vers[0]["is_head"] and vers[0]["source"] == "import"

    # the head sheet contains the new rows
    wb = openpyxl.load_workbook(io.BytesIO(c.get("/api/sheet/download").content))
    names = [ws_row[0].value for ws_row in wb["Shares"].iter_rows(min_row=3, max_row=45)]
    assert "ITC" in names and "TATA MOTORS PV" in names

    # same note again (saved password used) -> transaction level duplicates
    b2 = stage(c, [("again.pdf", encrypted_note), ("plain.pdf", note_bytes)])
    assert b2["files"][0]["status"] == "parsed" and b2["files"][1]["status"] == "duplicate_file"
    assert b2["summary"]["duplicates"] == 2 and b2["summary"]["new"] == 0
    assert all(i["proposal"]["mode"] == "existing" for i in b2["items"])  # remembered ISIN -> sheet name

    # diff / download / restore
    d = c.get(f"/api/versions/{vers[0]['id']}/diff").json()
    assert {x["name"] for x in d["added_lots"]} == {"ITC", "TATA MOTORS PV"}
    assert set(d["added_summary"]) == {"ITC", "TATA MOTORS PV"}
    old = c.get(f"/api/versions/{v1['id']}/download")
    assert old.status_code == 200 and old.content == sheet_bytes
    r = c.post(f"/api/versions/{v1['id']}/restore").json()
    assert r["source"] == "restore" and len(c.get("/api/versions").json()) == 3

    # analytics endpoints work offline from sheet prices
    ov = c.get("/api/portfolio/overview").json()
    assert ov["meta"]["price_source"] == "sheet" and ov["kpis"]["holdings"] == 1
    assert c.get("/api/portfolio/tax").status_code == 200
    assert c.get("/api/portfolio/charges").status_code == 200


def test_manual_entry_dedup(app_client, sheet_bytes, note_bytes):
    """A transaction typed into the sheet by hand must not be added again."""
    import datetime as dt

    from app.dao.xlsx.invt_sheet import InvtSheetDAO
    from app.domain.models import NewLot

    manual, _ = InvtSheetDAO().append(sheet_bytes, [NewLot("ITC", "FMCG", dt.date(2026, 10, 1), 10, 260, 3.01)])
    login(app_client)
    upload_sheet(app_client, manual)
    b = stage(app_client, [("n.pdf", note_bytes)])
    st = {i["trade"]["security_name"]: i["status"] for i in b["items"]}
    assert st == {"ITC LTD": "duplicate", "TATA MOTORS PASS VEH LTD": "new"}
    itc = next(i for i in b["items"] if i["trade"]["security_name"] == "ITC LTD")
    assert itc["proposal"]["include"] is False and itc["proposal"]["name"] == "ITC"


def test_bad_uploads(app_client):
    login(app_client)
    r = app_client.post("/api/versions/upload", files={"file": ("x.xlsx", b"not a zip", "application/octet-stream")})
    assert r.status_code == 415
    r = app_client.post("/api/imports", files=[("files", ("x.pdf", b"PK...", "application/pdf"))])
    assert r.status_code == 415
