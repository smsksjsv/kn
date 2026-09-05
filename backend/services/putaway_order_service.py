"""FASE R2 — PUTAWAY ORDER (PA): dokumen pemindahan roll dari gedung transit ke
gedung penyimpanan, dengan enforcement `storage_rules` + BTG (Bukti Terima Gudang).

SSOT: perpindahan antar-gedung mengubah `roll.warehouse_id` lalu `rebuild_balance`
kedua sisi (balance = proyeksi dari rolls, tidak pernah $inc). Kepemilikan TIDAK
berubah (bukan interco). Journey: putaway_assigned → putaway_in_transit → stored
(atau gate_exception untuk item yang tak tervalidasi saat tiba).
"""
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from db import db
from core_utils import new_id, now_iso, next_doc_number, safe_doc
from services import warehouse_profile_service as whp
from services.rfid_print_service import set_journey


async def _rolls_ready(warehouse_id: str, scope_ids: List[str], limit: int = 2000) -> List[Dict[str, Any]]:
    """Roll di gudang transit yang siap putaway: tag_verified + routing store."""
    rolls = await db.inventory_rolls.find({
        "warehouse_id": warehouse_id, "owner_entity_id": {"$in": scope_ids},
        "length_remaining": {"$gt": 0},
        "journey.stage": "tag_verified",
        "journey.routing": {"$ne": "cross_dock"},
    }, {"_id": 0}).to_list(limit)
    pids = list({r["product_id"] for r in rolls})
    prods = {p["id"]: p for p in await db.products.find(
        {"id": {"$in": pids}}, {"_id": 0, "id": 1, "sku": 1, "name": 1, "category": 1}).to_list(3000)}
    for r in rolls:
        p = prods.get(r["product_id"], {})
        r["sku"], r["product_name"], r["category"] = p.get("sku", ""), p.get("name", ""), p.get("category", "")
    return rolls


async def suggest(warehouse_from: str, scope_ids: List[str]) -> Dict[str, Any]:
    """Kelompokkan roll siap-putaway per (owner, kategori) + kandidat gudang tujuan
    berdasarkan roles=storage + storage_rules match (site sama diprioritaskan)."""
    src = await db.warehouses.find_one({"id": warehouse_from}, {"_id": 0})
    if not src:
        raise HTTPException(status_code=404, detail="Gudang asal tidak ditemukan")
    rolls = await _rolls_ready(warehouse_from, scope_ids)
    storages = await db.warehouses.find(
        {"active": {"$ne": False}, "roles": "storage", "id": {"$ne": warehouse_from}},
        {"_id": 0}).to_list(200)
    groups: Dict[str, Dict[str, Any]] = {}
    for r in rolls:
        key = f"{r.get('owner_entity_id')}|{r.get('category') or '—'}|{(r.get('grade') or 'A').upper()}"
        g = groups.setdefault(key, {
            "owner_entity_id": r.get("owner_entity_id"), "category": r.get("category") or "",
            "grade": (r.get("grade") or "A").upper(),
            "rolls": [], "qty": 0.0, "unit": r.get("unit", "meter"), "candidates": None})
        g["rolls"].append({k: r.get(k) for k in (
            "id", "roll_no", "sku", "product_name", "category", "grade",
            "length_remaining", "unit", "lot", "rfid_tag_id")})
        g["qty"] += float(r.get("length_remaining") or 0)
    for g in groups.values():
        cands = []
        for wh in storages:
            check = whp.check_storage_rules(wh, g["category"], g["grade"])
            if check["ok"]:
                cands.append({"warehouse_id": wh["id"], "warehouse_name": wh.get("name", ""),
                              "site_id": wh.get("site_id", ""),
                              "same_site": wh.get("site_id") and wh.get("site_id") == src.get("site_id"),
                              "rules_mode": (wh.get("storage_rules") or {}).get("mode", "none")})
        cands.sort(key=lambda c: (not c["same_site"], c["rules_mode"] == "none"))
        g["candidates"] = cands
        g["qty"] = round(g["qty"], 2)
    return {"warehouse_from": {"id": src["id"], "name": src.get("name", "")},
            "ready_count": len(rolls), "groups": list(groups.values())}


