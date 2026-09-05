"""Sales orders router (extra): read/preview/stats + lifecycle-action endpoints.

Dipisah dari `routers/sales_orders.py` (yang menyimpan create/get/update) agar tiap
file di bawah batas guardrail (<800 baris). Semua endpoint memakai prefix /api yang
sama; router ini DIREGISTER SEBELUM `sales_orders` di server.py agar path spesifik
(mis. /sales-orders/frequent-products) tetap match sebelum /sales-orders/{order_id}.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Request
from pymongo import ReturnDocument
from db import db
from dependencies import require_permission, audit
from core_utils import now_iso, new_id, safe_doc, DEFAULT_ENTITY_ID, strip_cost_fields, rupiah
from schemas import (AllocationPreviewIn, RollReconcilePreviewIn, RepeatRestockIn, SoReallocateIn,
                     SoReleaseRollsIn)
from services import restock_service        # PS-21 — repeat/restock SO → PR
from services import sales_ownership        # FASE E-8 (E8.4/US11) — "Pesanan Saya"
from services import line_scope             # FASE L — pagar & penyaring lini produk
from pagination import (is_paged, get_page_params, build_search, merge_query,
                       fetch_page, envelope)
from services.inventory_service import expire_old_reservations
from services.roll_service import (
    release_order_rolls, set_order_rolls_status,
    preview_line_allocation, deliver_order_rolls, compute_roll_reconcile,
    reserve_specific_rolls, allocations_from_reserved_rolls,
    record_reservation_movements, rebuild_balance,
)
from services.config_service import (
    evaluate_approval, role_satisfies, get_allocation_policy, get_effective_settings,
)
from services import so_approvals
from services.fulfillment_service import classify_lines
from services.fulfillment_status import recompute_so_status, create_outbound_tasks_for_order
from services.so_status import stage_fields
from services.uom_service import to_base, load_fixed_factors
from entity_scope import (entity_ctx, resolve_list_scope, assert_entity_access,
                          resolve_requested_entity)
from services.sales_order_helpers import (
    norm_backorder as _norm_backorder,
    so_transition as _transition,
    compute_frequent_products as _compute_frequent_products,
)

router = APIRouter(prefix="/api")


@router.post("/sales-orders/preview-allocation")
async def preview_allocation(payload: AllocationPreviewIn, request: Request) -> Dict[str, Any]:
    """Sub-fase 1.4 — ATP & Fulfillment Modes (READ-ONLY).

    Mengklasifikasikan SUMBER PEMENUHAN per baris (from_stock / from_incoming /
    inter_company / backorder) + ATP per item SEBELUM order dibuat. Dipakai POS
    agar Sales tahu risiko pemenuhan. Tidak memutasi stok / tidak mereservasi.
    """
    await require_permission(request, "order", "view")
    # ── FASE E-0 (E0.8g / L21 — KRITIS) ─────────────────────────────────────
    # Dulu: `payload.entity_id` dipakai mentah, lalu jatuh ke `DEFAULT_ENTITY_ID`.
    # Akibatnya sales CV Kanda Suka mendapat pratinjau ATP MILIK PT Kain Suka Cita
    # ("Stok on-hand cukup, dapat langsung direservasi", 788 yard) padahal stok
    # entitasnya sendiri 7 yard → sales menjanjikan barang yang bukan miliknya.
    # Sekarang: entitas WAJIB dari konteks; `payload.entity_id` divalidasi ∈ allowed.
    ctx = await entity_ctx(request)
    entity_id = resolve_requested_entity(ctx, payload.entity_id)
    if not (payload.entity_id or "").strip() and payload.customer_id:
        cust = await db.customers.find_one({"id": payload.customer_id},
                                          {"_id": 0, "entity_id": 1})
        cust_ent = (cust or {}).get("entity_id")
        if cust_ent:
            # Pelanggan milik entitas lain tidak boleh dipakai sebagai pintu belakang.
            entity_id = resolve_requested_entity(ctx, cust_ent)
    # Sub-fase 1.13 — preview pakai base_quantity agar konsisten dengan create_order.
    # S-5 (Gelombang 2) — lookup produk terarah by id (aman utk katalog besar).
    _pids = list({it.product_id for it in payload.items})
    products = {p["id"]: p for p in await db.products.find(
        {"id": {"$in": _pids}}, {"_id": 0}).to_list(len(_pids) + 1)}
    fixed_factors = await load_fixed_factors()
    items = []
    for it in payload.items:
        prod = products.get(it.product_id, {})
        try:
            bq = to_base(prod, float(it.quantity or 0), it.unit, fixed_factors) if prod else float(it.quantity or 0)
        except Exception:  # noqa: BLE001 — preview read-only; jangan gagal keras
            bq = float(it.quantity or 0)
        items.append({"product_id": it.product_id, "quantity": bq, "unit": prod.get("base_unit", "meter")})
    return await classify_lines(items, entity_id)


@router.post("/sales-orders/preview-lots")
async def preview_lots(payload: AllocationPreviewIn, request: Request) -> Dict[str, Any]:
    """Mixed-Lot Confirmation (READ-ONLY) — rencana LOT per baris sebelum order dibuat.

    Menerapkan allocation policy aktif (system→customer). Untuk tiap baris mengembalikan
    lot_mode (single/mixed), lot yang dipakai, qty terpenuhi/backorder, penjelasan, dan
    `requires_confirmation` (true bila kebijakan prefer_single tapi hasil lintas-lot).
    Tidak memutasi stok. Dipakai POS untuk dialog konfirmasi mixed-lot.
    """
    await require_permission(request, "order", "view")
    # FASE E-0 (L21) — entitas dari konteks; payload divalidasi ∈ allowed.
    ctx = await entity_ctx(request)
    entity_id = resolve_requested_entity(ctx, payload.entity_id)
    customer = None
    if payload.customer_id:
        customer = await db.customers.find_one({"id": payload.customer_id}, {"_id": 0})
        if not (payload.entity_id or "").strip() and (customer or {}).get("entity_id"):
            entity_id = resolve_requested_entity(ctx, customer["entity_id"])
    city = ""
    if customer:
        addrs = customer.get("addresses") or []
        city = (addrs[0].get("city") if addrs else "") or customer.get("city", "")
    policy = await get_allocation_policy(entity_id, customer)
    products = {p["id"]: p for p in await db.products.find({}, {"_id": 0}).to_list(2000)}
    prod_names = {pid: p.get("name", pid) for pid, p in products.items()}
    fixed_factors = await load_fixed_factors()

    lines = []
    requires_any = False
    for it in payload.items:
        prod = products.get(it.product_id, {})
        try:
            bq = to_base(prod, float(it.quantity or 0), it.unit, fixed_factors) if prod else float(it.quantity or 0)
        except Exception:  # noqa: BLE001 — preview read-only
            bq = float(it.quantity or 0)
        plan = await preview_line_allocation(it.product_id, bq, city, entity_id, policy,
                                             customer_id=payload.customer_id)
        plan["product_name"] = prod_names.get(it.product_id, it.product_id)
        requires_any = requires_any or plan["requires_confirmation"]
        lines.append(plan)
    return {
        "entity_id": entity_id,
        "policy": {"lot_mode": policy.get("lot_mode"), "lot_selection": policy.get("lot_selection"),
                   "location_pref": policy.get("location_pref")},
        "requires_confirmation": requires_any,
        "lines": lines,
    }


@router.get("/sales-orders")
async def list_orders(request: Request, status: str = None, customer_id: str = None,
                     entity_id: str = None, mine: Optional[bool] = None,
                     line: str = "") -> Any:
    """Daftar pesanan.

    FASE E-8 (E8.4 · US11) — kepemilikan data sales. Sebelum ini penyaringan hanya
    per-badan-usaha, sehingga `sales2@` melihat 8 pesanan milik rekannya dan nol
    miliknya. Definisi "pesanan saya" ada di `services/sales_ownership.py` (satu
    tempat, dipakai daftar · ringkasan · detail · laporan) supaya angka ringkasan
    tidak pernah berbeda dari isi daftarnya.
    """
    actor = await require_permission(request, "order", "view")
    await expire_old_reservations()
    ctx = await entity_ctx(request)
    query = {}
    if status:
        # UI/UX 2026-06 — kartu pipeline daftar pesanan menyaring per TAHAP yang
        # mencakup beberapa status sekaligus; koma = $in.
        parts = [s.strip() for s in str(status).split(",") if s.strip()]
        query["status"] = parts[0] if len(parts) == 1 else {"$in": parts}
    if customer_id:
        query["customer_id"] = customer_id
    query = resolve_list_scope("sales_orders", query, ctx, entity_id)
    query = sales_ownership.apply_scope(query, actor, mine)
    # FASE L — pagar lini + chip `?line=`. Dipakai field TURUNAN `line_codes` (kepala
    # dokumen) supaya penyaringan memakai index, bukan membongkar `items[]`.
    query = line_scope.narrow(query, actor, line, field=line_scope.LINES_FIELD)
    # P2 — paginasi OPT-IN: hanya bila klien mengirim ?page/?page_size. Tanpa itu
    # endpoint tetap array telanjang (konsumen lama & gate verify_api_contract aman).
    # Pencarian dipindah ke server supaya "cari nomor/pelanggan/produk" tidak lagi
    # bergantung pada seluruh daftar hadir di memori peramban.
    if is_paged(request):
        page, page_size, qs, _sort = get_page_params(request)
        if qs:
            query = merge_query(query, build_search(
                qs, ["number", "customer_name", "sales_name", "items.sku", "items.product_name"]))
        items, total = await fetch_page(db.sales_orders, query, page, page_size,
                                        sort_field="created_at", sort_dir=-1)
        rows = strip_cost_fields([_norm_backorder(safe_doc(o)) for o in items], actor.get("role"))
        return envelope(rows, total, page, page_size)
    orders = await db.sales_orders.find(query, {"_id": 0}).sort("created_at", -1).to_list(200)
    # Defensif: bersihkan ObjectId yang mungkin ter-embed di sub-dokumen (mis. payments[])
    return strip_cost_fields([_norm_backorder(safe_doc(o)) for o in orders], actor.get("role"))


@router.get("/sales-orders/stats/summary")
async def get_orders_stats(request: Request, entity_id: str = None,
                           mine: Optional[bool] = None,
                           line: str = "") -> Dict[str, Any]:
    """Get statistics summary for orders monitoring."""
    actor = await require_permission(request, "order", "view")
    await expire_old_reservations()

    # Multi-Entity (RC-7/INV-4): scope identik dgn GET /sales-orders agar
    # total_orders & by_status SELALU konsisten dengan list (tanpa header = entitas
    # AKTIF; X-Entity-Id:all = semua entitas yang diizinkan).
    # FASE E-8 (E8.4) — kepemilikan sales ikut dipakai di sini karena alasan yang sama:
    # kartu "12 pesanan" di atas daftar yang berisi 0 baris adalah bug yang membuat
    # pengguna berhenti percaya pada angka.
    ctx = await entity_ctx(request)
    scope = resolve_list_scope("sales_orders", {}, ctx, entity_id)
    scope = sales_ownership.apply_scope(scope, actor, mine)
    # FASE L — kartu ringkasan WAJIB memakai penyaring lini yang sama dengan daftarnya;
    # kalau tidak, kartu "12 pesanan" berdiri di atas daftar berisi 3 baris (kelas bug
    # yang sudah pernah menghancurkan kepercayaan pada angka beranda).
    scope = line_scope.narrow(scope, actor, line, field=line_scope.LINES_FIELD)

    # Count by status
    pipeline = [
        {"$match": scope},
        {"$group": {"_id": "$status", "count": {"$sum": 1}, "total_amount": {"$sum": "$total_amount"}}}
    ]
    status_counts = {doc["_id"]: {"count": doc["count"], "total_amount": doc["total_amount"]}
                     for doc in await db.sales_orders.aggregate(pipeline).to_list(100)}

    # Reserved qty across all products
    reserved_orders = await db.sales_orders.find(
        {**scope, "status": {"$in": ["reserved", "waiting_approval", "approved"]}},
        {"_id": 0, "allocations": 1, "reservation_expires_at": 1}
    ).to_list(200)

    total_reserved_qty = sum(
        alloc.get("quantity", 0)
        for order in reserved_orders
        for alloc in order.get("allocations", [])
    )

    # Expiring soon (within 24 hours)
    expiring_soon = sum(
        1 for order in reserved_orders
        if order.get("reservation_expires_at") and
        datetime.fromisoformat(order["reservation_expires_at"]) <
        datetime.now(timezone.utc) + timedelta(hours=24)
    )

    return {
        "by_status": status_counts,
        "total_reserved_qty": total_reserved_qty,
        "expiring_soon_count": expiring_soon,
        # P2 — kartu "Backorder" di layar Pesanan dulu dihitung dari SELURUH daftar yang
        # ada di memori peramban (`orders.filter(o => o.has_backorder)`). Begitu daftarnya
        # dipaginasi, angka itu akan diam-diam mengecil mengikuti isi halaman. Dihitung di
        # server supaya kartu & daftar tidak pernah bercerita beda.
        "backorder_count": await db.sales_orders.count_documents({**scope, "has_backorder": True}),
        "total_orders": await db.sales_orders.count_documents(scope)
    }


@router.get("/sales-orders/frequent-products")
async def frequent_products(request: Request, customer_id: str = "", limit: int = 8) -> List[Dict[str, Any]]:
    """EPIC5 — "Sering dibeli customer ini" (reorder). Logika di services.sales_order_helpers."""
    actor = await require_permission(request, "order", "view")
    return await _compute_frequent_products(customer_id, limit, actor)


@router.get("/sales-orders/{order_id}/journey")
async def order_journey(order_id: str, request: Request) -> Dict[str, Any]:
    """FASE E-8 (E8.14 · US12) — **PERJALANAN PESANAN**, read-only.

    Satu endpoint untuk pertanyaan "pesanan saya di mana?" — tahapan dipesan →
    diverifikasi → disetujui → dikonfirmasi → disiapkan → dikirim → diterima →
    ditagih → dibayar, plus **sumber pemenuhan** bila kekurangannya diambil dari PT
    lain / dipesan ulang ke supplier.

    Sengaja TIDAK memberi akses layar gudang: sebelumnya menu "Operasi Gudang" dipasang
    untuk sales supaya bisa memantau, padahal `/api/wms/tasks` menolaknya 403 — menu
    mati yang mengajari pengguna bahwa galat itu wajar. Di sini sales membaca HASILnya.
    """
    actor = await require_permission(request, "order", "view")
    order = safe_doc(await db.sales_orders.find_one({"id": order_id}, {"_id": 0}))
    if not order:
        raise HTTPException(status_code=404, detail="Pesanan tidak ditemukan")
    assert_entity_access(order, "sales_orders", await entity_ctx(request))  # IDOR entitas
    sales_ownership.assert_may_open(order, actor)                            # IDOR pemilik
    from services import order_journey_service as journey_svc
    return await journey_svc.journey(order)


@router.post("/sales-orders/preview-roll-reconcile")
async def preview_roll_reconcile(payload: RollReconcilePreviewIn, request: Request) -> List[Dict[str, Any]]:
    """SALES REVAMP V2 (C2) — opsi genapkan roll (round up/down/cut) per baris per-yard."""
    await require_permission(request, "order", "view")
    ctx = await entity_ctx(request)
    # FASE E-0 (L21) — `all_entities=true` membocorkan nomor roll & kode lot PT lain
    # (mis. `RL-632A10`, `KSC/LOT-2608-0026`) → hanya untuk peran lintas-entitas.
    selling = resolve_requested_entity(ctx, payload.entity_id)
    all_ent = bool(payload.all_entities) and ctx.is_cross_entity
    if bool(payload.all_entities) and not ctx.is_cross_entity:
        raise HTTPException(
            status_code=403,
            detail="Peran Anda hanya boleh melihat roll entitas sendiri.")
    out: List[Dict[str, Any]] = []
    for it in payload.items:
        target = float(it.base_quantity or it.quantity or 0)
        rec = await compute_roll_reconcile(it.product_id, target, selling, all_entities=all_ent)
        out.append(rec)
    return out


@router.get("/sales-orders/{order_id}/restock-state")
async def restock_state(order_id: str, request: Request) -> Dict[str, Any]:
    """PS-21(a) — kandidat repeat/restock + status pendingan + PR terkait order ini."""
    await require_permission(request, "order", "view")
    order = await db.sales_orders.find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Order tidak ditemukan")
    assert_entity_access(order, "sales_orders", await entity_ctx(request))
    try:
        return await restock_service.order_restock_state(order_id)
    except restock_service.RestockError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sales-orders/{order_id}/repeat-restock")
async def repeat_restock(order_id: str, payload: RepeatRestockIn,
                         request: Request) -> Dict[str, Any]:
    """PS-21(a) — 1 klik dari SO: buat **PR** (jalur PR→PO) + notifikasi MD.

    Barang yang belum tersedia tetap tercatat sebagai **pendingan (backorder)**
    pada order — tidak ada koleksi pendingan baru (R3).
    """
    actor = await require_permission(request, "order", "update")
    order = await db.sales_orders.find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Order tidak ditemukan")
    assert_entity_access(order, "sales_orders", await entity_ctx(request))
    if payload.warehouse_id:   # E4.1 — gudang tujuan restock harus boleh dipakai
        from services import warehouse_scope_service as whscope
        await whscope.assert_usable(payload.warehouse_id, order.get("entity_id", ""),
                                   action="meminta barang masuk ke sini",
                                   field_label="Gudang tujuan")
    try:
        return await restock_service.request_repeat_restock(
            order_id, payload.items, actor, reason=payload.reason, notes=payload.notes,
            warehouse_id=payload.warehouse_id, needed_by_date=payload.needed_by_date,
            submit_now=payload.submit_now)
    except restock_service.RestockError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sales-orders/{order_id}/submit-for-approval")
async def submit_for_approval(order_id: str, request: Request) -> Dict[str, Any]:
    actor = await require_permission(request, "order", "update")
    order = safe_doc(await db.sales_orders.find_one({"id": order_id}, {"_id": 0}))
    if not order:
        raise HTTPException(status_code=404, detail="Order tidak ditemukan")
    assert_entity_access(order, "sales_orders", await entity_ctx(request))  # S#074 IDOR
    # Fase 1B — re-evaluasi kebutuhan approval dari matriks (configurable) basis grand_total
    amount = float(order.get("grand_total", order.get("total_amount", 0)) or 0)
    appr = await evaluate_approval("sales_order", amount, order.get("entity_id"))
    settings_eff = await get_effective_settings(order.get("entity_id"))
    require_val = so_approvals.require_validation_default(settings_eff)
    summ = so_approvals.summarize(order)
    # F5/RBAC — SO WAJIB divalidasi admin/manager (default). Tidak ada auto-approve oleh sales.
    needs = bool(appr["requires_approval"] or summ["has_pending"] or require_val)
    req_role = appr["required_role"] or summ["required_role"] or ("manager" if require_val else "")
    # Pastikan ada entri 'nilai' sebagai penanda validasi bila belum ada approval spesifik.
    pa = list(order.get("pending_approvals") or [])
    if needs and not any(p.get("type") == "nilai" and p.get("status") == "pending" for p in pa) \
            and not any(p.get("type") in ("kredit", "special_price") and p.get("status") == "pending" for p in pa):
        pa.append(so_approvals.make_approval(
            "nilai", required_role=req_role or "manager",
            reason="Validasi admin atas pesanan." if not appr["requires_approval"]
                   else f"Nilai order {rupiah(amount)} memerlukan persetujuan.",
            requested_by=actor["name"], requested_by_id=actor["id"], amount=amount,
        ))
    await db.sales_orders.update_one({"id": order_id}, {"$set": {
        "pending_approvals": pa,
        "approval_required": needs,
        "required_approval_role": req_role,
        "approval_amount": amount, "updated_at": now_iso(),
    }})
    if needs:
        return strip_cost_fields(
            await _transition(order_id, ["reserved", "waiting_stock"], "waiting_approval", actor["name"],
                              "order_submitted", {"required_approval_role": req_role}),
            actor.get("role"))
    # Validasi nonaktif & di bawah ambang → auto-approve + hard-commit roll
    result = await _transition(order_id, ["reserved"], "approved", actor["name"],
                               "order_auto_approved",
                               {"approval_note": "Auto-approve (validasi nonaktif, di bawah threshold)"})
    await set_order_rolls_status(order_id, "committed")
    return strip_cost_fields(result, actor.get("role"))


async def advance_so_if_all_approved(order_id: str, actor_name: str) -> Optional[Dict[str, Any]]:
    """F5 — SO naik ke Approved HANYA bila SEMUA pending_approvals = approved."""
    order = safe_doc(await db.sales_orders.find_one({"id": order_id}, {"_id": 0}))
    if not order or not so_approvals.all_approved(order):
        return None
    if order.get("status") not in ("reserved", "waiting_approval", "waiting_stock"):
        return None
    result = await _transition(order_id, ["reserved", "waiting_approval", "waiting_stock"], "approved",
                               actor_name, "order_approved", {"approved_by": actor_name})
    await set_order_rolls_status(order_id, "committed")
    return result


@router.post("/sales-orders/{order_id}/approve")
async def approve_order(order_id: str, request: Request) -> Dict[str, Any]:
    # F5/RBAC — APPROVE butuh permission order.approve (sales TIDAK punya → 403).
    actor = await require_permission(request, "order", "approve")
    order = safe_doc(await db.sales_orders.find_one({"id": order_id}, {"_id": 0}))
    if not order:
        raise HTTPException(status_code=404, detail="Order tidak ditemukan")
    assert_entity_access(order, "sales_orders", await entity_ctx(request))  # S#074 IDOR
    required = order.get("required_approval_role")
    if not role_satisfies(actor.get("role"), required):
        raise HTTPException(status_code=403,
            detail=f"Approval butuh role minimal '{required}'. Role Anda: '{actor.get('role')}'.")
    # F5 — blokir bila masih ada approval harga/kredit menunggu (putuskan dulu di Pusat Persetujuan).
    summ = so_approvals.summarize(order)
    blockers = sorted({t for t in summ["pending_types"] if t in ("kredit", "special_price")})
    if blockers:
        names = ", ".join({"kredit": "kredit", "special_price": "harga khusus"}[b] for b in blockers)
        raise HTTPException(status_code=409, detail={
            "code": "APPROVAL_PENDING",
            "message": f"Masih ada persetujuan {names} yang menunggu keputusan. "
                       f"Putuskan dulu di Pusat Persetujuan / detail SO sebelum menyetujui pesanan.",
            "pending_types": summ["pending_types"]})
    pa = list(order.get("pending_approvals") or [])
    for p in pa:
        if p.get("type") == "nilai" and p.get("status") == "pending":
            p.update({"status": "approved", "decided_by": actor["name"],
                      "decided_by_id": actor["id"], "decided_at": now_iso()})
    await db.sales_orders.update_one({"id": order_id}, {"$set": {"pending_approvals": pa, "updated_at": now_iso()}})
    result = await advance_so_if_all_approved(order_id, actor["name"])
    if result is None:
        # T-11 (audit 2026-09) — SO mungkin SUDAH naik ke approved lewat jalur otomatis
        # (so_approvals.py saat approval kredit/harga terakhir diputuskan). Pesanan sudah
        # disetujui → hasil tercapai → idempoten 200, bukan 409 INVALID_TRANSITION.
        kini = safe_doc(await db.sales_orders.find_one({"id": order_id}, {"_id": 0}))
        if kini and kini.get("status") == "approved":
            return kini
        result = await _transition(order_id, ["reserved", "waiting_approval"], "approved",
                                   actor["name"], "order_approved", {"approved_by": actor["name"]})
        await set_order_rolls_status(order_id, "committed")
    # Fase 5 — auto-kirim WhatsApp bila ada aturan aktif (best-effort, non-blocking).
    try:
        from services import delivery_service as _ds
        await _ds.dispatch_event("sales_order", order_id, "approved",
                                 order.get("entity_id"), actor["name"])
    except Exception:  # noqa: BLE001
        pass
    return result


@router.post("/sales-orders/{order_id}/confirm")
async def confirm_order(order_id: str, request: Request) -> Dict[str, Any]:
    actor = await require_permission(request, "order", "confirm")
    # FASE E-8 (E8.13) — gerbang VERIFIKASI ADMINISTRATIF (bawaan HIDUP sejak T1 2026-06).
    # Sengaja diperiksa SEBELUM transisi: kalau pesanan sudah jadi `confirmed`,
    # tugas gudang lahir dan penolakan sesudahnya tidak lagi menolong siapa pun.
    _pre = safe_doc(await db.sales_orders.find_one({"id": order_id}, {"_id": 0}))
    if _pre:
        assert_entity_access(_pre, "sales_orders", await entity_ctx(request))  # IDOR
        # Urutan mengikuti dokumen training: verifikasi Admin Sales dulu (E8.13),
        # baru persetujuan manajer, baru konfirmasi.
        from services import so_verify_service as _verify
        await _verify.assert_ready_to_confirm(_pre)
        # T1 (audit training 2026-06) — persetujuan manajer TIDAK boleh dilompati:
        # pesanan yang masih menunggu keputusan (nilai/kredit/validasi) tidak boleh
        # langsung `confirmed` dari `reserved`/`waiting_approval`.
        if _pre.get("status") != "approved" and (
                _pre.get("approval_required") or not so_approvals.all_approved(_pre)):
            raise HTTPException(
                status_code=409,
                detail=(f"Pesanan {_pre.get('number')} belum disetujui manajer "
                        f"(status: {_pre.get('status')}). Ajukan/tunggu persetujuan "
                        "nilai & kredit dulu — konfirmasi hanya untuk pesanan berstatus "
                        "Disetujui."))
    result = await _transition(order_id, ["approved", "waiting_approval", "reserved"], "confirmed",
                               actor["name"], "order_confirmed")
    # Sub-fase 1.8 — otomatis buat task outbound saat confirmed (idempotent)
    tasks = await create_outbound_tasks_for_order(order_id, actor["name"])
    if tasks:
        await audit(actor["name"], "outbound_tasks_auto_created", "sales_order", order_id,
                    {"tasks_count": len(tasks)})
    await recompute_so_status(order_id)
    _final = safe_doc(await db.sales_orders.find_one({"id": order_id}, {"_id": 0}))
    # Fase 5 — auto-kirim WhatsApp bila ada aturan aktif (best-effort, non-blocking).
    try:
        from services import delivery_service as _ds
        await _ds.dispatch_event("sales_order", order_id, "confirmed",
                                 (_final or {}).get("entity_id"), actor["name"])
    except Exception:  # noqa: BLE001
        pass
    return _final


@router.post("/sales-orders/{order_id}/mark-delivered")
async def mark_delivered(order_id: str, request: Request) -> Dict[str, Any]:
    """Sub-fase 1.8 — tandai order TERKIRIM/DITERIMA (shipped → done).
    Roll in_transit_sales → 'delivered' (keluar dari owned_qty).

    FASE E-8 (E8.6 · keputusan pemilik E8.10b#3) — dipagari izin TERSENDIRI
    `order.deliver`, bukan `order.update`. Yang boleh: **gudang** (yang benar-benar
    menyerahkan barang) dan **Admin Sales** (yang menerima konfirmasi pelanggan) —
    plus manajer/admin. DICABUT dari `sales`: orang yang mengejar target tidak boleh
    menyatakan sendiri barangnya sudah diterima pelanggan.
    """
    actor = await require_permission(request, "order", "deliver")
    order = safe_doc(await db.sales_orders.find_one({"id": order_id}, {"_id": 0}))
    if not order:
        raise HTTPException(status_code=404, detail="Order tidak ditemukan")
    assert_entity_access(order, "sales_orders", await entity_ctx(request))  # S#074 IDOR
    if order["status"] != "shipped":
        raise HTTPException(status_code=409,
                            detail=f"Hanya order 'shipped' yang bisa ditandai diterima (saat ini '{order['status']}').")
    delivered = await deliver_order_rolls(order_id)
    result = await _transition(order_id, ["shipped"], "done", actor["name"], "order_delivered",
                               {"delivered_at": now_iso()})
    await audit(actor["name"], "order_delivered", "sales_order", order_id, {"rolls_delivered": delivered})
    return strip_cost_fields(result, actor.get("role"))


@router.post("/sales-orders/{order_id}/items/{product_id}/reallocate")
async def reallocate_line_rolls(order_id: str, product_id: str, payload: SoReallocateIn,
                                request: Request) -> Dict[str, Any]:
    """ALOKASI MANUAL — Admin Sales MENGGANTI roll pilihan sistem untuk 1 baris pesanan.
    Izin `inventory.pegging` (keputusan pemenuhan — sales lapangan tidak punya).
    Hanya sebelum pesanan dikonfirmasi/dipicking. Roll pengganti wajib milik entitas
    pesanan; kebutuhan lintas-PT tetap lewat Keputusan Pemenuhan (dokumen antar-PT)."""
    actor = await require_permission(request, "inventory", "pegging")
    order = safe_doc(await db.sales_orders.find_one({"id": order_id}, {"_id": 0}))
    if not order:
        raise HTTPException(status_code=404, detail="Order tidak ditemukan")
    assert_entity_access(order, "sales_orders", await entity_ctx(request))
    if order["status"] not in ["draft", "reserved", "waiting_approval", "approved", "waiting_stock"]:
        raise HTTPException(status_code=409,
                            detail="Roll hanya bisa diganti sebelum pesanan dikonfirmasi/dipicking.")
    item = next((i for i in order.get("items", []) if i.get("product_id") == product_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Baris produk tidak ada di pesanan ini.")
    if not payload.roll_lines:
        raise HTTPException(status_code=400, detail="Pilih minimal satu roll.")

    current = await db.inventory_rolls.find(
        {"reserved_ref.id": order_id, "product_id": product_id, "status": "reserved"},
        {"_id": 0}).to_list(1000)
    cur_by_id = {r["id"]: r for r in current}
    keep_ids, new_lines = set(), []
    for ln in payload.roll_lines:
        if ln.roll_id in cur_by_id:
            keep_ids.add(ln.roll_id)   # roll lama dipertahankan apa adanya (panjang reserve tetap)
            continue
        roll = await db.inventory_rolls.find_one({"id": ln.roll_id}, {"_id": 0})
        if not roll:
            raise HTTPException(status_code=404, detail=f"Roll {ln.roll_id} tidak ditemukan.")
        if roll.get("owner_entity_id") != order.get("entity_id"):
            raise HTTPException(status_code=400,
                                detail=f"Roll {roll.get('roll_no', ln.roll_id)} milik entitas lain — "
                                       "ambil lewat Keputusan Pemenuhan (antar-PT), bukan Ganti Roll.")
        new_lines.append({"roll_id": ln.roll_id, "take_qty": round(float(ln.take_qty or 0), 2)})

    # Urutan aman: reserve roll BARU dulu (atomik, rollback internal saat gagal),
    # baru lepas roll lama — gagal di tengah tidak meninggalkan baris tanpa reservasi.
    ref = {"type": "sales_order", "id": order_id}
    newly = await reserve_specific_rolls(new_lines, ref, product_id=product_id) if new_lines else []
    dropped = [r for r in current if r["id"] not in keep_ids]
    segments = set()
    for r in dropped:
        await db.inventory_rolls.update_one(
            {"id": r["id"], "status": "reserved", "reserved_ref.id": order_id},
            {"$set": {"status": "available", "reserved_ref": None, "updated_at": now_iso()}})
        segments.add((r["warehouse_id"], r["owner_entity_id"]))
    warehouses = {w["id"]: w for w in await db.warehouses.find({}, {"_id": 0}).to_list(100)}
    final_rolls = [cur_by_id[i] for i in keep_ids] + newly
    for r in final_rolls:
        segments.add((r["warehouse_id"], r["owner_entity_id"]))
    for wid, oid in segments:
        await rebuild_balance(product_id, wid, oid)
    if newly:
        await record_reservation_movements(product_id, newly, order_id, warehouses)

    reserved_qty = round(sum(float(r.get("length_remaining", 0) or 0) for r in final_rolls), 2)
    base_qty = round(float(item.get("base_quantity") or item.get("quantity") or 0), 2)
    backorder_qty = round(base_qty - reserved_qty, 2)
    if backorder_qty < 0.01:
        backorder_qty = 0.0
    item["reserved_qty"] = reserved_qty
    item["backorder_qty"] = backorder_qty
    if item.get("qty_rolls") is not None:
        item["qty_rolls"] = len(final_rolls)

    allocations = [a for a in order.get("allocations", []) if a.get("product_id") != product_id]
    new_allocs = allocations_from_reserved_rolls(product_id, final_rolls, warehouses, status="allocated")
    for a in new_allocs:
        a["allocation_explanation"] = (f"Dialokasikan MANUAL oleh {actor['name']} "
                                       f"({len(final_rolls)} roll) — menggantikan pilihan sistem.")
    allocations.extend(new_allocs)
    backorders = [b for b in order.get("backorders", []) if b.get("product_id") != product_id]
    if backorder_qty > 0.01:
        backorders.append({
            "id": new_id("bo"), "product_id": product_id, "sku": item.get("sku", ""),
            "product_name": item.get("product_name", ""), "entity_id": order.get("entity_id"),
            "customer_city": order.get("customer_city", ""),
            "requested_qty": base_qty, "reserved_qty": reserved_qty,
            "backorder_qty": backorder_qty, "status": "waiting_stock",
            "created_at": now_iso(), "updated_at": now_iso(),
        })
    update = {
        "items": order["items"], "allocations": allocations, "backorders": backorders,
        "has_backorder": any(float(b.get("backorder_qty", 0) or 0) > 0.01 for b in backorders),
        "is_split_warehouse": len({a["warehouse_id"] for a in allocations}) > 1,
        "has_mixed_lot": any(a.get("lot_mode") == "mixed" for a in allocations),
        "updated_at": now_iso(),
    }
    total_reserved = round(sum(float(i.get("reserved_qty", 0) or 0) for i in order["items"]), 2)
    if order["status"] in ("reserved", "waiting_stock"):
        update["status"] = "reserved" if total_reserved > 0.01 else "waiting_stock"
    update.update(stage_fields({**order, **update}))
    saved = await db.sales_orders.find_one_and_update(
        {"id": order_id}, {"$set": update}, projection={"_id": 0}, return_document=ReturnDocument.AFTER)
    await audit(actor["name"], "so_line_reallocated", "sales_order", order_id,
                {"product_id": product_id, "kept": len(keep_ids), "new": len(newly),
                 "released": len(dropped), "reserved_qty": reserved_qty,
                 "backorder_qty": backorder_qty})
    return strip_cost_fields(_norm_backorder(saved), actor.get("role"))


@router.post("/sales-orders/{order_id}/items/{product_id}/release-rolls")
async def release_line_rolls(order_id: str, product_id: str, payload: SoReleaseRollsIn,
                             request: Request) -> Dict[str, Any]:
    """AS-03 — Admin Sales MELEPAS SEBAGIAN roll ter-reserve pada satu baris SO pendingan.
    Status SO TETAP (tidak jadi draft); kekurangan tercatat sebagai backorder; jejak
    siapa/kapan/alasan disimpan di `reservation_releases`. Izin `inventory.pegging`."""
    actor = await require_permission(request, "inventory", "pegging")
    order = safe_doc(await db.sales_orders.find_one({"id": order_id}, {"_id": 0}))
    if not order:
        raise HTTPException(status_code=404, detail="Order tidak ditemukan")
    assert_entity_access(order, "sales_orders", await entity_ctx(request))
    if order["status"] not in ["reserved", "waiting_approval", "approved", "waiting_stock"]:
        raise HTTPException(status_code=409,
                            detail="Reservasi hanya bisa dilepas sebagian sebelum pesanan dikonfirmasi/dipicking.")
    item = next((i for i in order.get("items", []) if i.get("product_id") == product_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Baris produk tidak ada di pesanan ini.")
    current = await db.inventory_rolls.find(
        {"reserved_ref.id": order_id, "product_id": product_id, "status": "reserved"},
        {"_id": 0}).to_list(1000)
    cur_by_id = {r["id"]: r for r in current}
    wanted = [rid for rid in dict.fromkeys(payload.roll_ids) if rid]
    if not wanted:
        raise HTTPException(status_code=400, detail="Pilih minimal satu roll yang akan dilepas.")
    missing = [rid for rid in wanted if rid not in cur_by_id]
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"{len(missing)} roll tidak sedang ter-reserve untuk baris ini.")
    dropped = [cur_by_id[rid] for rid in wanted]
    kept = [r for r in current if r["id"] not in set(wanted)]
    reason = payload.reason.strip()
    segments = set()
    released_qty = 0.0
    for r in dropped:
        await db.inventory_rolls.update_one(
            {"id": r["id"], "status": "reserved", "reserved_ref.id": order_id},
            {"$set": {"status": "available", "reserved_ref": None, "updated_at": now_iso()}})
        qty = round(float(r.get("length_remaining", 0) or 0), 2)
        released_qty = round(released_qty + qty, 2)
        segments.add((r["warehouse_id"], r["owner_entity_id"]))
        await db.inventory_movements.insert_one({
            "id": new_id("mov"), "product_id": product_id, "warehouse_id": r["warehouse_id"],
            "owner_entity_id": r["owner_entity_id"], "movement_type": "release_reservation",
            "quantity": qty, "unit": r.get("unit", "meter"), "lot": r.get("lot", ""),
            "roll_id": r["id"], "qty_rolls": 1, "source_document": order_id, "timestamp": now_iso(),
            "note": f"Lepas reservasi sebagian oleh {actor['name']}: {reason}",
        })
    for wid, oid in segments:
        await rebuild_balance(product_id, wid, oid)
    warehouses = {w["id"]: w for w in await db.warehouses.find({}, {"_id": 0}).to_list(100)}
    reserved_qty = round(sum(float(r.get("length_remaining", 0) or 0) for r in kept), 2)
    base_qty = round(float(item.get("base_quantity") or item.get("quantity") or 0), 2)
    backorder_qty = round(base_qty - reserved_qty, 2)
    if backorder_qty < 0.01:
        backorder_qty = 0.0
    item["reserved_qty"] = reserved_qty
    item["backorder_qty"] = backorder_qty
    if item.get("qty_rolls") is not None:
        item["qty_rolls"] = len(kept)
    allocations = [a for a in order.get("allocations", []) if a.get("product_id") != product_id]
    if kept:
        new_allocs = allocations_from_reserved_rolls(product_id, kept, warehouses, status="allocated")
        for a in new_allocs:
            a["allocation_explanation"] = (f"{len(dropped)} roll dilepas sebagian oleh {actor['name']} "
                                           f"({released_qty:g}) — {len(kept)} roll dipertahankan.")
        allocations.extend(new_allocs)
    backorders = [b for b in order.get("backorders", []) if b.get("product_id") != product_id]
    if backorder_qty > 0.01:
        backorders.append({
            "id": new_id("bo"), "product_id": product_id, "sku": item.get("sku", ""),
            "product_name": item.get("product_name", ""), "entity_id": order.get("entity_id"),
            "customer_city": order.get("customer_city", ""),
            "requested_qty": base_qty, "reserved_qty": reserved_qty,
            "backorder_qty": backorder_qty, "status": "waiting_stock",
            "created_at": now_iso(), "updated_at": now_iso(),
        })
    entry = {
        "id": new_id("rel"), "product_id": product_id, "sku": item.get("sku", ""),
        "product_name": item.get("product_name", ""),
        "roll_ids": wanted, "roll_nos": [r.get("roll_no", "") for r in dropped],
        "qty": released_qty, "unit": item.get("unit", ""),
        "by": actor["name"], "by_id": actor.get("id", ""), "at": now_iso(), "reason": reason,
    }
    update = {
        "items": order["items"], "allocations": allocations, "backorders": backorders,
        "has_backorder": any(float(b.get("backorder_qty", 0) or 0) > 0.01 for b in backorders),
        "is_split_warehouse": len({a["warehouse_id"] for a in allocations}) > 1,
        "has_mixed_lot": any(a.get("lot_mode") == "mixed" for a in allocations),
        "updated_at": now_iso(),
    }
    update.update(stage_fields({**order, **update}))
    saved = await db.sales_orders.find_one_and_update(
        {"id": order_id}, {"$set": update, "$push": {"reservation_releases": entry}},
        projection={"_id": 0}, return_document=ReturnDocument.AFTER)
    await audit(actor["name"], "so_rolls_released_partial", "sales_order", order_id,
                {"product_id": product_id, "released": len(dropped), "kept": len(kept),
                 "qty": released_qty, "reserved_qty": reserved_qty, "backorder_qty": backorder_qty},
                reason=reason)
    return strip_cost_fields(_norm_backorder(saved), actor.get("role"))


@router.post("/sales-orders/{order_id}/release-reservation")
async def release_reservation(order_id: str, request: Request) -> Dict[str, Any]:
    """Manually release reservation without cancelling order (set to draft)."""
    actor = await require_permission(request, "order", "update")
    order = safe_doc(await db.sales_orders.find_one({"id": order_id}, {"_id": 0}))
    if not order:
        raise HTTPException(status_code=404, detail="Order tidak ditemukan")
    assert_entity_access(order, "sales_orders", await entity_ctx(request))  # S#074 IDOR
    if order["status"] not in ["reserved", "waiting_approval", "approved", "waiting_stock"]:
        raise HTTPException(status_code=409, detail="Order tidak dalam status yang di-reserve")
    # T-01 Opsi B (INV-ATOMIC-01) — klaim SO sebelum roll dilepas; tulisan akhir mencabut kunci.
    from services import atomic_claim as _saga
    await _saga.claim("sales_orders", order_id, "release_reservation",
                      precondition={"status": order["status"]}, actor=actor["name"])
    # Release reservations di level ROLL (KN_15)
    await release_order_rolls(order_id)
    # Update order to draft status
    update_data = {
        "status": "draft",
        "allocations": [],
        "backorders": [],
        "has_backorder": False,
        "updated_at": now_iso()
    }
    # F4 — sinkronkan stage/sub_status untuk status baru (draft → Reserved/...).
    update_data.update(stage_fields({**order, **update_data}))
    order = await db.sales_orders.find_one_and_update(
        {"id": order_id}, _saga.finish_set(update_data),
        projection={"_id": 0}, return_document=ReturnDocument.AFTER
    )
    await audit(actor["name"], "reservation_released", "sales_order", order_id,
                {"status": "draft", "note": "Reservasi dilepas manual"})
    return strip_cost_fields(order, actor.get("role"))


@router.post("/sales-orders/{order_id}/cancel")
async def cancel_order(order_id: str, request: Request) -> Dict[str, Any]:
    actor = await require_permission(request, "order", "update")
    order = safe_doc(await db.sales_orders.find_one({"id": order_id}, {"_id": 0}))
    if not order:
        raise HTTPException(status_code=404, detail="Order tidak ditemukan")
    assert_entity_access(order, "sales_orders", await entity_ctx(request))  # S#074 IDOR
    if order["status"] in ["done", "cancelled", "expired", "partially_shipped", "shipped"]:
        raise HTTPException(status_code=409, detail="Order tidak bisa dibatalkan (sudah terkirim sebagian/penuh atau terminal)")
    # T-01 Opsi B (INV-ATOMIC-01) — klaim SO sebelum roll dilepas + task gudang dibatalkan
    # + jurnal dibalik (3 koleksi tanpa transaksi). Kunci dicabut oleh `so_transition`.
    from services import atomic_claim as _saga
    await _saga.claim("sales_orders", order_id, "order_cancel",
                      precondition={"status": order["status"]}, actor=actor["name"])
    if order["status"] in ["reserved", "waiting_approval", "approved", "confirmed", "waiting_stock",
                            "partially_picked", "picked"]:
        await release_order_rolls(order_id)
    result = await _transition(order_id, [order["status"]], "cancelled", actor["name"], "order_cancelled")
    # S-4 (Gelombang 2) — batalkan task picking gudang yang masih aktif utk order ini
    # (antrean gudang bersih; barang order batal tidak ikut disiapkan).
    cancelled_tasks = await db.wms_tasks.update_many(
        {"order_id": order_id, "flow_type": "outbound",
         "status": {"$nin": ["dispatched", "completed", "cancelled"]}},
        {"$set": {"status": "cancelled", "cancel_reason": "SO dibatalkan",
                  "cancelled_by": actor["name"], "updated_at": now_iso()}})
    if cancelled_tasks.modified_count:
        await audit(actor["name"], "outbound_tasks_cancelled", "sales_order", order_id,
                    {"count": cancelled_tasks.modified_count, "reason": "SO dibatalkan"})
    # Gelombang 1 F-1 — jurnal balik otomatis bila order sudah berjurnal (best-effort).
    try:
        from services import gl_service
        await gl_service.reverse_order_journals(order_id, reason="order dibatalkan",
                                                actor_name=actor["name"])
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).error("Gagal reversal GL utk order %s: %s", order_id, exc)
    return strip_cost_fields(result, actor.get("role"))
