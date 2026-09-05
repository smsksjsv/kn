"""Klaim atomik dokumen induk untuk endpoint tulis multi-koleksi (T-01 Opsi B — saga).

Keputusan 2026-09 (§7 LAPORAN_PERBAIKAN_2026-09): TANPA replica set / transaksi Mongo.
Pola: (1) semua validasi 400 selesai → (2) `claim()` = satu `find_one_and_update`
berprasyarat status + belum ada `saga_lock` → hanya SATU pemanggil lolos, sisanya 409;
(3) tulis koleksi turunan; (4) tulisan status akhir memakai `finish_set()` yang ikut
mencabut kunci. Proses yang mati di tengah meninggalkan `saga_lock` — TERLIHAT lewat
`GET /api/saga-locks` dan bisa dilepas admin — bukan menulis dua kali diam-diam.
Penjaga: INV-ATOMIC-01 (`scripts/guardrails/verify_atomic_claim.py`).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException
from pymongo import ReturnDocument

from core_utils import now_iso
from db import db

LOCK = "saga_lock"


async def claim(collection: str, doc_id: str, action: str, *, precondition: Optional[Dict[str, Any]] = None,
                actor: str = "", detail: str = "") -> Dict[str, Any]:
    """Klaim atomik: dokumen harus memenuhi `precondition` DAN belum terkunci. Kalah → 409."""
    doc = await db[collection].find_one_and_update(
        {"id": doc_id, LOCK: {"$exists": False}, **(precondition or {})},
        {"$set": {LOCK: {"action": action, "by": actor, "started_at": now_iso()}}},
        projection={"_id": 0}, return_document=ReturnDocument.AFTER)
    if doc:
        return doc
    cur = await db[collection].find_one({"id": doc_id}, {"_id": 0, "status": 1, LOCK: 1}) or {}
    lock = cur.get(LOCK)
    if lock:
        raise HTTPException(status_code=409, detail={
            "code": "SAGA_IN_PROGRESS",
            "message": (f"Aksi '{lock.get('action')}' atas dokumen ini sedang/pernah berjalan "
                        f"(mulai {lock.get('started_at')}) dan belum selesai. Muat ulang; bila tetap "
                        "menggantung, admin dapat melepasnya lewat Kunci Saga."),
            "lock": lock})
    raise HTTPException(status_code=409, detail=detail or {
        "code": "STATE_CHANGED",
        "message": f"Dokumen sudah berubah (status '{cur.get('status')}') sebelum aksi '{action}' dijalankan. Muat ulang layar.",
        "current_status": cur.get("status")})


def finish_set(set_fields: Dict[str, Any]) -> Dict[str, Any]:
    """Update-doc untuk tulisan akhir: `$set` field status + cabut `saga_lock` sekaligus."""
    return {"$set": set_fields, "$unset": {LOCK: ""}}


async def release(collection: str, doc_id: str) -> None:
    await db[collection].update_one({"id": doc_id}, {"$unset": {LOCK: ""}})


async def mark_failed(collection: str, doc_id: str, error: str) -> None:
    """Saga gagal SESUDAH klaim: kunci dibiarkan (agar tidak ditulis dua kali) + alasan tercatat."""
    await db[collection].update_one(
        {"id": doc_id, LOCK: {"$exists": True}},
        {"$set": {f"{LOCK}.failed_at": now_iso(), f"{LOCK}.error": str(error)[:500]}})
