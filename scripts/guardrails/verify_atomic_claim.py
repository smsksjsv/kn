#!/usr/bin/env python3
"""INV-ATOMIC-01 — endpoint tulis multi-koleksi memakai klaim atomik (T-01 Opsi B, saga).

Keputusan 2026-09: TANPA replica set/transaksi Mongo. Endpoint yang menulis ≥2 koleksi
substantif wajib (a) `atomic_claim.claim()` sebelum tulisan turunan + tulisan akhir
mencabut `saga_lock` (`finish_set` / `$unset`), ATAU (b) CAS: `find_one_and_update`
induk berprasyarat `status` + tulisan turunan idempoten, ATAU (c) alasan tertulis di
`REVIEWED` (≥20 huruf). Sisanya "BELUM DITINJAU" — angkanya RATCHET: hanya boleh turun
dari `BASELINE_UNREVIEWED`; entri `REVIEWED` yang fungsinya tak lagi berpola → MERAH.

Pakai:  python3 scripts/guardrails/verify_atomic_claim.py
        python3 scripts/guardrails/verify_atomic_claim.py --self-test
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import Guard, BACKEND, G, R, X  # noqa: E402
import inventaris_multi_koleksi as inv  # noqa: E402

# Baseline "BELUM DITINJAU" saat penjaga lahir (2026-09-05). Turunkan angka ini setiap
# kali satu endpoint selesai ditinjau — jangan pernah dinaikkan.
BASELINE_UNREVIEWED = 62

# (berkas router, potongan path) → (mekanisme, alasan). mekanisme ∈ {claim, cas, service, log}
REVIEWED: dict[tuple[str, str], tuple[str, str]] = {
    ("outbound_picking.py", "/resolve-escalation"): ("cas", "T-01 Langkah 1: find_one_and_update berprasyarat escalation.status $nin [resolved,resolving] → resolving (2026-09-05)"),
    ("inbound_receiving.py", "/inbound/tasks/{task_id}/complete"): ("claim", "klaim wms_tasks sebelum roll/mutasi/PO ditulis; tulisan akhir $unset saga_lock"),
    ("sales_orders_extra.py", "/sales-orders/{order_id}/cancel"): ("claim", "klaim sales_orders sebelum roll dilepas; so_transition CAS + $unset"),
    ("so_approvals.py", "/approvals/{approval_id}/decide"): ("claim", "klaim sales_orders per entri approval ($elemMatch pending); release di akhir"),
    ("transfers.py", "/transfers/{transfer_id}/approve"): ("claim", "klaim warehouse_transfers sebelum kepemilikan roll + jurnal; finish_set"),
    ("transfers.py", "/transfers/{transfer_id}/reject"): ("claim", "klaim warehouse_transfers sebelum roll dilepas; finish_set"),
    ("transfers.py", "/transfers/{transfer_id}/status"): ("claim", "klaim warehouse_transfers sebelum dispatch/receive roll; finish_set"),
    ("cycle_count.py", "/cycle-count/sessions/{session_id}/approve"): ("claim", "klaim cycle_count_sessions sebelum selisih diterapkan ke roll; finish_set"),
    ("purchase_orders_extra.py", "/purchase-orders/{po_id}/close"): ("cas", "find_one_and_update induk berprasyarat status; wms_tasks update_many idempoten"),
    ("purchase_orders_extra.py", "/purchase-orders/{po_id}/cancel"): ("cas", "find_one_and_update induk berprasyarat status; wms_tasks update_many idempoten"),
    ("vendor_bills.py", "/vendor-bills/{bill_id}/pay"): ("cas", "find_one_and_update vendor_bills berprasyarat status posted + $inc amount_paid atomik (pra-eksisting)"),
    ("sales_orders_extra.py", "/sales-orders/{order_id}/approve"): ("cas", "so_transition CAS status $in expected_from; set_order_rolls_status idempoten"),
    ("sales_orders_extra.py", "/sales-orders/{order_id}/submit-for-approval"): ("cas", "so_transition CAS status $in expected_from; set_order_rolls_status idempoten"),
    ("sales_orders_extra.py", "/sales-orders/{order_id}/confirm"): ("cas", "so_transition CAS; create_outbound_tasks_for_order dedupe per order+product"),
    ("sales_orders_extra.py", "/sales-orders/{order_id}/release-reservation"): ("claim", "klaim sales_orders sebelum roll dilepas; tulisan akhir finish_set (draft)"),
    ("purchase_returns.py", "/purchase-returns/{return_id}/reverse"): ("service", "klaim purchase_returns di service sesudah guard, sebelum JE/roll/AP/kas; finish_set", "purchase_return_service.reverse_settlement"),
    ("sales_returns.py", "/sales-returns/{return_id}/reverse"): ("service", "klaim sales_returns di service sesudah guard, sebelum JE/roll/kas/CN; finish_set", "return_service.reverse_settlement"),
    ("putaway_orders.py", "/putaway-orders/{order_id}/confirm-arrival"): ("service", "klaim putaway_orders di service sebelum bulk roll/tag/mutasi; finish_set", "putaway_order_service.confirm_arrival"),
    ("sales_orders.py", "/sales-orders"): ("compensate", "id SO baru per permintaan (tak ada dokumen bersama); roll direservasi lebih dulu dan DILEPAS (release_order_rolls) di except bila insert gagal — kompensasi saga"),
    ("auth.py", "/auth/login"): ("service", "login_attempts + sessions + users(last_login): tulisan independen, tidak ada saldo/stok — aman diulang"),
}

RE_CLAIM = re.compile(r"atomic_claim|_saga\.claim\(")
RE_FINISH = re.compile(r"finish_set\(|\$unset\"?\s*:\s*\{\s*\"saga_lock\"|so_transition|_transition\(|_saga\.release\(")
RE_CAS = re.compile(r"find_one_and_update\(\s*\{[^}]*\"(status|escalation\.status)\"|_transition\(|find_one_and_update\(\s*\n?\s*\{\"id\": bill_id, \"status\"")
RE_COMP = re.compile(r"except[^\n]*:\s*\n\s*await (release_|_release|rollback|compensate)")


def service_function_source(ref: str) -> str | None:
    """'modul.fungsi' → sumber fungsi di backend/services/modul.py (None bila tak ada)."""
    mod, _, fn = ref.rpartition(".")
    f = BACKEND / "services" / f"{mod}.py"
    if not f.exists():
        return None
    src = f.read_text(errors="ignore")
    tree = ast.parse(src)
    lines = src.splitlines()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn:
            return "\n".join(lines[n.lineno - 1:n.end_lineno])
    return None


def endpoint_sources() -> dict[tuple[str, str], str]:
    """{(router file, path): source fungsi} untuk semua endpoint tulis di backend/routers."""
    out: dict[tuple[str, str], str] = {}
    for f in sorted((BACKEND / "routers").glob("*.py")):
        src = f.read_text(errors="ignore")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for n in tree.body:
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("post", "put", "patch", "delete") and d.args:
                    p = d.args[0]
                    if isinstance(p, ast.Constant):
                        out[(f.name, str(p.value))] = "\n".join(lines[n.lineno - 1:n.end_lineno])
    return out


def unreviewed_rows() -> list[tuple[str, str]]:
    """Endpoint ≥2 koleksi substantif (dari inventaris) yang belum masuk REVIEWED."""
    rows = inv.collect_rows()
    out = []
    for fn, _ln, path, _direct, _via, allc in rows:
        if len([c for c in allc if c not in inv.LOG_ONLY]) < 2:
            continue
        if not any(fn == rf and frag in path for (rf, frag) in REVIEWED):
            out.append((fn, path))
    return out


def check(sources: dict[tuple[str, str], str], reviewed=REVIEWED, unreviewed: list | None = None,
          baseline: int = BASELINE_UNREVIEWED) -> Guard:
    g = Guard("INV-ATOMIC-01", "endpoint tulis multi-koleksi memakai klaim atomik / CAS (saga, tanpa transaksi)")
    for (rf, frag), entry in sorted(reviewed.items()):
        mech, reason = entry[0], entry[1]
        svc_ref = entry[2] if len(entry) > 2 else None
        g.bump()
        hit = [(k, s) for k, s in sources.items() if k[0] == rf and frag in k[1]]
        if not hit:
            g.add(f"REVIEWED {rf} {frag}: endpoint tidak lagi ada di kode — hapus entrinya (ratchet).")
            continue
        src = hit[0][1]
        if len(reason.strip()) < 20:
            g.add(f"{rf} {frag}: alasan REVIEWED terlalu pendek (<20 huruf).")
        if mech == "claim" and not RE_CLAIM.search(src):
            g.add(f"backend/routers/{rf} {frag}: dicatat 'claim' tetapi tidak memanggil atomic_claim.claim(). Pasang klaim atau ubah mekanisme.")
        if mech == "claim" and not RE_FINISH.search(src):
            g.add(f"backend/routers/{rf} {frag}: klaim tanpa pencabut kunci (finish_set / $unset saga_lock / so_transition / release).")
        if mech == "cas" and not RE_CAS.search(src):
            g.add(f"backend/routers/{rf} {frag}: dicatat 'cas' tetapi find_one_and_update induk tidak berprasyarat status.")
        if mech == "compensate" and not RE_COMP.search(src):
            g.add(f"backend/routers/{rf} {frag}: dicatat 'compensate' tetapi tidak ada `except → await release_/rollback` (kompensasi hilang).")
        if mech == "service" and svc_ref:
            ssrc = service_function_source(svc_ref)
            if ssrc is None:
                g.add(f"{rf} {frag}: rujukan service '{svc_ref}' tidak ditemukan di backend/services.")
            elif not RE_CLAIM.search(ssrc) or not RE_FINISH.search(ssrc):
                g.add(f"backend/services/{svc_ref}: dicatat klaim di service tetapi tidak ada atomic_claim.claim() + finish_set/release.")
    if unreviewed is not None:
        g.bump()
        if len(unreviewed) > baseline:
            g.add(f"BELUM DITINJAU = {len(unreviewed)} > baseline {baseline}. Endpoint multi-koleksi BARU wajib memakai "
                  "atomic_claim.claim()/CAS dan dicatat di REVIEWED: " +
                  "; ".join(f"{a} {b}" for a, b in unreviewed[:5]))
        elif len(unreviewed) < baseline:
            g.add(f"BELUM DITINJAU = {len(unreviewed)} < baseline {baseline}: turunkan BASELINE_UNREVIEWED ke {len(unreviewed)} (ratchet hanya turun).")
    return g


def self_test() -> int:
    fails = 0

    def case(name, sources, reviewed, expect_red, unreviewed=None, baseline=0):
        nonlocal fails
        red = bool(check(sources, reviewed, unreviewed, baseline).violations)
        ok = red == expect_red
        fails += 0 if ok else 1
        print(f"  [{G if ok else R}{'PASS' if ok else 'FAIL'}{X}] {name} → {'MERAH' if red else 'hijau'}")

    good_claim = "async def f():\n    await _saga.claim('x', i, 'a')\n    await db.x.find_one_and_update({'id': i}, _saga.finish_set({'status': 'b'}))\n"
    no_finish = "async def f():\n    await _saga.claim('x', i, 'a')\n    await db.x.update_one({'id': i}, {'$set': {'status': 'b'}})\n"
    no_claim = "async def f():\n    await db.x.update_one({'id': i}, {'$set': {'status': 'b'}})\n"
    cas_ok = "async def f():\n    u = await db.x.find_one_and_update({'id': i, \"status\": {'$in': ['a']}}, {'$set': {}})\n"
    R1 = {("r.py", "/x/{id}/go"): ("claim", "alasan yang cukup panjang untuk lolos uji")}
    R2 = {("r.py", "/x/{id}/go"): ("cas", "alasan yang cukup panjang untuk lolos uji")}
    case("claim + finish_set → hijau", {("r.py", "/x/{id}/go"): good_claim}, R1, False)
    case("claim tanpa pencabut kunci → MERAH", {("r.py", "/x/{id}/go"): no_finish}, R1, True)
    case("dicatat claim tapi tak memanggil claim() → MERAH", {("r.py", "/x/{id}/go"): no_claim}, R1, True)
    case("cas berprasyarat status → hijau", {("r.py", "/x/{id}/go"): cas_ok}, R2, False)
    case("dicatat cas tapi tanpa prasyarat status → MERAH", {("r.py", "/x/{id}/go"): no_claim}, R2, True)
    comp_ok = "async def f():\n    try:\n        await reserve()\n    except HTTPException:\n        await release_order_rolls(i)\n        raise\n"
    R3 = {("r.py", "/x/{id}/go"): ("compensate", "alasan yang cukup panjang untuk lolos uji")}
    case("compensate dengan except→release → hijau", {("r.py", "/x/{id}/go"): comp_ok}, R3, False)
    case("dicatat compensate tanpa kompensasi → MERAH", {("r.py", "/x/{id}/go"): no_claim}, R3, True)
    R4 = {("r.py", "/x/{id}/go"): ("service", "alasan yang cukup panjang untuk lolos uji", "modul_tidak_ada.fungsi")}
    case("service merujuk fungsi yang tak ada → MERAH", {("r.py", "/x/{id}/go"): no_claim}, R4, True)
    R5 = {("r.py", "/x/{id}/go"): ("service", "alasan yang cukup panjang untuk lolos uji", "putaway_order_service.confirm_arrival")}
    case("service nyata berklaim (putaway confirm_arrival) → hijau", {("r.py", "/x/{id}/go"): no_claim}, R5, False)
    R6 = {("r.py", "/x/{id}/go"): ("service", "alasan yang cukup panjang untuk lolos uji", "putaway_order_service.resolve_exception")}
    case("service nyata TANPA klaim → MERAH", {("r.py", "/x/{id}/go"): no_claim}, R6, True)
    case("entri REVIEWED basi (endpoint hilang) → MERAH", {}, R1, True)
    case("alasan pendek → MERAH", {("r.py", "/x/{id}/go"): good_claim}, {("r.py", "/x/{id}/go"): ("claim", "pendek")}, True)
    case("BELUM DITINJAU naik di atas baseline → MERAH", {}, {}, True, unreviewed=[("a.py", "/b")], baseline=0)
    case("BELUM DITINJAU turun di bawah baseline → MERAH (turunkan baseline)", {}, {}, True, unreviewed=[], baseline=1)
    case("BELUM DITINJAU == baseline → hijau", {}, {}, False, unreviewed=[("a.py", "/b")], baseline=1)
    real = check(endpoint_sources(), REVIEWED, unreviewed_rows())
    ok = not real.violations
    fails += 0 if ok else 1
    print(f"  [{G if ok else R}{'PASS' if ok else 'FAIL'}{X}] kode nyata saat ini HIJAU ({len(real.violations)} pelanggaran)")
    for v in real.violations:
        print(f"    ✗ {v[:200]}")
    print(f"{G if not fails else R}  SELF-TEST {'HIJAU' if not fails else 'MERAH'} ({fails} gagal).{X}")
    return 1 if fails else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    return check(endpoint_sources(), REVIEWED, unreviewed_rows()).finish()


if __name__ == "__main__":
    sys.exit(main())
