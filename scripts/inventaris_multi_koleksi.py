#!/usr/bin/env python3
"""Inventaris endpoint tulis multi-koleksi (T-01 Langkah 2, audit 2026-09).

Untuk tiap endpoint tulis di backend/routers: kumpulkan koleksi yang ditulis LANGSUNG
(`await db.X.<op>`) DAN satu tingkat ke bawah — fungsi service yang dipanggil di badan
endpoint (dicari lewat AST: `await mod.fn(...)` / `await fn(...)` yang terdefinisi di
backend/services/*.py). Hasil: tabel Markdown. Ini INVENTARIS, bukan vonis.

Pakai: python3 scripts/inventaris_multi_koleksi.py [--md memory/INVENTARIS_MULTI_KOLEKSI_2026-09.md]
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BE = ROOT / "backend"
WRITE_OPS = {"insert_one", "insert_many", "update_one", "update_many", "delete_one", "delete_many",
             "find_one_and_update", "replace_one", "find_one_and_delete", "bulk_write"}
LOG_ONLY = {"audit_logs", "notifications", "activity_logs", "wa_messages", "doc_refs"}


def writes_in(node: ast.AST) -> set[str]:
    cols = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in WRITE_OPS:
            v = n.func.value
            if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id in ("db", "database"):
                cols.add(v.attr)
    return cols


def service_index() -> dict[str, tuple[str, set[str]]]:
    """{fn_name: (module, writes)} untuk semua fungsi tingkat modul di services/."""
    idx: dict[str, tuple[str, set[str]]] = {}
    for f in (BE / "services").glob("*.py"):
        try:
            tree = ast.parse(f.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                idx[f"{f.stem}.{n.name}"] = (f.stem, writes_in(n))
                idx.setdefault(n.name, (f.stem, writes_in(n)))
    return idx


def calls_in(node: ast.AST) -> list[str]:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name):
                out.append(f"{n.func.value.id}.{n.func.attr}")
            elif isinstance(n.func, ast.Name):
                out.append(n.func.id)
    return out


def collect_rows() -> list:
    """[(router file, lineno, 'METHOD /path', direct, via, allc)] untuk endpoint tulis ≥2 koleksi."""
    idx = service_index()
    rows = []
    for f in sorted((BE / "routers").glob("*.py")):
        tree = ast.parse(f.read_text(errors="ignore"))
        for n in tree.body:
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            path = None
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("post", "put", "patch", "delete") and d.args:
                    path = f"{d.func.attr.upper()} {getattr(d.args[0], 'value', '?')}"
            if not path:
                continue
            direct = writes_in(n)
            via: dict[str, set[str]] = {}
            for c in calls_in(n):
                key = c if c in idx else c.split(".")[-1]
                if key in idx and idx[key][1]:
                    mod, w = idx[key]
                    via[f"{mod}.{key.split('.')[-1]}"] = w
            allc = set(direct)
            for w in via.values():
                allc |= w
            if len(allc) >= 2:
                rows.append((f.name, n.lineno, path, sorted(direct), via, sorted(allc)))
    rows.sort(key=lambda r: -len(r[5]))
    return rows


def _reviewed():
    sys.path.insert(0, str(ROOT / "scripts" / "guardrails"))
    from verify_atomic_claim import REVIEWED  # noqa: E402
    return REVIEWED


def main() -> int:
    rows = collect_rows()
    reviewed = _reviewed()
    L = ["# Inventaris endpoint tulis ≥2 koleksi (T-01 Langkah 2) — dihasilkan `scripts/inventaris_multi_koleksi.py`", "",
         f"Total: **{len(rows)}** endpoint (router langsung + satu tingkat service). Penelusuran hanya SATU tingkat: "
         "service yang memanggil service lain TIDAK ikut dihitung — angka ini masih undercount.", "",
         "| # | Berkas:baris | Endpoint | Koleksi (semua) | Langsung di router | Lewat service (1 tingkat) | Klasifikasi |",
         "|---|---|---|---|---|---|---|"]
    for i, (fn, ln, path, direct, via, allc) in enumerate(rows, 1):
        substantive = [c for c in allc if c not in LOG_ONLY]
        rv = next((v for (rf, frag), v in reviewed.items() if rf == fn and frag in path), None)
        if len(substantive) < 2:
            kls = "TIDAK RELEVAN (koleksi lain hanya log/audit/notifikasi)"
        elif rv:
            kls = f"AMAN [{rv[0]}] — {rv[1]} (INV-ATOMIC-01)"
        else:
            kls = "BELUM DITINJAU — perlu pemeriksaan penjaga idempotensi satu per satu"
        via_s = "; ".join(f"`{k}`→{sorted(v)}" for k, v in via.items()) or "—"
        L.append(f"| {i} | `{fn}:{ln}` | `{path}` | {len(allc)}: {', '.join(allc)} | {', '.join(direct) or '—'} | {via_s} | {kls} |")
    text = "\n".join(L) + "\n"
    if "--md" in sys.argv:
        out = ROOT / sys.argv[sys.argv.index("--md") + 1]
        out.write_text(text)
        print(f"ditulis: {out} ({len(rows)} baris)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
