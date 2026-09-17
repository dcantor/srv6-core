"""Targeted edits of lab.conf (the operational source: VMs, links, tenants). Every edit keeps the file's structure —
new entries are appended inside the existing `declare -A NAME=( ... )` / `NAME=( ... )` blocks — so `lab.sh` and
`gen_configs.py` keep working and diffs stay readable."""
import re
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]; CONF = LAB / "lab.conf"


def read(): return CONF.read_text()
def write(text): CONF.write_text(text)


def _block(text, name):
    """(start, end) of the `NAME=(` ... `)` block (declare -A or plain array); end points at the closing paren."""
    m = re.search(rf"^(?:declare -A )?{re.escape(name)}=\(", text, re.M)
    if not m: raise ValueError(f"{name} not found in lab.conf")
    depth, i = 0, m.end() - 1
    for j in range(i, len(text)):
        if text[j] == "(": depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0: return m.start(), j
    raise ValueError(f"unterminated {name} block")


def add_assoc(text, name, key, value):
    """Add `[key]=value` to an associative array (no-op if present, error if present with another value)."""
    s, e = _block(text, name); body = text[s:e]
    m = re.search(rf"\[{re.escape(key)}\]=(\S+)", body)
    if m:
        if m[1] != str(value): raise ValueError(f"{name}[{key}] is {m[1]}, not {value}")
        return text
    sep = "\n" + " " * (len(re.match(r"^(?:declare -A )?\w+=\(", body)[0]) + 1) if "\n" in body.strip() else " "
    return text[:e].rstrip() + f"{sep}[{key}]={value} " + text[e:]


def remove_assoc(text, name, key):
    s, e = _block(text, name); body = text[s:e]
    return text[:s] + re.sub(rf"\s*\[{re.escape(key)}\]=\S+", "", body) + text[e:]


def add_list_item(text, name, item, comment=None):
    """Append a quoted item (a LINKS entry) or a bare word (HOSTS) before the block's closing paren."""
    s, e = _block(text, name); body = text[s:e]
    if item in body: return text
    if "\n" in body.strip():   # multi-line: one item per line
        return text[:e].rstrip() + ("\n  # " + comment if comment else "") + f"\n  {item}\n" + text[e:]
    return text[:e].rstrip() + f" {item}" + text[e:]


def remove_list_item(text, name, item):
    s, e = _block(text, name); body = text[s:e]
    body = re.sub(rf"\n[ \t]*{re.escape(item)}(?=\n)", "", body); body = body.replace(f" {item}", "") if item in body else body
    return text[:s] + body + text[e:]


def set_scalar(text, name, value):
    if not re.search(rf"^{re.escape(name)}=", text, re.M): raise ValueError(f"{name} not found")
    return re.sub(rf"^{re.escape(name)}=[^#\n]*", f"{name}={value} ", text, count=1, flags=re.M)


# ---- tenant-level operations -----------------------------------------------------------------------------
def add_tenant(text, name, table, rt):
    text = re.sub(r"^TENANTS=\(([^)]*)\)", lambda m: f"TENANTS=({m[1].rstrip()} {name})" if name not in m[1].split() else m[0], text, count=1, flags=re.M)
    text = add_assoc(text, "VRF_TABLE", name, table); text = add_assoc(text, "VRF_RT", name, rt)
    return text


def remove_tenant(text, name):
    text = re.sub(r"^TENANTS=\(([^)]*)\)", lambda m: "TENANTS=(" + " ".join(x for x in m[1].split() if x != name) + ")", text, count=1, flags=re.M)
    text = remove_assoc(text, "VRF_TABLE", name); text = remove_assoc(text, "VRF_RT", name)
    return text


def add_host(text, name, dc, mgmt_ip, console, idx):
    for arr, val in (("ROLE", "host"), ("MGMT_IP", mgmt_ip), ("DC", dc), ("CONSOLE_PORT", console), ("NODE_IDX", idx)): text = add_assoc(text, arr, name, val)
    return re.sub(r"^HOSTS=\(([^)]*)\)", lambda m: f"HOSTS=({m[1].rstrip()} {name})" if name not in m[1].split() else m[0], text, count=1, flags=re.M)


def remove_host(text, name):
    for arr in ("ROLE", "MGMT_IP", "DC", "CONSOLE_PORT", "NODE_IDX"): text = remove_assoc(text, arr, name)
    return re.sub(r"^HOSTS=\(([^)]*)\)", lambda m: "HOSTS=(" + " ".join(x for x in m[1].split() if x != name) + ")", text, count=1, flags=re.M)


def add_link(text, a, a_port, b, b_port, prefix, tenant, comment=None):
    return add_list_item(text, "LINKS", f'"{a}:{a_port} {b}:{b_port} {prefix} {tenant}"', comment)


def remove_links(text, tenant):
    s, e = _block(text, "LINKS"); body = text[s:e]
    body = re.sub(rf'\n[ \t]*"[^"\n]* {re.escape(tenant)}"(?=\n)', "", body)
    body = re.sub(rf"\n[ \t]*# [^\n]*{re.escape(tenant)}[^\n]*(?=\n)", "", body)
    return text[:s] + body + text[e:]
