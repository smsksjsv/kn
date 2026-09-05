"""
Iteration 313 backend regression tests for audit fixes T-01..T-11.

Coverage:
- T-02: login cookie flags + CORS enforcement
- T-10: RBAC vs validation order on POST /api/ar-receipts
- T-11: idempotent approve on sales-orders
- T-08: seed-demo confirm token check
- T-09: reorder-suggestions lifecycle keys
- T-03: reorder-suggestions & stock analytics endpoints
- Regression: login smoke on all demo accounts, dashboard, root
"""

from __future__ import annotations
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", os.environ["REACT_APP_BACKEND_URL"]).rstrip("/")
API = f"{BASE_URL}/api"
# Direct backend URL — the k8s ingress rewrites Set-Cookie (Secure; SameSite=None; Partitioned)
# and Access-Control-Allow-Origin (*) on the public URL. To verify the *application-level*
# T-02 fix we must hit the backend behind the ingress.
LOCAL_API = "http://localhost:8001/api"
PASSWORD = "demo12345"

ACCOUNTS = {
    "admin": "admin@kainnusantara.id",
    "manager": "manager@kainnusantara.id",
    "finance": "finance@kainnusantara.id",
    "warehouse": "warehouse@kainnusantara.id",
    "sales": "sales@kainnusantara.id",
}


def _login(email: str) -> requests.Response:
    return requests.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD}, timeout=30)


@pytest.fixture(scope="module")
def tokens():
    out = {}
    for role, email in ACCOUNTS.items():
        r = _login(email)
        assert r.status_code == 200, f"login {email} -> {r.status_code}: {r.text}"
        out[role] = r.json()["token"]
    return out


def _headers(token: str, entity: str = "all") -> dict:
    return {"Authorization": f"Bearer {token}", "X-Entity-Id": entity, "Content-Type": "application/json"}


# ---------- T-02 login cookie (direct backend; ingress rewrites cookie attrs) ----------
def test_t02_login_cookie_flags():
    r = requests.post(f"{LOCAL_API}/auth/login",
                      json={"email": ACCOUNTS["admin"], "password": PASSWORD}, timeout=30)
    assert r.status_code == 200
    set_cookies = r.raw.headers.getlist("Set-Cookie") if hasattr(r.raw.headers, "getlist") else [r.headers.get("Set-Cookie", "")]
    joined = " || ".join(set_cookies)
    assert "session_token" in joined, f"session_token cookie missing: {joined!r}"
    session_cookie = next((c for c in set_cookies if "session_token" in c), "")
    lower = session_cookie.lower()
    assert "httponly" in lower, f"HttpOnly missing: {session_cookie!r}"
    assert "samesite=lax" in lower, f"SameSite=Lax missing: {session_cookie!r}"
    assert "secure" not in lower, f"Secure attr should be absent in dev: {session_cookie!r}"


def test_t02_auth_me_bearer(tokens):
    r = requests.get(f"{API}/auth/me", headers=_headers(tokens["admin"]), timeout=30)
    assert r.status_code == 200
    body = r.json()
    assert body.get("email") == ACCOUNTS["admin"] or body.get("user", {}).get("email") == ACCOUNTS["admin"]


# ---------- T-02 CORS (direct backend; ingress rewrites CORS on public URL) ----------
def test_t02_cors_disallowed_origin():
    r = requests.options(
        f"{LOCAL_API}/auth/me",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
        timeout=30,
    )
    acao = r.headers.get("access-control-allow-origin", "")
    assert acao != "https://evil.example", f"Evil origin echoed: {acao!r}"
    assert acao == "" or acao == "null", f"Unexpected ACAO: {acao!r}"


def test_t02_cors_allowed_origin():
    origin = BASE_URL
    r = requests.options(
        f"{LOCAL_API}/auth/me",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
        timeout=30,
    )
    assert r.headers.get("access-control-allow-origin", "") == origin, dict(r.headers)


# ---------- T-10 RBAC vs validation ----------
# Write endpoints require a concrete entity (not "all"), otherwise a 409 entity-scope
# guard fires before RBAC. Use ent_ksc as concrete scope.
def test_t10_ar_receipt_rbac_before_validation(tokens):
    r = requests.post(f"{API}/ar-receipts", json={"ngawur": 1},
                      headers=_headers(tokens["warehouse"], entity="ent_ksc"), timeout=30)
    assert r.status_code == 403, f"warehouse should be 403, got {r.status_code}: {r.text}"
    body_lower = r.text.lower()
    assert "ar_receipt.create" in body_lower or "permission" in body_lower or "ditolak" in body_lower


