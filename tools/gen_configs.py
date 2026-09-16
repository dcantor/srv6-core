#!/usr/bin/env python3
"""Render nodes/<node>/vyos_config.txt (the day-0 `set` lines pushed by `lab.sh bootstrap` / `configure`) from lab.conf,
via `lab.sh inventory` and tools/render.py. Idempotent; run it after changing lab.conf."""
import json, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); from render import render_all   # noqa: E402

LAB_DIR = Path(__file__).resolve().parents[1]
inv = json.loads(subprocess.run([str(LAB_DIR / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
changed = 0
for name, text in render_all(inv).items():
    path = LAB_DIR / "nodes" / name / "vyos_config.txt"; path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text: path.write_text(text); changed += 1; print(f"wrote {path.relative_to(LAB_DIR)} ({text.count(chr(10))} lines)")
print(f"{changed} file(s) changed")
