# LAPORAN SESI LANJUTAN 2026-09-05 — 4 Next Action Items audit 2026-09

Repo `pandeyoga/kn123` @65a3c9a di-clone ke `/app`, lingkungan dipulihkan (`.restore_env.sh`).
**Temuan lingkungan:** backend template datang dengan `CORS_ORIGINS="*"` → T-02 (gagal berisik) menolak start.
Diperbaiki di `backend/.env` (asal preview + localhost, `SESSION_COOKIE_SECURE=true`). `.env` di-.gitignore — kontainer baru
WAJIB mengisinya (lihat komentar T-02 di `server.py`).

## 1. T-01 Atomisitas — KEPUTUSAN: **Opsi B (saga / klaim atomik), tanpa replica set**
Alasan: replica set mengubah `MONGO_URL` di semua lingkungan (preview/CI/prod) dan 82 endpoint tetap harus di-refactor
satu per satu untuk memakai sesi transaksi — biaya infra + refactor; saga memberi jaminan yang dibutuhkan (satu pemenang,
kegagalan di tengah TERLIHAT) tanpa perubahan infra, dan bisa ditegakkan statik.

- `backend/services/atomic_claim.py`: `claim(coll, id, action, precondition)` = `find_one_and_update` berprasyarat status +
  `saga_lock` belum ada → pemenang tunggal, kalah 409 (`SAGA_IN_PROGRESS` bila terkunci, `STATE_CHANGED` bila status berubah);
  `finish_set()` = `$set` akhir + `$unset saga_lock`; `release()`; `mark_failed()`.
- `backend/routers/saga_locks.py`: `GET /api/saga-locks` (admin) daftar kunci menggantung; `POST /api/saga-locks/{coll}/{id}/release`.
- Diterapkan (klaim): inbound complete · SO cancel · SO release-reservation · SO approval decide · transfers approve/reject/status ·
  cycle-count approve. CAS berprasyarat status: PO close · PO cancel · **`so_transition`** (semua transisi SO kini CAS `status $in expected_from`
  + `$unset saga_lock`). Pra-eksisting diakui: vendor-bills pay, resolve-escalation.
- Guard baru **INV-ATOMIC-01** `scripts/guardrails/verify_atomic_claim.py` (+ self-test 10 kasus) dipasang di `gate.sh`:
  `REVIEWED` (mekanisme+alasan per endpoint, diverifikasi terhadap sumber fungsi), ratchet `BASELINE_UNREVIEWED=67` (hanya turun).
  Inventaris `memory/INVENTARIS_MULTI_KOLEKSI_2026-09.md` kini membaca `REVIEWED` (16 AMAN · 67 BELUM DITINJAU · 4 tidak relevan).
- Bukti runtime: 2× cancel SO bersamaan → 200 + 409, `saga_lock` tidak tersisa; 2× cancel PO bersamaan → 200 + 400/409;
  kunci disuntik manual → cancel 409 `SAGA_IN_PROGRESS`, tampil di `/api/saga-locks`, release 200.

## 2. T-05 Korpus uji — `memory/TRIASE_KORPUS_2026-09_TINDAK_LANJUT.md`
- `scripts/codemod_env_url.py` mengubah 63+4 berkas: literal URL preview → `os.environ["REACT_APP_BACKEND_URL"]`.
- 51 skrip direct dijalankan ulang: **21 LULUS penuh** (sebelumnya 0 — semua 404), 19 lulus-sebagian ≥70% (asersi lama vs aturan
  yang berevolusi; dipertahankan, alasan per skrip di dokumen), **11 dihapus** (9 rasio ≤50%/premis hilang + 2 UJI BASI SoD).
  Korpus 220 → 210. Bukti: `coverage_data/corpus_converted_2026-09-05.json`.

## 3. Seed demo — `seed_realistic.py`
- `clear_collections()` kini mengosongkan SEMUA koleksi selain `KEEP_MASTER` (design_gallery, amendment_reasons,
  expense_categories, uom_conversion_rules, bank_statement_formats, rnd_person_divisions) — 42 koleksi sisa tidak lagi bocor.
- `_replant_bootstrap()` di akhir `main()` menjalankan `bootstrap.run_bootstrap()` (idempoten): akun md@/wh.admin@, COA, hr_*,
  config ditanam ulang **tanpa restart backend**. Bukti: sesudah seed, login md@/wh.admin@/admin → 200; fondasi 8/75/18.

## 4. Lencana eskalasi menggantung
- `POST /api/outbound/tasks/{id}/reopen-escalation` (wms.approve): `resolving` → `pending_review` (+`reopened_by/at`), 409 bila tidak menggantung.
- `EscalationManagement.jsx`: badge "N Menggantung", label "MENGGANTUNG (resolving)", panel penjelasan (waktu klaim, peringatan roll),
  tombol "Buka Kembali Eskalasi (lepas klaim)" menggantikan Resolve pada kartu menggantung.

## Verifikasi
- `verify_atomic_claim.py --self-test` HIJAU; `gate.sh --quick`: 5 merah = persis pra-eksisting (modal_dismiss, escape_layers ×2,
  ux_audit, audit_i18n_id) — nol regresi; 2 gate baru hijau.
- Testing agent iteration_314: semua PASS (backend saga/CAS/stuck-lock/reopen + frontend badge & reopen).

## Terbuka / keputusan berikutnya
- 67 endpoint multi-koleksi masih BELUM DITINJAU (ratchet menjaga agar tidak bertambah). Prioritas berikut: reverse settlement
  retur beli/jual (5 koleksi, lewat service), putaway confirm-arrival, `POST /sales-orders` (alokasi roll).
- 2 skrip lulus-sebagian PERLU DIBACA: `backend_test_fase_f_write_flows.py` (roll 800→50 saat issue material — kemungkinan residu
  urutan) dan `backend_test_po_timeline_approval.py` (matriks approval PO kini butuh admin).
- `backend/.env` wajib berisi `CORS_ORIGINS` eksplisit — pertimbangkan `.restore_env.sh` memeriksanya (gagal berisik) sebelum restart.
