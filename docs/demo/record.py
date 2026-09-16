#!/usr/bin/env python3
"""Record a terminal demo of the lab: runs the real commands against the running lab, replays them in a terminal-styled
page (typed prompt, streamed output, captions) with Playwright, and writes docs/demo/srv6-demo.gif and .mp4.
Needs the cat8000v-ipsec webapp venv (playwright + imageio-ffmpeg + Pillow) and system Chrome:
   /home/dcantor/cat8000v-ipsec/webapp/.venv/bin/python docs/demo/record.py"""
import html, io, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright

LAB = Path(__file__).resolve().parents[2]; OUT = LAB / "docs" / "demo"; OUT.mkdir(exist_ok=True)
PY = LAB / "tests" / ".venv" / "bin" / "python"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def sh(cmd, timeout=600):
    r = subprocess.run(cmd, shell=True, cwd=LAB, capture_output=True, text=True, timeout=timeout)
    return ANSI.sub("", r.stdout + r.stderr).rstrip()


def vy(node, cmd):
    ip = {"pe1": "10.3.0.11", "p1": "10.3.0.21", "p2": "10.3.0.22", "p3": "10.3.0.23"}[node]
    return sh(f"{PY} tools/vyos_cmd.py {ip} \"{cmd}\"")


# ---- 1. run the real thing and keep the output ------------------------------------------------------------------
print("collecting output from the lab ...")
SCENES = []   # (caption, subcaption, [(prompt_cmd, output, seconds_to_hold)])
SCENES.append(("The lab", "19 VMs: 4 VyOS PEs, a P triangle (p1/p3 = route reflectors), 4 CEs with two tenant VRFs, 8 CirrOS hosts",
               [("./lab.sh status | head -21", sh("./lab.sh status | head -21"), 6)]))
SCENES.append(("Underlay: IS-IS level-2, IPv6-only", "every core link has an adjacency; the locators are advertised as SRv6 capabilities",
               [("./lab.sh ssh p2 'show isis neighbor'", vy("p2", "show isis neighbor"), 4),
                ("./lab.sh ssh p1 'show isis segment-routing srv6 node'", vy("p1", "show isis segment-routing srv6 node"), 5)]))
SCENES.append(("SRv6 SIDs on a PE", "End (node), End.X (per adjacency) and one End.DT4 per tenant VRF, installed in the Linux data plane",
               [("./lab.sh ssh pe1 'sudo ip -6 route show | grep seg6local'", sh(f"{PY} tools/vyos_cmd.py 10.3.0.11 'sudo ip -c=never -6 route show' | grep seg6local"), 6)]))
SCENES.append(("BGP VPNv4 over SRv6", "each tenant's LANs sit under their PE's RD at both reflectors; the next hop is the PE's loopback, the SID its End.DT4",
               [("./lab.sh ssh p1 'show bgp ipv4 vpn summary'", vy("p1", "show bgp ipv4 vpn summary"), 4),
                ("./lab.sh ssh p1 'show bgp ipv4 vpn'", "\n".join(l for l in vy("p1", "show bgp ipv4 vpn").splitlines() if "Route Distinguisher" in l or "*>" in l), 6),
                ("./lab.sh ssh pe1 'sudo ip route show vrf tenant-a'", sh(f"{PY} tools/vyos_cmd.py 10.3.0.11 'sudo ip -c=never route show vrf tenant-a'"), 6)]))
SCENES.append(("Two isolated tenants", "h1 hosts (tenant-a) reach each other, h2 hosts (tenant-b) reach each other — never across, not even at the same site",
               [("tools/host_cmd.py matrix", sh(f"{PY} tools/host_cmd.py matrix"), 8)]))
print("route-reflector failover ...")
before = vy("pe1", "show bgp ipv4 vpn summary")
SHUT = "echo 'set protocols bgp peer-group RR-CLIENTS shutdown' | tools/vyos_push.py 10.3.0.21 /dev/stdin"
UNSHUT = SHUT.replace("set ", "delete ")
shut_out = sh(SHUT.replace("| tools/", f"| {PY} tools/"))
time.sleep(25)
during = vy("pe1", "show bgp ipv4 vpn summary"); routes = vy("pe1", "show bgp ipv4 vpn 172.20.3.0/24"); matrix = sh(f"{PY} tools/host_cmd.py matrix")
unshut_out = sh(UNSHUT.replace("| tools/", f"| {PY} tools/"))
time.sleep(30)
after = vy("pe1", "show bgp ipv4 vpn summary")
SCENES.append(("Route-reflector redundancy", "shut every client session on p1: the PEs keep every route from p3 and the tenants never notice",
               [("./lab.sh ssh pe1 'show bgp ipv4 vpn summary'", before, 4),
                (SHUT, shut_out, 3),
                ("./lab.sh ssh pe1 'show bgp ipv4 vpn summary'", during, 5),
                ("./lab.sh ssh pe1 'show bgp ipv4 vpn 172.20.3.0/24'", routes, 5),
                ("tools/host_cmd.py matrix", matrix, 6),
                (UNSHUT, unshut_out, 3),
                ("./lab.sh ssh pe1 'show bgp ipv4 vpn summary'", after, 5)]))
def robot_summary():
    import xml.etree.ElementTree as ET
    root = ET.parse(LAB / "results" / "latest" / "output.xml").getroot(); lines = []
    for su in root.iter("suite"):
        if su.get("source", "").endswith(".robot"):
            st = su.find("status"); n = sum(1 for _ in su.iter("test")); lines.append(f"{su.get('name'):26s} {n:2d} tests  {st.get('status')}")
    tot = root.find("statistics/total/stat"); lines.append(f"\n{tot.get('pass')} passed, {tot.get('fail')} failed"); lines.append("    no configuration changes during the run")
    return "==> running Robot Framework suites\n" + "\n".join(lines)
