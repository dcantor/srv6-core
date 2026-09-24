#!/usr/bin/env python3
"""Render nodes/<node>/vyos_config.txt (the day-0 `set` lines pushed by `lab.sh bootstrap` / `configure`) from lab.conf,
via `lab.sh inventory` and tools/render.py — plus nodes/lg/frr.conf and nodes/lg/lgd.json, the looking-glass collector's
own configuration (FRR syntax; it is not a VyOS node). Idempotent; run it after changing lab.conf."""
import json, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent)); from render import render_all, render_lg, lg_app_config, lg_nodes   # noqa: E402

LAB_DIR = Path(__file__).resolve().parents[1]
inv = json.loads(subprocess.run([str(LAB_DIR / "lab.sh"), "inventory"], capture_output=True, text=True, check=True).stdout)
files = {LAB_DIR / "nodes" / name / "vyos_config.txt": text for name, text in render_all(inv).items()}
for n in lg_nodes(inv):
    files[LAB_DIR / "nodes" / n["name"] / "frr.conf"] = render_lg(inv, n["name"])
    files[LAB_DIR / "nodes" / n["name"] / "lgd.json"] = json.dumps(lg_app_config(inv, n["name"]), indent=1, sort_keys=True) + "\n"
changed = 0
for path, text in files.items():
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text: path.write_text(text); changed += 1; print(f"wrote {path.relative_to(LAB_DIR)} ({text.count(chr(10))} lines)")
print(f"{changed} file(s) changed")
