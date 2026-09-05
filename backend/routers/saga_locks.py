"""Kunci saga (T-01 Opsi B) — daftar & lepas `saga_lock` yang menggantung (admin)."""
from typing import Any, Dict, List
from fastapi import APIRouter, HTTPException, Request
from db import db
from dependencies import require_role, audit
from services.atomic_claim import LOCK, release

router = APIRouter(prefix="/api")

# Koleksi induk yang endpoint-nya memakai `atomic_claim.claim()` — sumber: INV-ATOMIC-01.
LOCKED_COLLECTIONS = ["wms_tasks", "sales_orders", "warehouse_transfers", "cycle_count_sessions",
                      "purchase_returns", "sales_returns", "putaway_orders"]


@router.get("/saga-locks")
async def list_saga_locks(request: Request) -> List[Dict[str, Any]]:
    await require_role(request, ["admin"])
    out: List[Dict[str, Any]] = []
    for coll in LOCKED_COLLECTIONS:
        async for d in db[coll].find({LOCK: {"$exists": True}}, {"_id": 0, "id": 1, "status": 1, LOCK: 1}):
            out.append({"collection": coll, **d})
    return out


@router.post("/saga-locks/{collection}/{doc_id}/release")
async def release_saga_lock(collection: str, doc_id: str, request: Request) -> Dict[str, Any]:
    actor = await require_role(request, ["admin"])
    if collection not in LOCKED_COLLECTIONS:
        raise HTTPException(status_code=404, detail="Koleksi tidak dikenal.")
    doc = await db[collection].find_one({"id": doc_id, LOCK: {"$exists": True}}, {"_id": 0, LOCK: 1})
    if not doc:
        raise HTTPException(status_code=404, detail="Tidak ada kunci saga pada dokumen ini.")
    await release(collection, doc_id)
    await audit(actor["name"], "saga_lock_released", collection, doc_id, {"lock": doc[LOCK]})
    return {"released": True, "collection": collection, "id": doc_id, "lock": doc[LOCK]}