async def create_order(warehouse_from: str, warehouse_to: str, roll_ids: List[str],
                       scope_ids: List[str], actor_name: str) -> Dict[str, Any]:
    if not roll_ids:
        raise HTTPException(status_code=400, detail="Pilih minimal satu roll")
    if warehouse_from == warehouse_to:
        raise HTTPException(status_code=400, detail="Gudang asal dan tujuan sama")
    wh_to = await db.warehouses.find_one({"id": warehouse_to}, {"_id": 0})
    wh_from = await db.warehouses.find_one({"id": warehouse_from}, {"_id": 0})
    if not wh_to or not wh_from:
        raise HTTPException(status_code=404, detail="Gudang tidak ditemukan")
    rolls = await db.inventory_rolls.find(
        {"id": {"$in": roll_ids}, "warehouse_id": warehouse_from,
         "owner_entity_id": {"$in": scope_ids}}, {"_id": 0}).to_list(len(roll_ids) + 5)
    if len(rolls) != len(set(roll_ids)):
        raise HTTPException(status_code=400,
                            detail="Sebagian roll tidak ditemukan di gudang asal / di luar entitas")
    owners = {r.get("owner_entity_id") for r in rolls}
    if len(owners) > 1:
        raise HTTPException(status_code=400, detail="Satu PA hanya untuk satu pemilik barang")
    pids = list({r["product_id"] for r in rolls})
    prods = {p["id"]: p for p in await db.products.find(
        {"id": {"$in": pids}}, {"_id": 0, "id": 1, "sku": 1, "name": 1, "category": 1}).to_list(3000)}
    items, violations = [], []
    for r in rolls:
        stage = (r.get("journey") or {}).get("stage")
        if stage not in ("tag_verified",):
            violations.append(f"Roll {r.get('roll_no')} belum terverifikasi (stage: {stage or '—'})")
            continue
        p = prods.get(r["product_id"], {})
        check = whp.check_storage_rules(wh_to, p.get("category", ""), r.get("grade", "A"))
        if not check["ok"]:
            violations.append(check["reason"])
            continue
        tag = await db.rfid_tags.find_one({"id": r.get("rfid_tag_id")}, {"_id": 0, "epc": 1}) or {}
        items.append({
            "roll_id": r["id"], "roll_no": r.get("roll_no", ""), "epc": tag.get("epc", ""),
            "sku": p.get("sku", ""), "product_name": p.get("name", ""),
            "category": p.get("category", ""), "grade": r.get("grade", ""),
            "product_id": r["product_id"], "lot": r.get("lot", ""),
            "qty": float(r.get("length_remaining") or 0), "unit": r.get("unit", "meter"),
            "status": "pending",
        })
    if violations:
        raise HTTPException(status_code=400, detail=" | ".join(violations[:5]))
    order = {
        "id": new_id("pa"),
        "pa_number": await next_doc_number("putaway_orders", "pa_number", "PA"),
        "from_warehouse_id": warehouse_from, "from_warehouse_name": wh_from.get("name", ""),
        "to_warehouse_id": warehouse_to, "to_warehouse_name": wh_to.get("name", ""),
        "owner_entity_id": list(owners)[0],
        "items": items, "item_count": len(items),
        "total_qty": round(sum(i["qty"] for i in items), 2),
        "status": "open", "btg_number": None,
        "created_at": now_iso(), "created_by": actor_name,
        "dispatched_at": None, "confirmed_at": None, "confirmed_by": None,
    }
    await db.putaway_orders.insert_one(dict(order))
    await set_journey([i["roll_id"] for i in items], "putaway_assigned", {"putaway_order_id": order["id"]})
    return safe_doc(order)


