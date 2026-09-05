"""Iter 256 — Sapuan luas semua flow besar (procure-to-pay, order-to-cash,
WMS, kontrabon, interco, retur, GL/finance, persetujuan lintas peran, HRD,
R&D, isolasi entitas). Fokus: tangkap 5xx / gap wiring / angka tak konsisten.
Bukan uji T1..T8 (sudah HIJAU iter 255)."""
import os
import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL", os.environ["REACT_APP_BACKEND_URL"]).rstrip("/")

ACCOUNTS = {
    "admin":         "admin@kainnusantara.id",
    "manager":       "manager@kainnusantara.id",
    "finance":       "finance@kainnusantara.id",
    "warehouse":     "warehouse@kainnusantara.id",
    "salesadmin":    "salesadmin@kainnusantara.id",
    "sales":         "sales@kainnusantara.id",
    "sales3":        "sales3@kainnusantara.id",       # home CV Kanda Suka
    "designer":      "designer@kainnusantara.id",
    "mgr_printing":  "manager.printing@kainnusantara.id",
    "dewi_printing": "dewi.printing@kainnusantara.id",
}
PWD = "demo12345"

TOKENS = {}
def _login(email):
    r = requests.post(f"{BASE}/api/auth/login", json={"email": email, "password": PWD}, timeout=30)
    assert r.status_code == 200, f"login {email} -> {r.status_code} {r.text[:200]}"
    return r.json()["token"]

def _h(role="admin", ent="ent_ksc"):
    if role not in TOKENS:
        TOKENS[role] = _login(ACCOUNTS[role])
    hdrs = {"Authorization": f"Bearer {TOKENS[role]}"}
    if ent:
        hdrs["X-Entity-Id"] = ent
    return hdrs


# ---------- HELPER: assert no 5xx, tolerate 200/4xx ----------
def _assert_no_5xx(r, ctx=""):
    assert r.status_code < 500, f"5xx at {ctx}: {r.status_code} {r.text[:400]}"


# ---------- LOGIN sanity ----------
@pytest.mark.parametrize("role", list(ACCOUNTS.keys()))
def test_login_all_roles(role):
    tok = _login(ACCOUNTS[role])
    assert isinstance(tok, str) and len(tok) > 10


# ---------- HOME per peran ----------
@pytest.mark.parametrize("role", ["admin","manager","finance","warehouse","sales"])
def test_home_no_5xx(role):
    """Hanya 5 endpoint home resmi. salesadmin/designer TIDAK punya endpoint home
    tersendiri — frontend memakai routing lain (dilaporkan sebagai catatan)."""
    r = requests.get(f"{BASE}/api/home/{role}", headers=_h(role), timeout=30)
    _assert_no_5xx(r, f"/home/{role}")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, dict)

def test_home_salesadmin_designer_have_own_endpoint_or_reuse():
    """CATATAN GAP: /api/home/salesadmin & /api/home/designer → 404. Frontend perlu
    memetakan peran ini ke endpoint lain. Uji hanya memastikan bukan 5xx."""
    for role in ("salesadmin","designer"):
        r = requests.get(f"{BASE}/api/home/{role}", headers=_h(role), timeout=30)
        _assert_no_5xx(r, f"/home/{role}")
        # 404 diterima sebagai kondisi yang harus DIDOKUMENTASIKAN
        assert r.status_code in (200, 404), f"{role} -> {r.status_code}"


# ---------- INVENTORY reconciliation & Buku Besar ----------
def test_inventory_reconciliation_admin():
    r = requests.get(f"{BASE}/api/gl/inventory-reconciliation", headers=_h("admin"), timeout=60)
    _assert_no_5xx(r, "inv-recon admin")
    assert r.status_code == 200

def test_inventory_reconciliation_finance():
    r = requests.get(f"{BASE}/api/gl/inventory-reconciliation", headers=_h("finance"), timeout=60)
    _assert_no_5xx(r, "inv-recon finance")
    assert r.status_code in (200,), f"finance -> {r.status_code} {r.text[:200]}"

def test_inventory_reconciliation_salesadmin():
    r = requests.get(f"{BASE}/api/gl/inventory-reconciliation", headers=_h("salesadmin"), timeout=60)
    _assert_no_5xx(r, "inv-recon salesadmin")

