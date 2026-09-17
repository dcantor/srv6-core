#!/usr/bin/env python3
"""Tenant provisioning portal for the SRv6 core lab.

The UI (static/index.html) shows the tenants with live state and the topology, and drives pipelines:
    add tenant / add site  ->  lab.conf + day-0 configs -> host VMs -> CE VMs re-wired -> configure (SSH) -> Nautobot seed
                               -> verify (ping matrix, Nautobot == lab.conf) -> Robot tests
    remove tenant          ->  hosts off, Nautobot clean-up, lab.conf, configure (interfaces/VRFs deleted), seed, verify, tests
    steering               ->  explicit-path SRv6 policies (tools/steer.py), applied immediately
Runs execute one at a time in a background thread; state is mirrored to runs/<id>.json. Start with ./lab.sh webapp
(uvicorn on 0.0.0.0:8091) or the systemd user unit srv6-webapp."""
import json, os, subprocess, sys, time
from pathlib import Path
from labportal import RunBase, RunRegistry, install_runs_api
from fastapi import FastAPI, HTTPException, Query, Path as PathParam
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import tenants as T
from state import State

LAB = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(LAB / "tools")); from topology_svg import draw   # noqa: E402
RUNS_DIR = Path(__file__).resolve().parent / "runs"; RUNS_DIR.mkdir(exist_ok=True); RESULTS = LAB / "results"
PY = str(LAB / "tests" / ".venv" / "bin" / "python")
STEP_TITLES = {"validate": "Validate the allocation", "labconf": "Register in lab.conf, render the day-0 configs", "hosts": "Create and boot the host VMs",
               "ces": "Re-wire the CE VMs (new attachment circuit and LAN ports)", "configure": "Push the configuration to the PEs and CEs (SSH)",
               "nautobot": "Nautobot source of truth (seed)", "verify": "Verify: tenant ping matrix, Nautobot rendering == lab.conf", "test": "Robot Framework tests",
               "rm_validate": "Validate the removal", "rm_hosts": "Power off and delete the host VMs", "rm_nautobot": "Remove the tenant from Nautobot",
               "rm_labconf": "Remove from lab.conf, render the day-0 configs", "rm_configure": "Delete the VRF, interfaces and BGP on the PEs and CEs", "rm_ces": "Re-wire the CE VMs",
               "backup": "Commit the configurations to Gitea"}
TAGS = [{"name": "state", "description": "Tenants, sites, hosts and live state (eBGP per tenant, VRF routes, SIDs, host reachability), topology."},
        {"name": "provisioning", "description": "Suggest / validate a new tenant or a new site; plan a removal."},
        {"name": "steering", "description": "Explicit-path SRv6 steering policies (applied immediately)."},
        {"name": "runs", "description": "Pipeline runs: add tenant, add site, remove tenant, tests."}]
app = FastAPI(title="SRv6 Tenant Provisioning Portal API", version="1.0", openapi_tags=TAGS, docs_url="/docs", redoc_url="/redoc",
              description="REST API behind the SRv6 core lab's tenant portal. Every change goes **lab.conf → day-0 configs → VMs → SSH push → Nautobot seed → verification → Robot tests**; "
                          "runs are asynchronous (`POST /api/runs`, poll `GET /api/runs/{id}`). UI: [/](/)")
registry = RunRegistry(RUNS_DIR); state = State()


class SiteSpec(BaseModel):
    dc: str; pe: str; ce: str; pe_port: str; ce_pe_port: str; ce_lan_port: str; attachment_circuit: str; lan: str
    host: str; host_mgmt: str; host_console: int; host_idx: int; host_ip: str | None = None; gateway: str | None = None


class TenantSpec(BaseModel):
    name: str = Field(examples=["tenant-c"]); table: int = Field(examples=[300]); rt: str = Field(examples=["65000:300"]); description: str = ""
    sites: list[SiteSpec]


class SteerSpec(BaseModel):
    pe: str = Field(examples=["pe1"]); tenant: str = Field(examples=["tenant-b"]); prefix: str = Field(examples=["172.21.3.0/24"]); via: list[str] = Field(examples=[["p1", "p3"]])


class RunRequest(BaseModel):
    mode: str = Field(examples=["tenant"], description="tenant | site | remove | test")
    tenant: TenantSpec | None = None
    name: str | None = Field(None, description="remove: the tenant to remove")
    options: dict = Field(default_factory=dict, description="{test: bool (default true), suites: [..]}")


