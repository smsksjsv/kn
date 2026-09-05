#!/usr/bin/env python3
"""T-05 (audit 2026-09) — codemod: URL preview LAMA yang di-hardcode di skrip uji → env.

Setiap literal `"https://<slug>.preview.emergentagent.com"` (opsional `/api`) diganti
`os.environ["REACT_APP_BACKEND_URL"]` (+ `"/api"`), `import os` ditambah bila belum ada.
Gagal berisik bila env kosong (KeyError) — bukan 404 dari ingress yang menyesatkan.

Pakai: python3 scripts/codemod_env_url.py [--dry] [path ...]
       (tanpa path → semua skrip korpus `scripts/run_corpus.py --list`)
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
RE_URL = re.compile(r"(['\"])https://[a-z0-9\-]+\.preview\.emergentagent\.com(/api)?/?\1")


def rewrite(src: str) -> str | None:
    if not RE_URL.search(src):
        return None

    def repl(m: re.Match) -> str:
        return 'os.environ["REACT_APP_BACKEND_URL"]' + (' + "/api"' if m.group(2) else "")

    out = RE_URL.sub(repl, src)
    if not re.search(r"^\s*import os\b|^\s*import os,|^\s*from os import", out, re.M):
        lines = out.splitlines(keepends=True)
        i = 0
        if lines and lines[0].startswith("#!"):
            i = 1
        # lewati docstring modul
        if i < len(lines) and lines[i].lstrip().startswith(('"""', "'''")):
            q = lines[i].lstrip()[:3]
            if lines[i].count(q) >= 2 and len(lines[i].strip()) > 3:
                i += 1
            else:
                i += 1
                while i < len(lines) and q not in lines[i]:
                    i += 1
                i += 1
        lines.insert(i, "import os\n")
        out = "".join(lines)
    return out


def corpus_files() -> list[pathlib.Path]:
    p = subprocess.run([sys.executable, "scripts/run_corpus.py", "--list"], cwd=ROOT, capture_output=True, text=True)
    files = []
    for line in p.stdout.splitlines():
        m = re.search(r"((?:backend|tests|forensic|scripts)/\S+\.py|\S+\.py)", line)
        if m and (ROOT / m.group(1)).exists():
            files.append(ROOT / m.group(1))
    return files


def main() -> int:
    dry = "--dry" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    files = [pathlib.Path(a).resolve() for a in args] if args else corpus_files()
    changed = 0
    for f in files:
        src = f.read_text(errors="ignore")
        new = rewrite(src)
        if new is None or new == src:
            continue
        changed += 1
        print(f"{'[dry] ' if dry else ''}ubah: {f.relative_to(ROOT)} ({len(RE_URL.findall(src))} URL)")
        if not dry:
            f.write_text(new)
    print(f"selesai: {changed} berkas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