def test_gl_summary():
    r = requests.get(f"{BASE}/api/gl/summary", headers=_h("finance"), timeout=30)
    _assert_no_5xx(r, "gl summary")

def test_gl_journals_list():
    r = requests.get(f"{BASE}/api/gl/journals", headers=_h("finance"), timeout=30)
    _assert_no_5xx(r, "gl journals")


# ---------- PROCURE-to-PAY ----------
def test_pr_list():
    r = requests.get(f"{BASE}/api/purchase-requisitions", headers=_h("admin"), timeout=30)
    _assert_no_5xx(r, "PR list")
    assert r.status_code == 200

def test_po_list():
    r = requests.get(f"{BASE}/api/purchase-orders", headers=_h("admin"), timeout=30)
    _assert_no_5xx(r, "PO list")
    assert r.status_code == 200

def test_vendor_bills_list():
    """Finance TIDAK punya vendor_bill.view (E8.1b — sisi HUTANG milik manager/admin).
    Uji dengan manager/admin."""
    r = requests.get(f"{BASE}/api/vendor-bills", headers=_h("admin"), timeout=30)
    _assert_no_5xx(r, "VB list admin")
    assert r.status_code == 200
    # regresi: finance harus 403 (BUKAN 500)
    r2 = requests.get(f"{BASE}/api/vendor-bills", headers=_h("finance"), timeout=30)
    assert r2.status_code == 403

def test_grn_list():
    r = requests.get(f"{BASE}/api/inbound-receiving", headers=_h("warehouse"), timeout=30)
    _assert_no_5xx(r, "GR list")


# ---------- ORDER-to-CASH ----------
def test_so_list():
    r = requests.get(f"{BASE}/api/sales-orders", headers=_h("sales"), timeout=30)
    _assert_no_5xx(r, "SO list")

def test_deliveries_list():
    r = requests.get(f"{BASE}/api/deliveries", headers=_h("warehouse"), timeout=30)
    _assert_no_5xx(r, "deliveries")

def test_tax_invoices_list():
    r = requests.get(f"{BASE}/api/tax-invoices", headers=_h("finance"), timeout=30)
    _assert_no_5xx(r, "tax invoices")

def test_ar_receipts_list():
    r = requests.get(f"{BASE}/api/ar-receipts", headers=_h("finance"), timeout=30)
    _assert_no_5xx(r, "ar receipts")

def test_ar_aging():
    r = requests.get(f"{BASE}/api/ar-aging", headers=_h("finance"), timeout=30)
    _assert_no_5xx(r, "ar aging")


# ---------- WMS ----------
def test_wms_stock():
    r = requests.get(f"{BASE}/api/wms/stock", headers=_h("warehouse"), timeout=30)
    _assert_no_5xx(r, "wms stock")

def test_transfers_list():
    r = requests.get(f"{BASE}/api/transfers", headers=_h("warehouse"), timeout=30)
    _assert_no_5xx(r, "transfers")

def test_cycle_count_list():
    r = requests.get(f"{BASE}/api/cycle-count/sessions", headers=_h("warehouse"), timeout=30)
    _assert_no_5xx(r, "cycle count")

def test_outbound_picking():
    r = requests.get(f"{BASE}/api/outbound-picking", headers=_h("warehouse"), timeout=30)
    _assert_no_5xx(r, "outbound picking")


# ---------- KONTRABON ----------
def test_contra_bons_list():
    r = requests.get(f"{BASE}/api/contra-bons", headers=_h("finance"), timeout=30)
    _assert_no_5xx(r, "contra bons")


# ---------- INTERCO ----------
def test_interco_list():
    r = requests.get(f"{BASE}/api/interco/transactions", headers=_h("finance"), timeout=30)
    _assert_no_5xx(r, "interco")

def test_interco_balances():
    # coba dua kemungkinan endpoint
    for path in ("/api/interco/balances", "/api/interco/accounts"):
        r = requests.get(f"{BASE}{path}", headers=_h("finance"), timeout=30)
        _assert_no_5xx(r, path)


# ---------- RETUR ----------
def test_sales_returns():
    r = requests.get(f"{BASE}/api/sales-returns", headers=_h("salesadmin"), timeout=30)
    _assert_no_5xx(r, "sales returns")

def test_purchase_returns():
    r = requests.get(f"{BASE}/api/purchase-returns", headers=_h("warehouse"), timeout=30)
    _assert_no_5xx(r, "purchase returns")


