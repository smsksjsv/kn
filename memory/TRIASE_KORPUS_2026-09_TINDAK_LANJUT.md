# TRIASE KORPUS — TINDAK LANJUT T-05 (2026-09-05, sesi lanjutan)

Keputusan pemilik: **"Hapus yang sudah basi, ubah sisanya."**

## 1. Ubah: 55 skrip ber-URL hardcoded → env (`scripts/codemod_env_url.py`)
- Codemod mekanis: literal `https://<slug>.preview.emergentagent.com[/api]` → `os.environ["REACT_APP_BACKEND_URL"]`
  (+ `"/api"`), `import os` disisipkan bila belum ada. Gagal berisik (KeyError) bila env kosong — bukan 404 ingress.
- Dijalankan atas seluruh korpus `run_corpus.py --list` (63 berkas berubah: 51 direct + 12 pytest/poc) + 4 berkas
  di luar korpus (`backend_test_fase_d.py`, `backend_test_fase_l.py`, `scripts/debug_blank.py`, `scripts/crawl_blank.py`).
- Bukti: `coverage_data/corpus_converted_2026-09-05.json` (51 skrip direct dijalankan ulang sesudah codemod, DB seed bersih).

| Hasil sesudah codemod | Jumlah | Catatan |
|---|---|---|
| **LULUS** (rc=0) | **21** | sinyal uji hidup kembali — sebelumnya 0/51 (semua 404) |
| Lulus-sebagian ≥70% | 19 | asersi lama vs aturan yang sudah berevolusi (matriks izin, gerbang "verifikasi kelengkapan", wajib jenis kain/GSM, auth kuki) — DIPERTAHANKAN, dicatat di bawah |
| **BASI → DIHAPUS** | 9 | rasio ≤50% / premis tidak lagi ada (lihat §2) |
| Di luar korpus | 2 | `backend_test_fase_d.py` 18/20 · `backend_test_fase_l.py` 82% — dipertahankan |

### Lulus-sebagian yang DIPERTAHANKAN (asersi basi, bukan bug) — galat pertama
| Skrip | Lulus | Sebab (dibaca) |
|---|---|---|
| `backend/backend_test.py` | 33/35 | sales kini boleh GET /makloons (matriks izin berubah) |
| `backend_test_crm_enforcements.py` | 15/16 | urutan gerbang: MIXED_LOT dicek sebelum CREDIT_BLOCKED |
| `backend_test_depth1.py` | 29/35 | manager tak lagi punya `purchase_order.create` (SoD) |
| `backend_test_depth3_enhancements.py` | 12/14 | validasi 400 baru |
| `backend_test_dyelot.py` | 14/15 | bentuk roll QC berubah |
| `backend_test_epic7b_bank.py` | ~8/15 | `bank-accounts` kini 403 untuk peran uji (matriks izin) |
| `backend_test_f0a_entity_context.py` | 40/42 | sales hanya ent_ksc — header X-Entity-Id lain DIABAIKAN by design |
| `backend_test_f6_approval_retirement.py` | 46/47 | finance kini boleh lihat backlog approval |
| `backend_test_fase4.py` | 19/21 | 409 gerbang baru |
| `backend_test_fase_f_final.py` | ~18/24 | kata 'Scan' di onboarding gudang (audit_i18n pra-eksisting) |
| `backend_test_fase_f_write_flows.py` | ~12/13 | issue material: stok tidak turun — **PERLU DIBACA** (mungkin bug) |
| `backend_test_g6.py` | ~18/21 | "tanpa auth → 200": uji hanya mencabut header, kuki sesi masih ada — auth kini kuki |
| `backend_test_h1.py` | 27/28 | 403 matriks izin |
| `backend_test_iteration_183.py` | ~11/16 | baseline hitungan sample_issue berubah (data demo) |
| `backend_test_m0_color.py` | 19/20 | wajib jenis kain + GSM untuk stage Grey (FASE T) |
| `backend_test_phase2_forms.py` | ~34/38 | prefiks nomor log kendaraan berubah |
| `backend_test_po_timeline_approval.py` | 10/14 | endpoint 404 — **PERLU DIBACA** |
| `backend_test_qc.py` / `_qc_4point.py` | 19/21 · 10/12 | bentuk respons QC berubah |
| `backend_test_r1_05_06.py` / `_r6_3_budget.py` | 4/5 · ~25/29 | validasi 400 baru |

## 2. Hapus (BASI): 11 berkas
| Berkas | Alasan |
|---|---|
| `backend/backend_test_18.py` | 1/2 — premis konfirmasi SO tanpa verifikasi kelengkapan sudah tidak ada |
| `backend/backend_test_f3_mto_rma.py` | 6/13 — admin tak lagi `order.approve` (SoD); alur MTO/RMA sudah diuji `test_iter*` |
| `backend/backend_test_g6_iter192.py` | 1/2 — mencari transaksi G-6 tanpa tugas gudang (tidak ada lagi di seed) |
| `backend/backend_test_e0_isolation.py` | 3/3 gagal — isolasi entitas kini dijaga `audit_entity_isolation` + `test_f0*` |
| `backend/backend_test_finance_analytics.py` | 0/8 — kontrak respons analitik berubah total |
| `backend/backend_test_sales_returns.py` | terhenti di setup (admin tak punya `order.confirm`) |
| `tests/backend_test_catch_weight.py` | 1/2 — skema produk berubah (gramasi/lebar wajib) |
| `tests/backend_test_f2_f3_sales_team_delivery.py` | 14/30 — kontrak tim sales/pengiriman lama |
| `tests/agent_e7_fe_iteration_219.py` | traceback saat start — skrip agen FE sekali pakai |
| `backend/tests/test_price_approval_supersede_notification.py` | UJI BASI (laporan §7 no.3): admin sebagai pengaju+penyetuju, dilarang SoD |
| `backend/tests/test_price_approvals_supersede.py` | UJI BASI (idem) — siklus supersede masih diuji `test_iter273_approval_rules.py` |

Korpus kini **210** skrip (dari 220). `scripts/triase_korpus.py` aturan "URL preview basi" kini tidak akan pernah terpicu lagi
(tidak ada literal preview di korpus; `verify_codebase_map`/`run_corpus` mengisi env dari `frontend/.env`).