SCENES.append(("Robot Framework", "management, underlay, SRv6, VPN, end-to-end, RR redundancy — every run diffs the configs before and after",
               [("./lab.sh test", robot_summary(), 7)]))

# ---- 2. replay in a terminal page ---------------------------------------------------------------------------------
PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
 body{margin:0;background:#0b1020;font-family:system-ui;color:#e2e8f0}
 #cap{position:absolute;left:0;right:0;top:0;padding:14px 26px;background:linear-gradient(#111a33,#0b1020);border-bottom:1px solid #1e293b}
 #cap b{font-size:22px} #cap span{display:block;color:#94a3b8;font-size:14px;margin-top:3px}
 #term{position:absolute;left:0;right:0;top:82px;bottom:0;padding:14px 26px;font:14px/1.35 ui-monospace,Menlo,Consolas,monospace;white-space:pre;overflow:hidden;color:#cbd5e1}
 .p{color:#7dd3fc} .c{color:#f8fafc;font-weight:600} .ok{color:#4ade80} .bad{color:#f87171} .cur{background:#e2e8f0;color:#0b1020}
 #foot{position:absolute;right:20px;bottom:10px;color:#475569;font-size:12px}
</style></head><body><div id="cap"><b id="t"></b><span id="s"></span></div><div id="term"></div><div id="foot">github.com/dcantor/srv6-core</div></body></html>"""


def colour(text):
    t = html.escape(text)
    t = re.sub(r"\b(ok|Up|PASS|Established|0% packet loss)\b", r'<span class="ok">\1</span>', t)
    t = re.sub(r"(100% loss|FAIL|Active|Idle \(Admin\)|Idle|Connect)\b", r'<span class="bad">\1</span>', t)
    return t


frames, durs = [], []
def snap(page, seconds):
    frames.append(Image.open(io.BytesIO(page.screenshot())).convert("P", palette=Image.ADAPTIVE, colors=128)); durs.append(int(seconds * 1000))


with sync_playwright() as pw:
    b = pw.chromium.launch(channel="chrome", headless=True); page = b.new_page(viewport={"width": 1280, "height": 800}); page.set_content(PAGE)
    page.evaluate("document.getElementById('t').textContent='SRv6 WAN core lab — VyOS, IS-IS, BGP L3VPN over SRv6'; document.getElementById('s').textContent='4 PEs · P triangle · 2 tenants · 8 hosts · Robot Framework'")
    page.evaluate("document.getElementById('term').innerHTML='<span class=p>dcantor@ubuntu:~/srv6-core$</span> <span class=cur> </span>'"); snap(page, 2.5)
    for title, sub, steps in SCENES:
        page.evaluate("([t,s]) => {document.getElementById('t').textContent=t; document.getElementById('s').textContent=s; document.getElementById('term').innerHTML=''}", [title, sub])
        buf = ""
        for cmd, out, hold in steps:
            typed = ""
            for ch in cmd:   # type the command, a few characters per frame
                typed += ch
                if len(typed) % 6 == 0 or typed == cmd:
                    page.evaluate("h => document.getElementById('term').innerHTML = h", buf + f'<span class="p">$</span> <span class="c">{html.escape(typed)}</span><span class="cur"> </span>'); snap(page, 0.05)
            lines = out.splitlines(); shown = []
            for i, l in enumerate(lines):  # stream the output
                shown.append(l)
                if i % 4 == 3 or i == len(lines) - 1:
                    visible = "\n".join(shown)[-6000:]
                    page.evaluate("h => {const t=document.getElementById('term'); t.innerHTML = h; t.scrollTop = t.scrollHeight}", buf + f'<span class="p">$</span> <span class="c">{html.escape(cmd)}</span>\n' + colour(visible)); snap(page, 0.08)
            snap(page, hold)
            buf = (buf + f'<span class="p">$</span> <span class="c">{html.escape(cmd)}</span>\n' + colour(out) + "\n\n")
            if buf.count("\n") > 44: buf = ""   # start a fresh screen when it would scroll off
    page.evaluate("document.getElementById('t').textContent='github.com/dcantor/srv6-core'; document.getElementById('s').textContent='./lab.sh up · bootstrap · verify · test  —  ≈6 minutes from cold, 13 GiB RAM'; document.getElementById('term').innerHTML=''"); snap(page, 3)
    b.close()

gif = OUT / "srv6-demo.gif"; frames[0].save(gif, save_all=True, append_images=frames[1:], duration=durs, loop=0, optimize=True)
print(f"{gif} ({gif.stat().st_size / 1e6:.1f} MB, {len(frames)} frames, {sum(durs) / 1000:.0f}s)")
import imageio_ffmpeg; ffmpeg = imageio_ffmpeg.get_ffmpeg_exe(); tmp = Path(tempfile.mkdtemp())
for i, f in enumerate(frames): f.convert("RGB").save(tmp / f"f{i:05d}.png")
(tmp / "list.txt").write_text("".join(f"file 'f{i:05d}.png'\nduration {d / 1000}\n" for i, d in enumerate(durs)) + f"file 'f{len(frames) - 1:05d}.png'\n")
mp4 = OUT / "srv6-demo.mp4"
subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"), "-vf", "fps=15,format=yuv420p,scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-movflags", "+faststart", str(mp4)], check=True)
shutil.rmtree(tmp); print(f"{mp4} ({mp4.stat().st_size / 1e6:.1f} MB)")