class Run(RunBase):
    STEP_TITLES = STEP_TITLES
    EXTRA = {"tenant": "tenant", "spec": "spec", "removal": "removal"}

    def __init__(self, mode, spec, options, resume_of=None):
        self.spec, self.removal = spec, (resume_of or {}).get("removal")
        self.tenant = (spec or {}).get("name")
        super().__init__(mode, options, resume_of, runs_dir=RUNS_DIR, cwd=LAB)

    def plan(self):
        if self.mode == "test": return ["test"]
        if self.mode == "remove":
            steps = ["rm_validate", "rm_hosts", "rm_nautobot", "rm_labconf", "rm_configure", "rm_ces", "nautobot", "verify"]
        else:
            steps = ["validate", "labconf", "hosts", "ces", "configure", "nautobot", "verify"]
        if self.options.get("test", True): steps.append("test")
        steps.append("backup"); return steps

    def after(self): state._cache = None

    # ---- add tenant / add site ------------------------------------------------------------------------------
    def do_validate(self, s):
        problems = T.validate(self.spec, new_tenant=self.mode == "tenant")
        if problems: raise RuntimeError("; ".join(problems))
        s["summary"] = f"{self.spec['name']}: {len(self.spec['sites'])} site(s) — " + ", ".join(f"{x['dc']} ({x['host']}, {x['lan']})" for x in self.spec["sites"])

    def do_labconf(self, s):
        ces, hosts = T.apply_to_labconf(self.spec, new_tenant=self.mode == "tenant"); self.spec["_ces"], self.spec["_hosts"] = ces, hosts
        self.say(f"lab.conf: tenant {self.spec['name']} (table {self.spec['table']}, RT {self.spec['rt']}), hosts {hosts}, links on {ces}")
        self.sh([PY, LAB / "tools" / "gen_configs.py"]); s["summary"] = f"hosts {', '.join(hosts)}; CEs {', '.join(ces)}"

    def do_hosts(self, s):
        hosts = self.spec.get("_hosts") or [x["host"] for x in self.spec["sites"]]
        self.sh([LAB / "lab.sh", "up", *hosts]); self.sh([LAB / "lab.sh", "wait", *hosts]); s["summary"] = ", ".join(hosts)

    def do_ces(self, s):
        ces = self.spec.get("_ces") or sorted({x["ce"] for x in self.spec["sites"]})
        self.say("the CEs get new NIC wiring (the PE side anchors the UDP links and is unchanged): shut down, redefine, boot")
        self.sh([LAB / "lab.sh", "down", *ces]); self.sh([LAB / "lab.sh", "rebuild", *ces]); self.sh([LAB / "lab.sh", "up", *ces]); self.sh([LAB / "lab.sh", "wait", *ces])
        s["summary"] = ", ".join(ces) + " re-wired and back"

    def do_configure(self, s):
        nodes = sorted({x["pe"] for x in self.spec["sites"]} | {x["ce"] for x in self.spec["sites"]})
        self.sh([LAB / "lab.sh", "configure", *nodes]); s["summary"] = ", ".join(nodes)

    def do_nautobot(self, s):
        self.sh([LAB / "lab.sh", "nautobot", "seed"]); s["summary"] = "seeded"

    def do_verify(self, s):
        hosts = [x["host"] for x in (self.spec or {}).get("sites", [])] if self.mode != "remove" else []
        time.sleep(20)   # eBGP + VPNv4 convergence after the pushes
        if hosts:
            if len(hosts) > 1: self.sh([PY, LAB / "tools" / "host_cmd.py", "matrix", *hosts])
            else: self.sh([PY, LAB / "tools" / "host_cmd.py", "matrix", *hosts, *[h["host"] for h in T.tenant_sites(T.facts(), self.spec["name"]) if h["host"] != hosts[0]][:1]], check=False)
        self.sh([LAB / "lab.sh", "nautobot", "render", "--check"]); s["summary"] = "tenant reachable, Nautobot == lab.conf"

    def do_test(self, s):
        suites = self.options.get("suites") or ["04_vpn", "05_end_to_end", "09_nautobot"]
        args = [f"suites/{x}.robot" for x in suites] if suites != ["all"] else []
        self.record_tests(RESULTS, s, self.sh([LAB / "tests" / "run.sh", *args], check=False))

    def do_backup(self, s):
        self.sh([LAB / "lab.sh", "backup", "-m", f"portal run {self.id}: {self.mode} {(self.spec or {}).get('name', '')}".strip()], check=False); s["summary"] = "running + intended configs, routing tables"

    # ---- remove tenant ----------------------------------------------------------------------------------------
    def do_rm_validate(self, s):
        problems, plan = T.removal_plan(self.spec["name"])
        if problems: raise RuntimeError("; ".join(problems))
        self.removal = plan; s["summary"] = f"{plan['name']}: hosts {', '.join(plan['hosts'])}, sites {', '.join(x['dc'] for x in plan['sites'])}"

    def do_rm_hosts(self, s):
        self.sh([LAB / "lab.sh", "down", *self.removal["hosts"]], check=False); self.sh([LAB / "lab.sh", "clean", *self.removal["hosts"]])
        for h in self.removal["hosts"]: subprocess.run(["rm", "-rf", str(LAB / "nodes" / h)])
        s["summary"] = ", ".join(self.removal["hosts"])

    def do_rm_nautobot(self, s):
        self.sh([LAB / "lab.sh", "nautobot", "remove-tenant", self.removal["name"]]); s["summary"] = "tenant, VRF, prefixes, peerings, hosts removed"

    def do_rm_labconf(self, s):
        T.remove_from_labconf(self.removal["name"]); self.sh([PY, LAB / "tools" / "gen_configs.py"]); s["summary"] = "lab.conf + day-0 configs"

    def do_rm_configure(self, s):
        """The rendered configs no longer contain the tenant; delete its VRF, interfaces and BGP explicitly (the push is additive)."""
        from netmiko import ConnectHandler
        t = self.removal["name"]; N = {n["name"]: n for n in T.inventory()["nodes"]}
        for site in self.removal["sites"]:
            for node, ports in ((site["pe"], [site["pe_port"]]), (site["ce"], [site["ce_pe_port"], site["ce_lan_port"]])):
                cmds = [f"delete vrf name {t}"] + [f"delete interfaces ethernet {p}" for p in ports]
                self.say(f"{node}: " + "; ".join(cmds))
                c = ConnectHandler(device_type="vyos", host=N[node]["mgmt_ip"], username="vyos", password="vyos")
                out = c.send_config_set(cmds + ["commit", "save"], exit_config_mode=True, cmd_verify=False, read_timeout=180); c.disconnect()
                if "failed" in out and "Nothing to delete" not in out: raise RuntimeError(f"{node}: {out[-400:]}")
        self.sh([LAB / "lab.sh", "configure", *sorted({x["pe"] for x in self.removal["sites"]} | {x["ce"] for x in self.removal["sites"]})])
        s["summary"] = "VRF and ports deleted, remaining config re-applied"

    def do_rm_ces(self, s):
        ces = self.removal["ces"]; self.sh([LAB / "lab.sh", "down", *ces]); self.sh([LAB / "lab.sh", "rebuild", *ces]); self.sh([LAB / "lab.sh", "up", *ces]); self.sh([LAB / "lab.sh", "wait", *ces])
        s["summary"] = ", ".join(ces)