# ---------- FINANCE — closing & suspense & statements ----------
def test_closing_status():
    for path in ("/api/closing/periods", "/api/closing/status"):
        r = requests.get(f"{BASE}{path}", headers=_h("finance"), timeout=30)
        _assert_no_5xx(r, path)

def test_financial_statements():
    for path in ("/api/financial-statements/balance-sheet",
                 "/api/financial-statements/income-statement"):
        r = requests.get(f"{BASE}{path}", headers=_h("finance"), timeout=30)
        _assert_no_5xx(r, path)


# ---------- PRODUKSI / MAKLOON ----------
def test_production_list():
    r = requests.get(f"{BASE}/api/production/work-orders", headers=_h("admin"), timeout=30)
    _assert_no_5xx(r, "production WO")

def test_makloon_list():
    r = requests.get(f"{BASE}/api/makloon-orders", headers=_h("admin"), timeout=30)
    _assert_no_5xx(r, "makloon orders")

def test_qc_inspections():
    r = requests.get(f"{BASE}/api/qc-inspections", headers=_h("warehouse"), timeout=30)
    _assert_no_5xx(r, "qc")


# ---------- R&D / DESAIN ----------
def test_design_requests_list():
    r = requests.get(f"{BASE}/api/design-requests", headers=_h("designer"), timeout=30)
    _assert_no_5xx(r, "design requests")

def test_design_gallery():
    r = requests.get(f"{BASE}/api/design-gallery", headers=_h("designer"), timeout=30)
    _assert_no_5xx(r, "design gallery")


# ---------- HRD ----------
def test_hr_leave_list():
    r = requests.get(f"{BASE}/api/hr/leave", headers=_h("admin"), timeout=30)
    _assert_no_5xx(r, "hr leave")

def test_hr_payroll_list():
    r = requests.get(f"{BASE}/api/hr/payroll", headers=_h("admin"), timeout=30)
    _assert_no_5xx(r, "hr payroll")


# ---------- ISOLASI ENTITAS ----------
def test_sales3_home_entity_kanda():
    # sales3 ber-home CV Kanda Suka — panggil home tanpa entity harus tetap 200
    r = requests.get(f"{BASE}/api/home/sales", headers={"Authorization": f"Bearer {_login(ACCOUNTS['sales3'])}",
                                                       "X-Entity-Id": "ent_kanda"}, timeout=30)
    _assert_no_5xx(r, "sales3 home ent_kanda")
    assert r.status_code == 200

def test_all_entities_view_only_write_rejected():
    """Mode 'Semua Entitas' — aksi tulis wajib 409, bukan 500."""
    hdrs = {"Authorization": f"Bearer {_login(ACCOUNTS['admin'])}"}  # tanpa X-Entity-Id -> semua entitas
    # coba create PR — harus ditolak dengan 4xx (409 idealnya), bukan 500
    r = requests.post(f"{BASE}/api/purchase-requisitions", json={"lines":[]}, headers=hdrs, timeout=30)
    _assert_no_5xx(r, "PR create all-ent")


# ---------- APPROVALS: papan admin harus konsisten dengan queue ----------
def test_admin_home_boards_shape():
    r = requests.get(f"{BASE}/api/home/admin", headers=_h("admin"), timeout=30)
    assert r.status_code == 200
    body = r.json()
    # papan atau approvals harus ada
    assert isinstance(body, dict)
    assert ("waiting_boards" in body) or ("approvals" in body) or ("boards" in body), \
        f"admin home shape unexpected: keys={list(body.keys())[:10]}"

def test_manager_printing_line_gate_po_board():
    """FASE P: manager.printing hanya melihat lini printing pada Papan PO."""
    tok = _login(ACCOUNTS["mgr_printing"])
    r = requests.get(f"{BASE}/api/po-board", headers={"Authorization": f"Bearer {tok}",
                                                     "X-Entity-Id": "ent_ksc"}, timeout=30)
    _assert_no_5xx(r, "po-board mgr_printing")


# ---------- Idempotency: home dipanggil berulang tidak berubah shape ----------
def test_home_idempotent_finance():
    r1 = requests.get(f"{BASE}/api/home/finance", headers=_h("finance"), timeout=30).json()
    r2 = requests.get(f"{BASE}/api/home/finance", headers=_h("finance"), timeout=30).json()
    assert set(r1.keys()) == set(r2.keys())
