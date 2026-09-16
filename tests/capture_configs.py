#!/usr/bin/env python3
"""Download the configuration (`show configuration commands`) of every VyOS node into a directory."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "resources"))
from LabLib import LabLib          # noqa: E402
from lab_vars import VYOS, MGMT    # noqa: E402

out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
lib = LabLib(); stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
try:
    for name in VYOS:
        try:
            cfg = lib.run_vyos_command(MGMT[name], "show configuration commands", timeout=120)
        except Exception as e:      # a node that is down must not stop the capture
            print(f"[{name}] {e.__class__.__name__}: {e}"); continue
        path = out / f"{name}.config.txt"
        path.write_text(f"# {name} ({MGMT[name]}) show configuration commands captured {stamp}\n{cfg}\n")
        print(f"[{name}] {path} ({len(cfg)} bytes)")
finally:
    lib.close_all_connections()
