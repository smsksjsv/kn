# 🧾 GATE RECEIPT — Kain Nusantara

> Bukti verifikasi otomatis. Dihasilkan `scripts/gate.sh`. JANGAN edit manual.

- **Waktu:** 2026-09-05 15:58:26
- **Mode:** `quick`  ·  **Durasi total:** 25s  ·  **Pekerja statik:** 2
- **Backend:** DOWN / tidak diperiksa (mode quick atau Phase 0)

| Gate | Hasil |
|------|-------|
| guard:auth_coverage (INV-AUTH-01) | PASS (0s) |
| guard:auth_coverage SELF-TEST (bukti-merah penjaga auth) | PASS (0s) |
| validate_compliance (file/naming/docs/api/env) | PASS (1s) |
| check_nav_map (navigasi vs SSOT) | PASS (0s) |
| guard:modal_dismiss (INV-UI-01, modal auto-close) | FAIL (0s) |
| guard:create_modal SELF-TEST (bukti-merah penjaga create pop-up) | PASS (0s) |
| guard:create_modal (INV-UI-05, tombol Buat = pop-up konsisten) | PASS (0s) |
| guard:blocking_dialogs SELF-TEST (bukti-merah + anti tuduh palsu) | PASS (0s) |
| guard:blocking_dialogs (INV-UI-06, alert/confirm/prompt dilarang) | FAIL (1s) |
| guard:list_export SELF-TEST (bukti-merah + CSV rusak harus memerah) | PASS (0s) |
| guard:list_export (INV-UI-07, daftar berhalaman wajib bisa diunduh) | PASS (1s) |
| guard:detail_modal SELF-TEST (bukti-merah + anti tuduh palsu) | PASS (1s) |
| guard:detail_modal (INV-UI-08, panel rincian wajib pop-up) | PASS (1s) |
| guard:picker_portal SELF-TEST (bukti-merah + anti tuduh palsu, 16 kasus) | PASS (3s) |
| guard:picker_portal (INV-UI-09, pemilih wajib ber-portal · pop-up bukan anak <label>) | PASS (5s) |
| guard:escape_layers SELF-TEST (bukti-merah + anti tuduh palsu, 13 kasus) | FAIL (0s) |
| guard:escape_layers (INV-UI-10, Esc menutup lapisan teratas saja) | FAIL (1s) |
| guard:to_list_bound SELF-TEST (bukti-merah dua arah, 11 kasus) | PASS (0s) |
| guard:to_list_bound (INV-PERF-01, to_list(n>20000) dilarang) | PASS (1s) |
| guard:codebase_map SELF-TEST (bukti-merah dua arah, 6 kasus) | PASS (1s) |
| guard:codebase_map (INV-DOC-01, CODEBASE_MAP.md selaras kode) | PASS (1s) |
| guard:atomic_claim SELF-TEST (bukti-merah dua arah, 10 kasus) | PASS (1s) |
| guard:atomic_claim (INV-ATOMIC-01, saga/CAS endpoint multi-koleksi) | PASS (2s) |
| ux_audit SELF-TEST (bukti-merah baseline UX + anti tuduh palsu) | PASS (0s) |
| ux_audit --strict (INV-UX-01, loading/empty/chart baseline) | FAIL (1s) |
| config_wiring (INV-CFG-01/04, satu sumber kebenaran) | PASS (1s) |
| config_wiring SELF-TEST (bukti-merah guardrail) | PASS (11s) |
| audit_doc_refs SELF-TEST (bukti-merah relasi dokumen) | PASS (0s) |
| guard:ref_unlink SELF-TEST (bukti-merah dua arah, 8 kasus) | PASS (1s) |
| guard:ref_unlink (INV-REF-04, hapus dokumen wajib sapu tautan balik) | PASS (3s) |
| guard:notif_audience SELF-TEST (bukti-merah dua arah, 14 kasus) | PASS (0s) |
| guard:notif_audience (INV-NOTIF-02, alamat notifikasi dari WEWENANG) | PASS (3s) |
| audit_i18n_id (label antarmuka Bahasa Indonesia) | FAIL (1s) |
| audit_i18n_id SELF-TEST (bukti-merah guardrail bahasa) | PASS (0s) |
| fix_i18n_id SELF-TEST (codemod tak boleh sentuh kode) | PASS (0s) |
| guard:entity_label (INV-UI-02, id entitas tak boleh tampil) | PASS (0s) |
| guard:error_notice (INV-UI-03, error tak boleh senyap) | PASS (0s) |
| guard:role_label (INV-ROLE-01, peran dari registry & izin) | PASS (1s) |
| guard:derived_fields (INV-UI-04, field turunan tak boleh dari respons daftar) | PASS (0s) |
| audit_entity_isolation SELF-TEST (bukti-merah pagar isolasi) | PASS (1s) |
| guard:write_scope SELF-TEST (INV-ENTITY-02, mode gabungan hanya-lihat) | PASS (0s) |
| guard:warehouse_scope SELF-TEST (E4.1, gudang khusus badan usaha) | PASS (1s) |
| guard:numeric_bounds (INV-NUM-01, statik+runtime) | PASS (0s) |
| seed_realistic | SKIP |
| verify_data_integrity | SKIP |
| gate runtime (IDOR/race/state/sweep/health) | SKIP |
| INV-GATE-01 anti-residu | SKIP |

## ❌ VERDICT: MERAH — ADA GATE GAGAL. Lihat memory/BUG_REGISTRY.md. Perbaiki lalu jalankan ulang.

**Tingkatan:** `--quick` (statik ~7s) · default (~25s) · `--ci` (default + receipt JSON) · `--full` (+POC fase ~95s).

_Catatan: SKIP bukan PASS. Gate runtime harus dijalankan ulang saat backend hidup._