async def list_orders(scope_ids: List[str], warehouse_id: Optional[str],
                      status: Optional[str], limit: int = 100) -> List[Dict[str, Any]]:
    q: Dict[str, Any] = {"owner_entity_id": {"$in": scope_ids}}
    if warehouse_id:
        q["$or"] = [{"from_warehouse_id": warehouse_id}, {"to_warehouse_id": warehouse_id}]
    if status:
        q["status"] = status
    orders = await db.putaway_orders.find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return [safe_doc(o) for o in orders]


async def _get(order_id: str, scope_ids: List[str]) -> Dict[str, Any]:
    order = await db.putaway_orders.find_one({"id": order_id}, {"_id": 0})
    if not order:
        raise HTTPException(status_code=404, detail="Putaway Order tidak ditemukan")
    if order.get("owner_entity_id") not in scope_ids:
        raise HTTPException(status_code=403, detail="PA di luar entitas Anda")
    return order


async def dispatch(order_id: str, scope_ids: List[str]) -> Dict[str, Any]:
    order = await _get(order_id, scope_ids)
    if order["status"] != "open":
        raise HTTPException(status_code=400, detail=f"PA berstatus {order['status']}")
    await db.putaway_orders.update_one({"id": order_id}, {"$set": {
        "status": "in_transit", "dispatched_at": now_iso(), "updated_at": now_iso()}})
    await set_journey([i["roll_id"] for i in order["items"]], "putaway_in_transit")
    return await _get(order_id, scope_ids)


async def confirm_arrival(order_id: str, scanned_epcs: Optional[List[str]],
                          scope_ids: List[str], actor_name: str) -> Dict[str, Any]:
    """Validasi tiba di gate-in gudang tujuan. Bila `scanned_epcs` diberikan (gate/
    handheld), hanya item yang EPC-nya terbaca yang dipindah; sisanya EXCEPTION dan
    tetap di gudang asal. Terbit BTG untuk item yang sah."""
    from services.roll_service import rebuild_balance
    order = await _get(order_id, scope_ids)
    if order["status"] not in ("open", "in_transit"):
        raise HTTPException(status_code=400, detail=f"PA berstatus {order['status']}")
    # INV-ATOMIC-01 (T-01 Opsi B) — klaim PA sebelum roll/tag/mutasi ditulis (bulk).
    from services import atomic_claim as _saga
    await _saga.claim("putaway_orders", order_id, "putaway_confirm_arrival",
                      precondition={"status": {"$in": ["open", "in_transit"]}}, actor=actor_name)
    scanned = {e.strip().upper() for e in (scanned_epcs or []) if e and e.strip()}
    arrived, exceptions = [], []
    for item in order["items"]:
        if scanned and (item.get("epc") or "").upper() not in scanned:
            item["status"] = "exception"
            exceptions.append(item)
        else:
            item["status"] = "arrived"
            arrived.append(item)
    now = now_iso()
    segs = set()
    # Volume ribuan roll/hari → bulk write, bukan N await per roll.
    from pymongo import UpdateOne
    roll_ops, tag_ops, movements = [], [], []
    for item in arrived:
        roll_ops.append(UpdateOne({"id": item["roll_id"]}, {"$set": {
            "warehouse_id": order["to_warehouse_id"], "bin_id": None, "updated_at": now}}))
        tag_ops.append(UpdateOne({"roll_id": item["roll_id"], "status": "active"},
                                 {"$set": {"warehouse_id": order["to_warehouse_id"]}}))
        for wh, mtype in ((order["from_warehouse_id"], "putaway_transfer_out"),
                          (order["to_warehouse_id"], "putaway_transfer_in")):
            movements.append({
                "id": new_id("mov"), "product_id": item["product_id"], "warehouse_id": wh,
                "owner_entity_id": order["owner_entity_id"], "movement_type": mtype,
                "quantity": item["qty"] if mtype.endswith("_in") else -item["qty"],
                "unit": item["unit"], "lot": item.get("lot", ""), "roll_id": item["roll_id"],
                "qty_rolls": 1, "source_document": order["pa_number"], "timestamp": now,
            })
        segs.add((item["product_id"], order["from_warehouse_id"]))
        segs.add((item["product_id"], order["to_warehouse_id"]))
    if roll_ops:
        await db.inventory_rolls.bulk_write(roll_ops)
        await db.rfid_tags.bulk_write(tag_ops)
        await db.inventory_movements.insert_many(movements)
    for pid, wid in segs:
        await rebuild_balance(pid, wid, order["owner_entity_id"])
    if arrived:
        await set_journey([i["roll_id"] for i in arrived], "stored", {"putaway_order_id": order_id})
    if exceptions:
        await set_journey([i["roll_id"] for i in exceptions], "gate_exception",
                          {"exception_reason": "EPC tidak terbaca saat konfirmasi tiba",
                           "putaway_order_id": order_id})
    btg = await next_doc_number("putaway_orders", "btg_number", "BTG") if arrived else None
    status = "completed" if not exceptions else ("completed_with_exception" if arrived else "exception")
    await db.putaway_orders.update_one({"id": order_id}, _saga.finish_set({
        "items": order["items"], "status": status, "btg_number": btg,
        "arrived_count": len(arrived), "exception_count": len(exceptions),
        "confirmed_at": now, "confirmed_by": actor_name, "updated_at": now}))
    return await _get(order_id, scope_ids)