# ---- API ----------------------------------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index(): return FileResponse(str(Path(__file__).resolve().parent / "static" / "index.html"))


@app.get("/api/state", tags=["state"], summary="Tenants, sites, hosts with live state")
def get_state(refresh: bool = Query(False), live: bool = Query(True)):
    st = state.get(refresh=refresh, live=live); return {k: v for k, v in st.items() if k != "inv"} | {"nodes": st["inv"]["nodes"], "service": st["inv"]["service"]}


@app.get("/api/topology.svg", tags=["state"], summary="The topology as SVG (live host colouring)", response_class=Response)
def topology_svg(live: bool = Query(True)):
    st = state.get(live=live); svg, _ = draw(st["inv"], live=st.get("hosts_live") if live else None)
    return Response(svg, media_type="image/svg+xml")


@app.get("/api/tenants/suggest", tags=["provisioning"], summary="Suggest a fully allocated new tenant")
def tenant_suggest(dcs: str = Query("", description="comma-separated sites (default: every DC)")):
    return T.suggest([d for d in dcs.split(",") if d] or None)


@app.get("/api/tenants/{name}/suggest-site", tags=["provisioning"], summary="Suggest a new site for an existing tenant")
def site_suggest(name: str, dc: str = Query(...)):
    r = T.suggest_site(name, dc)
    if "error" in r: raise HTTPException(422, r["error"])
    return r


