"""Iteration 314 — Verifikasi (1) endpoint /api/saga-locks, (2) CAS PO cancel,
(3) SO cancel saga, (4) visibility & release stuck lock, (5) reopen-escalation."""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest
import requests
from pymongo import MongoClient

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")

ADMIN = ("admin@kainnusantara.id", "demo12345")
MGR = ("manager@kainnusantara.id", "demo12345")
WH = ("warehouse@kainnusantara.id", "demo12345")
SALES = ("sales@kainnusantara.id", "demo12345")


def _login(email: str, password: str) -> str:
    r = requests.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": password}, timeout=15)
    assert r.status_code == 200, f"Login {email} → {r.status_code} {r.text}"
    return r.json()["token"]


def _h(token: str, entity: bool = False) -> Dict[str, str]:
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if entity:
        h["X-Entity-Id"] = "ent_ksc"
    return h


@pytest.fixture(scope="module")
def tokens() -> Dict[str, str]:
    return {"admin": _login(*ADMIN), "manager": _login(*MGR),
            "warehouse": _login(*WH), "sales": _login(*SALES)}


@pytest.fixture(scope="module")
def mongo():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


# ---------- (1) saga-locks endpoint ----------
def test_saga_locks_list_admin(tokens):
    r = requests.get(f"{BASE_URL}/api/saga-locks", headers=_h(tokens["admin"]), timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)


def test_saga_locks_list_non_admin_forbidden(tokens):
    r = requests.get(f"{BASE_URL}/api/saga-locks", headers=_h(tokens["sales"]), timeout=15)
    assert r.status_code == 403, r.text


# ---------- (2) CAS PO cancel concurrency ----------
def _find_cancellable_po(token: str) -> Optional[str]:
    r = requests.get(f"{BASE_URL}/api/purchase-orders", headers=_h(token, entity=True), timeout=20)
    if r.status_code != 200:
        return None
    for po in r.json():
        if po.get("status") in ("pending", "waiting_approval", "draft", "created"):
            return po["id"]
    return None


def test_po_cancel_cas_concurrency(tokens):
    po_id = _find_cancellable_po(tokens["admin"])
    if not po_id:
        pytest.skip("Tidak ada PO status pending/waiting_approval.")

    results: List[Tuple[int, str]] = []

    def fire():
        try:
            r = requests.post(f"{BASE_URL}/api/purchase-orders/{po_id}/cancel",
                              headers=_h(tokens["admin"], entity=True), timeout=20)
            results.append((r.status_code, r.text[:200]))
        except Exception as e:  # noqa: BLE001
            results.append((-1, str(e)))

    t1, t2 = threading.Thread(target=fire), threading.Thread(target=fire)
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted(c for c, _ in results)
    print(f"PO {po_id} concurrency results: {results}")
    assert 200 in codes, f"Expected one 200: {results}"
    # The other should be 409 or 400 (CAS lost)
    losers = [c for c in codes if c != 200]
    assert len(losers) == 1 and losers[0] in (400, 409), f"Expected one 4xx: {results}"

    # Verify final status = cancelled
    r = requests.get(f"{BASE_URL}/api/purchase-orders/{po_id}",
                     headers=_h(tokens["admin"], entity=True), timeout=15)
    assert r.status_code == 200
    assert r.json().get("status") == "cancelled"


# ---------- (3) SO cancel saga concurrency ----------
def _find_reserved_so(token: str) -> Optional[str]:
    r = requests.get(f"{BASE_URL}/api/sales-orders", headers=_h(token, entity=True), timeout=20)
    if r.status_code != 200:
        return None
    for so in r.json():
        if so.get("status") == "reserved":
            return so["id"]
    return None


def test_so_cancel_saga_concurrency(tokens, mongo):
    so_id = _find_reserved_so(tokens["admin"])
    if not so_id:
        pytest.skip("Tidak ada SO reserved.")

    results: List[Tuple[int, str]] = []

    def fire():
        try:
            r = requests.post(f"{BASE_URL}/api/sales-orders/{so_id}/cancel",
                              headers=_h(tokens["admin"], entity=True), timeout=20)
            results.append((r.status_code, r.text[:300]))
        except Exception as e:  # noqa: BLE001
            results.append((-1, str(e)))

    t1, t2 = threading.Thread(target=fire), threading.Thread(target=fire)
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted(c for c, _ in results)
    print(f"SO {so_id} concurrency results: {results}")
    assert 200 in codes, f"Expected one 200: {results}"
    losers = [c for c in codes if c != 200]
    assert len(losers) == 1 and losers[0] in (400, 409), f"Expected one 4xx: {results}"

    # After completion, saga_lock should not exist on the doc
    doc = mongo.sales_orders.find_one({"id": so_id}, {"_id": 0})
    assert doc is not None
    assert "saga_lock" not in doc, f"saga_lock masih ada: {doc.get('saga_lock')}"

    # And GET /api/sales-orders/{id} response should not contain saga_lock
    r = requests.get(f"{BASE_URL}/api/sales-orders/{so_id}",
                     headers=_h(tokens["admin"], entity=True), timeout=15)
    if r.status_code == 200:
        assert "saga_lock" not in r.json()


# ---------- (4) Stuck-lock visibility & release ----------
def _pick_any_so(mongo) -> Optional[str]:
    doc = mongo.sales_orders.find_one({}, {"id": 1})
    return doc["id"] if doc else None