async def resolve_exception(order_id: str, roll_ids: List[str], action: str,
                            scope_ids: List[str], actor_name: str) -> Dict[str, Any]:
    """Checker (handheld scan ulang di ERP): `accept` = barang ternyata sah → pindahkan;
    `return_transit` = kembalikan status siap-putaway di gudang asal."""
    from services.roll_service import rebuild_balance
    order = await _get(order_id, scope_ids)
    if action not in ("accept", "return_transit"):
        raise HTTPException(status_code=400, detail="Aksi harus 'accept' atau 'return_transit'")
    target = [i for i in order["items"] if i["status"] == "exception" and i["roll_id"] in set(roll_ids)]
    if not target:
        raise HTTPException(status_code=404, detail="Tidak ada item exception yang cocok")
    now = now_iso()
    segs = set()
    for item in target:
        if action == "accept":
            item["status"] = "arrived"
            await db.inventory_rolls.update_one({"id": item["roll_id"]}, {"$set": {
                "warehouse_id": order["to_warehouse_id"], "bin_id": None, "updated_at": now}})
            await db.rfid_tags.update_one({"roll_id": item["roll_id"], "status": "active"},
                                          {"$set": {"warehouse_id": order["to_warehouse_id"]}})
            segs.add((item["product_id"], order["from_warehouse_id"]))
            segs.add((item["product_id"], order["to_warehouse_id"]))
            await set_journey([item["roll_id"]], "stored", {"exception_resolved_by": actor_name})
        else:  # return_transit
            item["status"] = "returned_transit"
            await set_journey([item["roll_id"]], "tag_verified", {"exception_resolved_by": actor_name})
    for pid, wid in segs:
        await rebuild_balance(pid, wid, order["owner_entity_id"])
    still_exc = [i for i in order["items"] if i["status"] == "exception"]
    arrived_any = any(i["status"] == "arrived" for i in order["items"])
    status = "completed" if not still_exc else "completed_with_exception"
    upd: Dict[str, Any] = {"items": order["items"], "status": status,
                           "exception_count": len(still_exc), "updated_at": now}
    if arrived_any and not order.get("btg_number"):
        upd["btg_number"] = await next_doc_number("putaway_orders", "btg_number", "BTG")
    await db.putaway_orders.update_one({"id": order_id}, {"$set": upd})
    return await _get(order_id, scope_ids)
