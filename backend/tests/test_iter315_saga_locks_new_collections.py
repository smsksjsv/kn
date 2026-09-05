"""Iter 315 — saga-locks meliputi koleksi baru (purchase_returns, sales_returns, putaway_orders)
+ guard reverse retur beli/jual saat status belum finalisasi (400 tanpa meninggalkan saga_lock).

Prasyarat: MONGO_URL/DB_NAME dari env; REACT_APP_BACKEND_URL untuk API publik.
"""
import os
import time
import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://kn-dev-review-1.preview.emergentagent.com").rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")
ENT = "ent_ksc"

NEW_COLLS = ["purchase_returns", "sales_returns", "putaway_orders"]


@pytest.fixture(scope="module")
def dbh():
    cli = MongoClient(MONGO_URL)
    yield cli[DB_NAME]
    cli.close()


def _login(email, password="demo12345"):
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": password}, timeout=15)
    assert r.status_code == 200, f"login {email}: {r.status_code} {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def admin_headers():
    tok = _login("admin@kainnusantara.id")
    return {"Authorization": f"Bearer {tok}", "X-Entity-Id": ENT, "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def manager_headers():
    tok = _login("manager@kainnusantara.id")
    return {"Authorization": f"Bearer {tok}", "X-Entity-Id": ENT, "Content-Type": "application/json"}


# ── (1) LOCKED_COLLECTIONS mencakup 3 koleksi baru ────────────────────────────
def test_saga_locks_lists_new_collections(dbh, admin_headers):
    """Suntik saga_lock pada satu dokumen di tiap koleksi baru → GET /api/saga-locks memuatnya."""
    injected = []  # (collection, id, was_pre_existing)
    for coll in NEW_COLLS:
        existing = dbh[coll].find_one({"id": {"$exists": True}}, {"_id": 0, "id": 1})
        if existing:
            doc_id = existing["id"]
            was_pre = True
        else:
            doc_id = f"TEST_iter315_{coll}"
            dbh[coll].insert_one({"id": doc_id, "status": "pending"})
            was_pre = False
        dbh[coll].update_one({"id": doc_id}, {"$set": {"saga_lock": {
            "action": "test_probe", "by": "iter315", "started_at": "2026-09-05T11:00:00+00:00",
            "error": "simulasi iter315"
        }}})
        injected.append((coll, doc_id, was_pre))

    try:
        r = requests.get(f"{BASE_URL}/api/saga-locks", headers=admin_headers, timeout=15)
        assert r.status_code == 200, r.text
        rows = r.json()
        colls_present = {row["collection"] for row in rows}
        for coll, doc_id, _ in injected:
            assert coll in colls_present, f"{coll} tidak muncul di GET /api/saga-locks"
            match = [row for row in rows if row["collection"] == coll and row["id"] == doc_id]
            assert match, f"row {coll}/{doc_id} tidak ada di daftar"
            assert match[0]["saga_lock"]["action"] == "test_probe"

        # release satu per satu via API
        for coll, doc_id, _ in injected:
            rr = requests.post(f"{BASE_URL}/api/saga-locks/{coll}/{doc_id}/release",
                               headers=admin_headers, timeout=15)
            assert rr.status_code == 200, rr.text
            body = rr.json()
            assert body.get("released") is True
            # verify gone from Mongo
            d = dbh[coll].find_one({"id": doc_id}, {"_id": 0, "saga_lock": 1})
            assert not d or "saga_lock" not in d
        # verify GET no longer has them
        r2 = requests.get(f"{BASE_URL}/api/saga-locks", headers=admin_headers, timeout=15)
        assert r2.status_code == 200
        rows2 = r2.json()
        for coll, doc_id, _ in injected:
            assert not [row for row in rows2 if row["collection"] == coll and row["id"] == doc_id]
    finally:
        # cleanup: hapus doc yang kami buat, unset saga_lock di dok pre-existing
        for coll, doc_id, was_pre in injected:
            if was_pre:
                dbh[coll].update_one({"id": doc_id}, {"$unset": {"saga_lock": ""}})
            else:
                dbh[coll].delete_one({"id": doc_id})


# ── (2) Non-admin ditolak ─────────────────────────────────────────────────────
def test_saga_locks_denied_for_manager(manager_headers):
    r = requests.get(f"{BASE_URL}/api/saga-locks", headers=manager_headers, timeout=10)
    assert r.status_code == 403


# ── (3) Release: collection tidak dikenal → 404 ───────────────────────────────
def test_release_unknown_collection(admin_headers):
    r = requests.post(f"{BASE_URL}/api/saga-locks/foobar_unknown/x/release",
                      headers=admin_headers, timeout=10)
    assert r.status_code == 404


# ── (4) Release: dokumen tanpa saga_lock → 404 ────────────────────────────────
def test_release_no_lock(dbh, admin_headers):
    # cari satu doc putaway yang TIDAK terkunci
    d = dbh.putaway_orders.find_one({"id": {"$exists": True}, "saga_lock": {"$exists": False}},
                                    {"_id": 0, "id": 1})
    if not d:
        pytest.skip("Tidak ada putaway_orders yang bisa dipakai untuk uji 404 no-lock")
    r = requests.post(f"{BASE_URL}/api/saga-locks/putaway_orders/{d['id']}/release",
                      headers=admin_headers, timeout=10)
    assert r.status_code == 404


# ── (5) Guard: reverse retur beli status pending → 400 tanpa saga_lock ───────
def test_reverse_purchase_return_guarded_no_saga_lock(dbh, manager_headers):
    """Cari retur beli non-finalisasi (bukan supplier_status=accepted_supplier atau stock_adjusted=False).
    Panggil /reverse → 400. Pastikan dokumen TIDAK meninggalkan saga_lock."""
    pr = dbh.purchase_returns.find_one({
        "$or": [{"stock_adjusted": {"$ne": True}}, {"supplier_status": {"$ne": "accepted_supplier"}}],
        "status": {"$nin": ["cancelled"]}, "reversed": {"$ne": True},
    }, {"_id": 0, "id": 1, "status": 1, "supplier_status": 1, "stock_adjusted": 1})
    if not pr:
        pytest.skip("Tidak ada retur beli non-finalisasi untuk uji guard")
    pr_id = pr["id"]
    # pastikan tidak ada saga_lock pre-existing
    dbh.purchase_returns.update_one({"id": pr_id}, {"$unset": {"saga_lock": ""}})
    r = requests.post(f"{BASE_URL}/api/purchase-returns/{pr_id}/reverse",
                      headers=manager_headers, json={}, timeout=15)
    assert r.status_code == 400, f"expected 400 got {r.status_code}: {r.text}"
    # setelah 400 → tidak boleh ada saga_lock
    doc = dbh.purchase_returns.find_one({"id": pr_id}, {"_id": 0, "saga_lock": 1})
    assert "saga_lock" not in (doc or {}), f"guard 400 meninggalkan saga_lock: {doc}"


# ── (6) Guard: reverse retur jual draft/pending_approval → 400 tanpa saga_lock
def test_reverse_sales_return_guarded_no_saga_lock(dbh, manager_headers):
    sr = dbh.sales_returns.find_one({
        "status": {"$in": ["draft", "pending_approval", "approved", "quarantined"]},
        "reversed": {"$ne": True},
    }, {"_id": 0, "id": 1, "status": 1})
    if not sr:
        pytest.skip("Tidak ada sales_returns non-settled untuk uji guard")
    sr_id = sr["id"]
    dbh.sales_returns.update_one({"id": sr_id}, {"$unset": {"saga_lock": ""}})
    r = requests.post(f"{BASE_URL}/api/sales-returns/{sr_id}/reverse",
                      headers=manager_headers, json={}, timeout=15)
    assert r.status_code == 400, f"expected 400 got {r.status_code}: {r.text}"
    doc = dbh.sales_returns.find_one({"id": sr_id}, {"_id": 0, "saga_lock": 1})
    assert "saga_lock" not in (doc or {}), f"guard 400 meninggalkan saga_lock: {doc}"


# ── (7) Sesudah suntik saga_lock pada sales_return non-settled → tetap 400
def test_reverse_sales_return_with_prelock_still_400(dbh, manager_headers):
    """Guard status dievaluasi SEBELUM claim → dokumen ber-saga_lock non-settled tetap 400
    (bukan 409 SAGA_IN_PROGRESS). Setelah itu bersihkan kunci."""
    sr = dbh.sales_returns.find_one({
        "status": {"$in": ["draft", "pending_approval", "approved", "quarantined"]},
        "reversed": {"$ne": True},
    }, {"_id": 0, "id": 1, "status": 1})
    if not sr:
        pytest.skip("Tidak ada sales_returns non-settled untuk uji guard prelock")
    sr_id = sr["id"]
    dbh.sales_returns.update_one({"id": sr_id}, {"$set": {"saga_lock": {
        "action": "sales_return_reverse", "by": "iter315", "started_at": "2026-09-05T11:00:00+00:00"
    }}})
    try:
        r = requests.post(f"{BASE_URL}/api/sales-returns/{sr_id}/reverse",
                          headers=manager_headers, json={}, timeout=15)
        # Guard status precede saga_lock check → 400
        assert r.status_code == 400, f"expected 400 got {r.status_code}: {r.text}"
    finally:
        dbh.sales_returns.update_one({"id": sr_id}, {"$unset": {"saga_lock": ""}})