def test_stuck_saga_lock_visibility_and_release(tokens, mongo):
    so_id = _pick_any_so(mongo)
    if not so_id:
        pytest.skip("Tidak ada SO.")

    # Inject saga_lock directly
    mongo.sales_orders.update_one(
        {"id": so_id},
        {"$set": {"saga_lock": {"action": "probe", "started_at": "2026-09-05T00:00:00+00:00"}}},
    )

    try:
        # Cancel now should 409 with SAGA_IN_PROGRESS
        r = requests.post(f"{BASE_URL}/api/sales-orders/{so_id}/cancel",
                          headers=_h(tokens["admin"], entity=True), timeout=15)
        assert r.status_code == 409, f"Expected 409, got {r.status_code}: {r.text}"
        body = r.json()
        detail = body.get("detail") if isinstance(body, dict) else body
        # detail may be dict or wrapped
        if isinstance(detail, dict):
            assert detail.get("code") == "SAGA_IN_PROGRESS", detail

        # GET saga-locks lists it
        r2 = requests.get(f"{BASE_URL}/api/saga-locks", headers=_h(tokens["admin"]), timeout=15)
        assert r2.status_code == 200
        locks = r2.json()
        assert any(l.get("collection") == "sales_orders" and l.get("id") == so_id for l in locks), locks

        # Release
        r3 = requests.post(f"{BASE_URL}/api/saga-locks/sales_orders/{so_id}/release",
                           headers=_h(tokens["admin"]), timeout=15)
        assert r3.status_code == 200, r3.text
        assert r3.json().get("released") is True

        # GET saga-locks no longer lists it
        r4 = requests.get(f"{BASE_URL}/api/saga-locks", headers=_h(tokens["admin"]), timeout=15)
        assert r4.status_code == 200
        assert not any(l.get("id") == so_id and l.get("collection") == "sales_orders"
                       for l in r4.json())
    finally:
        # Belt-and-suspenders cleanup
        mongo.sales_orders.update_one({"id": so_id}, {"$unset": {"saga_lock": ""}})


# ---------- (5) Escalation reopen flow ----------
def _find_outbound_task_to_escalate(token: str) -> Optional[str]:
    r = requests.get(f"{BASE_URL}/api/outbound/tasks", headers=_h(token, entity=True), timeout=20)
    if r.status_code != 200:
        return None
    for t in r.json():
        if t.get("status") in ("created", "picking"):
            return t["id"]
    return None


def test_escalation_reopen_flow(tokens, mongo):
    task_id = _find_outbound_task_to_escalate(tokens["warehouse"])
    if not task_id:
        pytest.skip("Tidak ada task outbound status created/picking.")

    # Escalate via warehouse
    r = requests.post(f"{BASE_URL}/api/outbound/tasks/{task_id}/escalate",
                      params={"reason": "uji iter314"},
                      headers=_h(tokens["warehouse"], entity=True), timeout=15)
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "escalated"

    try:
        # Force escalation.status = resolving via Mongo
        mongo.wms_tasks.update_one({"id": task_id}, {"$set": {"escalation.status": "resolving"}})

        # resolve-escalation should now 409
        r_resolve = requests.post(f"{BASE_URL}/api/outbound/tasks/{task_id}/resolve-escalation",
                                  params={"resolution_notes": "x"},
                                  headers=_h(tokens["manager"], entity=True), timeout=15)
        assert r_resolve.status_code == 409, f"Expected 409, got {r_resolve.status_code}: {r_resolve.text}"

        # GET list escalated returns this task
        r_list = requests.get(f"{BASE_URL}/api/outbound/tasks",
                              params={"status": "escalated"},
                              headers=_h(tokens["manager"], entity=True), timeout=15)
        assert r_list.status_code == 200
        assert any(t.get("id") == task_id and
                   (t.get("escalation") or {}).get("status") == "resolving"
                   for t in r_list.json())

        # reopen-escalation → 200 and status='pending_review'
        r_reopen = requests.post(f"{BASE_URL}/api/outbound/tasks/{task_id}/reopen-escalation",
                                 headers=_h(tokens["manager"], entity=True), timeout=15)
        assert r_reopen.status_code == 200, r_reopen.text
        body = r_reopen.json()
        esc = body.get("escalation") or {}
        assert esc.get("status") == "pending_review", esc
        assert esc.get("reopened_by"), esc

        # Second reopen → 409
        r_reopen2 = requests.post(f"{BASE_URL}/api/outbound/tasks/{task_id}/reopen-escalation",
                                  headers=_h(tokens["manager"], entity=True), timeout=15)
        assert r_reopen2.status_code == 409

        # resolve-escalation → 200
        r_resolve2 = requests.post(f"{BASE_URL}/api/outbound/tasks/{task_id}/resolve-escalation",
                                   params={"resolution_notes": "iter314 sukses"},
                                   headers=_h(tokens["manager"], entity=True), timeout=20)
        assert r_resolve2.status_code == 200, r_resolve2.text
    finally:
        pass  # task resolved by last step


# ---------- (6) Regresi ringan ----------
def test_inbound_escalated_list_manager(tokens):
    r = requests.get(f"{BASE_URL}/api/inbound/tasks", params={"status": "escalated"},
                     headers=_h(tokens["manager"], entity=True), timeout=15)
    assert r.status_code == 200, r.text


def test_outbound_escalated_list_manager(tokens):
    r = requests.get(f"{BASE_URL}/api/outbound/tasks", params={"status": "escalated"},
                     headers=_h(tokens["manager"], entity=True), timeout=15)
    assert r.status_code == 200, r.text