@app.post("/api/tenants/validate", tags=["provisioning"], summary="Validate a tenant / site spec")
def tenant_validate(spec: TenantSpec, new_tenant: bool = Query(True)):
    return {"problems": T.validate(spec.model_dump(), new_tenant=new_tenant)}


@app.get("/api/tenants/{name}/removal", tags=["provisioning"], summary="Plan a tenant's removal")
def tenant_removal(name: str):
    problems, plan = T.removal_plan(name)
    if problems: raise HTTPException(422, {"problems": problems})
    return plan


@app.get("/api/iperf", tags=["state"], summary="Throughput between two tenant hosts (iperf3, blocks for ~10 s)")
def iperf(src: str = Query(..., examples=["dc1-h1"]), dst: str = Query(..., examples=["dc3-h1"]), seconds: int = Query(5, ge=2, le=30), udp: bool = Query(False), rate: str = Query("50M")):
    r = subprocess.run([PY, str(LAB / "tools" / "iperf.py"), src, dst, "-t", str(seconds), "--json"] + (["-u", "-b", rate] if udp else []), capture_output=True, text=True, timeout=120)
    if r.returncode != 0: raise HTTPException(422, (r.stderr or r.stdout).strip()[-400:])
    return json.loads(r.stdout)


@app.get("/api/steering", tags=["steering"], summary="Steering policies present on the PEs")
def steering_list(): return state.steering()


@app.post("/api/steering", tags=["steering"], summary="Add an explicit-path policy (applied now)")
def steering_add(spec: SteerSpec):
    r = subprocess.run([PY, str(LAB / "tools" / "steer.py"), "add", spec.pe, spec.tenant, spec.prefix, *spec.via], capture_output=True, text=True, timeout=300)
    if r.returncode != 0: raise HTTPException(422, (r.stderr or r.stdout).strip()[-500:])
    state._cache = None; return {"output": r.stdout}


@app.delete("/api/steering", tags=["steering"], summary="Remove a policy (applied now)")
def steering_del(pe: str, tenant: str, prefix: str):
    r = subprocess.run([PY, str(LAB / "tools" / "steer.py"), "del", pe, tenant, prefix], capture_output=True, text=True, timeout=300)
    if r.returncode != 0: raise HTTPException(422, (r.stderr or r.stdout).strip()[-500:])
    state._cache = None; return {"output": r.stdout}


@app.post("/api/runs", tags=["runs"], summary="Start a pipeline run", responses={409: {"description": "a run is already in progress"}, 422: {"description": "validation problems"}})
def start_run(body: RunRequest):
    """Modes: **tenant** (a TenantSpec: new tenant with its sites), **site** (a TenantSpec naming an existing tenant with the new site(s)),
    **remove** (`name`), **test** (Robot suites; `options.suites`, default the VPN / end-to-end / Nautobot suites, `["all"]` for everything)."""
    mode = body.mode; spec = None
    if mode in ("tenant", "site"):
        if body.tenant is None: raise HTTPException(422, {"problems": ["tenant spec required"]})
        spec = body.tenant.model_dump(); problems = T.validate(spec, new_tenant=mode == "tenant")
        if problems: raise HTTPException(422, {"problems": problems})
    elif mode == "remove":
        problems, _ = T.removal_plan(body.name or "")
        if problems: raise HTTPException(422, {"problems": problems})
        spec = {"name": body.name}
    elif mode != "test": raise HTTPException(400, "mode must be tenant, site, remove or test")
    try: return registry.start(Run(mode, spec, body.options))
    except RuntimeError as e: raise HTTPException(409, str(e))


install_runs_api(app, registry, resume_factory=lambda d: Run(d["mode"], d.get("spec"), d.get("options") or {}, resume_of=d))


app.mount("/results", StaticFiles(directory=str(RESULTS), html=True), name="results")
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("WEBAPP_HOST", "0.0.0.0"), port=int(os.environ.get("WEBAPP_PORT", "8091")))