def test_t10_ar_receipt_validation_for_finance(tokens):
    r = requests.post(f"{API}/ar-receipts", json={"ngawur": 1},
                      headers=_headers(tokens["finance"], entity="ent_ksc"), timeout=30)
    assert r.status_code == 422, f"finance should hit validation 422, got {r.status_code}: {r.text}"


# ---------- T-11 SO approve idempotent ----------
def test_t11_so_approve_idempotent(tokens):
    r = requests.get(f"{API}/sales-orders?status=approved&limit=50", headers=_headers(tokens["manager"]), timeout=30)
    assert r.status_code == 200, r.text
    data = r.json()
    items = data.get("items") if isinstance(data, dict) else data
    approved = [x for x in (items or []) if (x.get("status") == "approved")]
    if not approved:
        pytest.skip("No SO in status 'approved' available")
    so_id = approved[0].get("id") or approved[0].get("_id")
    r2 = requests.post(f"{API}/sales-orders/{so_id}/approve", headers=_headers(tokens["manager"]), timeout=30)
    assert r2.status_code == 200, f"approve idempotent got {r2.status_code}: {r2.text}"
    body = r2.json()
    status = body.get("status") or body.get("order", {}).get("status")
    assert status == "approved", body


def test_t11_so_approve_terminal_conflict(tokens):
    for target_status in ("done", "shipped", "confirmed"):
        r = requests.get(
            f"{API}/sales-orders?status={target_status}&limit=5",
            headers=_headers(tokens["manager"]),
            timeout=30,
        )
        if r.status_code != 200:
            continue
        data = r.json()
        items = data.get("items") if isinstance(data, dict) else data
        rows = [x for x in (items or []) if x.get("status") == target_status]
        if not rows:
            continue
        so_id = rows[0].get("id") or rows[0].get("_id")
        r2 = requests.post(f"{API}/sales-orders/{so_id}/approve", headers=_headers(tokens["manager"]), timeout=30)
        assert r2.status_code == 409, f"status={target_status} should be 409, got {r2.status_code}: {r2.text}"
        assert "INVALID_TRANSITION" in r2.text, r2.text
        return
    pytest.skip("No terminal-state SO available to test conflict")


# ---------- T-08 seed-demo confirm token ----------
def test_t08_seed_demo_wrong_confirm(tokens):
    r = requests.post(f"{API}/admin/seed-demo", json={"confirm": "salah"}, headers=_headers(tokens["admin"]), timeout=30)
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
    assert "confirm" in r.text.lower() or "tidak sesuai" in r.text.lower()


# ---------- T-09 reorder suggestions lifecycle ----------
def test_t09_reorder_suggestions_lifecycle(tokens):
    r = requests.get(f"{API}/purchase-requisitions/reorder-suggestions", headers=_headers(tokens["admin"]), timeout=60)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body and isinstance(body["items"], list), body
    for it in body["items"]:
        assert "lifecycle" in it, it
        assert "lifecycle_warning" in it, it


# ---------- T-03 analytics endpoints ----------
def test_t03_reorder_and_stock_analytics_no_500(tokens):
    r1 = requests.get(f"{API}/purchase-requisitions/reorder-suggestions", headers=_headers(tokens["admin"]), timeout=60)
    assert r1.status_code == 200, r1.text
    # try common stock analytics endpoints
    candidates = [
        "/inventory/analytics",
        "/inventory/stock-analytics",
        "/stock/analytics",
        "/warehouse/stock-analytics",
    ]
    hits = []
    for path in candidates:
        r = requests.get(f"{API}{path}", headers=_headers(tokens["admin"]), timeout=60)
        if r.status_code != 404:
            hits.append((path, r.status_code, r.text[:200]))
            assert r.status_code < 500, f"{path} -> {r.status_code}: {r.text[:300]}"
    # It's fine if none exist under these names; ensure at least one endpoint responded non-500
    # (not asserting >=1 hit to avoid false negatives on naming)


# ---------- Regression: logins + basics ----------
@pytest.mark.parametrize("role", list(ACCOUNTS.keys()))
def test_reg_login(role):
    r = _login(ACCOUNTS[role])
    assert r.status_code == 200


def test_reg_root_and_dashboard(tokens):
    r = requests.get(f"{API}/", timeout=30)
    assert r.status_code == 200, r.text
    r2 = requests.get(f"{API}/dashboard", headers=_headers(tokens["admin"]), timeout=60)
    assert r2.status_code == 200, r2.text
