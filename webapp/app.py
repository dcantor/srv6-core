#!/usr/bin/env python3
"""Tenant provisioning portal for the SRv6 core lab.

The UI (static/index.html) shows the tenants with live state and the topology, and drives pipelines:
    add tenant / add site  ->  lab.conf + day-0 configs -> host VMs -> CE VMs re-wired -> configure (SSH) -> Nautobot seed
                               -> verify (ping matrix, Nautobot == lab.conf) -> Robot tests
    remove tenant          ->  hosts off, Nautobot clean-up, lab.conf, configure (interfaces/VRFs deleted), seed, verify, tests
    steering               ->  explicit-path SRv6 policies (tools/steer.py), applied immediately; drawn on the map with the IGP
                               path they replace and the delay of each, measured (steermap.py)
    restore                ->  a backup (backup.py): tenants removed / added and steering put back as the backup had them
The portal also probes every pair of a tenant's sites every minute (sla.py), and says how much room the lab has (capacity.py).
Runs execute one at a time in a background thread; state is mirrored to runs/<id>.json. Start with ./lab.sh webapp
(uvicorn on 0.0.0.0:8091) or the systemd user unit srv6-webapp."""
import json, os, re, subprocess, sys, threading, time
from pathlib import Path
from labportal import RunBase, RunRegistry, install_runs_api, grafana
from fastapi import FastAPI, HTTPException, Query, Request, Path as PathParam
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import tenants as T
from state import State
import metrics as M
import sla as SLA
import capacity as CAP
import backup as BK
import steermap as SM
import whatif as WI
import traffic as TF
import health as HL

LAB = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(LAB / "tools")); from topology_svg import draw   # noqa: E402
RUNS_DIR = Path(__file__).resolve().parent / "runs"; RUNS_DIR.mkdir(exist_ok=True); RESULTS = LAB / "results"
PY = str(LAB / "tests" / ".venv" / "bin" / "python")
STEP_TITLES = {"validate": "Validate the allocation", "labconf": "Register in lab.conf, render the day-0 configs", "hosts": "Create and boot the host VMs",
               "ces": "Re-wire the CE VMs (new attachment circuit and LAN ports)", "configure": "Push the configuration to the PEs and CEs (SSH)",
               "nautobot": "Nautobot source of truth (seed)", "verify": "Verify: tenant ping matrix, Nautobot rendering == lab.conf", "test": "Robot Framework tests",
               "rm_validate": "Validate the removal", "rm_hosts": "Power off and delete the host VMs", "rm_nautobot": "Remove the tenant from Nautobot",
               "rm_labconf": "Remove from lab.conf, render the day-0 configs", "rm_configure": "Delete the VRF, interfaces and BGP on the PEs and CEs", "rm_ces": "Re-wire the CE VMs",
               "backup": "Commit the configurations to Gitea",
               "rs_validate": "Read the backup and plan the restore", "rs_remove": "Remove the tenants the backup does not have",
               "rs_add": "Add the tenants and sites the backup has", "rs_steering": "Put the steering policies back as the backup had them"}
TAGS = [{"name": "state", "description": "Tenants, sites, hosts and live state (eBGP per tenant, VRF routes, SIDs, host reachability), topology."},
        {"name": "provisioning", "description": "Suggest / validate a new tenant or a new site; plan a removal."},
        {"name": "steering", "description": "Explicit-path SRv6 steering policies (applied immediately)."},
        {"name": "runs", "description": "Pipeline runs: add tenant, add site, remove tenant, restore, tests."},
        {"name": "sla", "description": "Tenant SLA probes: delay and loss between every pair of a tenant's sites, every minute."},
        {"name": "capacity", "description": "How much room the lab has for more tenants and sites, and what runs out first."},
        {"name": "backup", "description": "The whole lab state in one file, and a restore that brings the lab back to it."}]
app = FastAPI(title="SRv6 Tenant Provisioning Portal API", version="1.0", openapi_tags=TAGS, docs_url="/docs", redoc_url="/redoc",
              description="REST API behind the SRv6 core lab's tenant portal. Every change goes **lab.conf → day-0 configs → VMs → SSH push → Nautobot seed → verification → Robot tests**; "
                          "runs are asynchronous (`POST /api/runs`, poll `GET /api/runs/{id}`). UI: [/](/)")
registry = RunRegistry(RUNS_DIR); state = State(); collector = M.Collector(state, interval=int(os.environ.get("METRICS_INTERVAL", "60")))


class SiteSpec(BaseModel):
    dc: str; pe: str; ce: str; pe_port: str; ce_pe_port: str; ce_lan_port: str; attachment_circuit: str; lan: str
    host: str; host_mgmt: str; host_console: int; host_idx: int; host_ip: str | None = None; gateway: str | None = None


class TenantSpec(BaseModel):
    name: str = Field(examples=["tenant-c"]); table: int = Field(examples=[300]); rt: str = Field(examples=["65000:300"]); description: str = ""
    sites: list[SiteSpec]


class SteerSpec(BaseModel):
    pe: str = Field(examples=["pe1"]); tenant: str = Field(examples=["tenant-b"]); prefix: str = Field(examples=["172.21.3.0/24"]); via: list[str] = Field(examples=[["p1", "p3"]])


class RunRequest(BaseModel):
    mode: str = Field(examples=["tenant"], description="tenant | site | remove | restore | test")
    tenant: TenantSpec | None = None
    name: str | None = Field(None, description="remove: the tenant to remove; restore: the backup file")
    options: dict = Field(default_factory=dict, description="{test: bool (default true), suites: [..]}")


class Run(RunBase):
    LAB = "srv6-core"
    STEP_TITLES = STEP_TITLES
    EXTRA = {"tenant": "tenant", "spec": "spec", "removal": "removal", "rplan": "rplan"}

    def __init__(self, mode, spec, options, resume_of=None):
        self.spec, self.removal = spec, (resume_of or {}).get("removal")
        self.rplan = (resume_of or {}).get("rplan")
        self.tenant = (spec or {}).get("name")
        super().__init__(mode, options, resume_of, runs_dir=RUNS_DIR, cwd=LAB)

    def plan(self):
        if self.mode == "test": return ["test"]
        if self.mode == "restore":
            steps = ["rs_validate", "rs_remove", "rs_add", "rs_steering", "nautobot", "verify"]
            if self.options.get("test", True): steps.append("test")
            steps.append("backup"); return steps
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
        hosts = [x["host"] for x in (self.spec or {}).get("sites", [])] if self.mode not in ("remove", "restore") else []
        time.sleep(20)   # eBGP + VPNv4 convergence after the pushes
        if self.mode == "restore":                                   # the whole lab, as the backup had it
            f = T.facts()                                            # one matrix per tenant: tenants never reach each other
            for t in f["order"]:
                hs = [x["host"] for x in T.tenant_sites(f, t) if x.get("host")]
                if len(hs) > 1: self.sh([PY, LAB / "tools" / "host_cmd.py", "matrix", *hs])
            self.sh([LAB / "lab.sh", "nautobot", "render", "--check"])
            s["summary"] = "every host reaches its tenant's others, Nautobot == lab.conf"; return
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

    # ---- restore from a backup ----------------------------------------------------------------------------------
    def do_rs_validate(self, s):
        b = BK.load(self.spec["backup"])
        self.rplan = BK.plan(b, state.steering())
        if self.rplan["problems"]: raise RuntimeError("; ".join(self.rplan["problems"]))
        p = self.rplan
        s["summary"] = (f"{self.spec['backup']}: add {', '.join(x['name'] for x in p['add']) or '—'}; sites {', '.join(x['name'] + ' ' + '/'.join(y['dc'] for y in x['sites']) for x in p['add_sites']) or '—'}; "
                        f"remove {', '.join(p['remove']) or '—'}; steering +{len(p['steering_add'])} −{len(p['steering_remove'])}")

    def do_rs_remove(self, s):
        done = []
        for t in self.rplan["remove"]:
            problems, self.removal = T.removal_plan(t)
            if problems: raise RuntimeError(f"{t}: " + "; ".join(problems))
            self.say(f"== removing {t}")
            for step in (self.do_rm_hosts, self.do_rm_nautobot, self.do_rm_labconf, self.do_rm_configure, self.do_rm_ces):
                step({})
            done.append(t)
        s["summary"] = ", ".join(done) or "nothing to remove"

    def do_rs_add(self, s):
        done = []
        for spec, new in [(x, True) for x in self.rplan["add"]] + [(x, False) for x in self.rplan["add_sites"]]:
            self.say(f"== adding {spec['name']}: " + ", ".join(x["dc"] for x in spec["sites"]))
            problems = T.validate(spec, new_tenant=new)
            if problems: raise RuntimeError(f"{spec['name']}: " + "; ".join(problems))
            self.spec = {**spec, "backup": self.spec.get("backup")}
            ces, hosts = T.apply_to_labconf(spec, new_tenant=new); self.spec["_ces"], self.spec["_hosts"] = ces, hosts
            self.sh([PY, LAB / "tools" / "gen_configs.py"])
            for step in (self.do_hosts, self.do_ces, self.do_configure):
                step({})
            done.append(spec["name"])
        s["summary"] = ", ".join(done) or "nothing to add"

    def do_rs_steering(self, s):
        for p in self.rplan["steering_remove"]:
            self.sh([PY, LAB / "tools" / "steer.py", "del", p["pe"], p["tenant"], p["prefix"]])
        for p in self.rplan["steering_add"]:
            self.sh([PY, LAB / "tools" / "steer.py", "add", p["pe"], p["tenant"], p["prefix"], *p["via"]])
        s["summary"] = f"{len(self.rplan['steering_add'])} added, {len(self.rplan['steering_remove'])} removed"


# ---- API ----------------------------------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def index(): return FileResponse(str(Path(__file__).resolve().parent / "static" / "index.html"))


@app.get("/api/state", tags=["state"], summary="Tenants, sites, hosts with live state")
def get_state(refresh: bool = Query(False), live: bool = Query(True)):
    st = state.get(refresh=refresh, live=live); return {k: v for k, v in st.items() if k != "inv"} | {"nodes": st["inv"]["nodes"], "service": st["inv"]["service"]}


@app.get("/api/topology.svg", tags=["state"], summary="The topology as SVG (live host colouring)", response_class=Response)
def topology_svg(live: bool = Query(True)):
    st = state.get(live=live)
    try:
        svg, _ = draw(st["inv"], live=st.get("hosts_live") if live else None)
    except Exception as e:   # noqa: BLE001 — a new node role the drawing does not know yet, most often; say which
        raise HTTPException(500, f"the topology could not be drawn: {e.__class__.__name__}: {e}"
                                 " — if tools/topology_svg.py changed, the portal has to be restarted (webapp/restart.sh)")
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


@app.get("/metrics", tags=["state"], summary="Prometheus metrics (tenant health, host reachability, IS-IS / BFD counts, VPNv4 sessions, runs)", response_class=PlainTextResponse)
def prometheus_metrics():
    body = M.render(collector.snapshot(), registry.list())
    body += "# HELP lab_collector_last_refresh_seconds When the background refresh last succeeded (0 = never)\n# TYPE lab_collector_last_refresh_seconds gauge\n"
    body += M.line("lab_collector_last_refresh_seconds", {"lab": "srv6-core"}, int(collector.last or 0)) + "\n"
    body += prober.metrics(M.line)
    hl = _health("1h")
    if hl:
        body += "# HELP lab_router_health Control-plane health score per router, 0-100 (see /api/health: every deduction has a reason)\n# TYPE lab_router_health gauge\n"
        body += "".join(M.line("lab_router_health", {"lab": "srv6-core", "node": r["node"], "role": r["role"]}, r["score"]) + "\n" for r in hl["routers"])
    cap = _capacity(max_age=None)
    if cap:
        body += "# HELP lab_capacity_room_tenants How many more tenants fit (every data centre, or one)\n# TYPE lab_capacity_room_tenants gauge\n"
        body += M.line("lab_capacity_room_tenants", {"lab": "srv6-core", "scope": "every_dc"}, cap["room"]["every_dc"]["tenants"]) + "\n"
        for dc, r in cap["room"]["per_dc"].items():
            body += M.line("lab_capacity_room_tenants", {"lab": "srv6-core", "scope": dc}, r["tenants"]) + "\n"
        body += "# HELP lab_capacity_used_ratio How much of a resource is in use (0-1)\n# TYPE lab_capacity_used_ratio gauge\n"
        for r in [x for d in cap["per_dc"] for x in (d["pe_ports"], d["ce_ports"])] + cap["lab"] + cap["host"]["resources"]:
            if r["pct"] is not None:
                body += M.line("lab_capacity_used_ratio", {"lab": "srv6-core", "resource": r["name"]}, round(r["pct"] / 100, 4)) + "\n"
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


@app.get("/api/sd", tags=["state"], summary="Prometheus HTTP service discovery: every exporter of the lab")
def prometheus_sd(): return M.targets(state._cache or state.model())   # never waits for a live refresh (the collector holds the state lock for a while)


@app.get("/api/iperf", tags=["state"], summary="Throughput between two tenant hosts (iperf3, blocks for ~10 s)")
def iperf(src: str = Query(..., examples=["dc1-h1"]), dst: str = Query(..., examples=["dc3-h1"]), seconds: int = Query(5, ge=2, le=30), udp: bool = Query(False), rate: str = Query("50M")):
    r = subprocess.run([PY, str(LAB / "tools" / "iperf.py"), src, dst, "-t", str(seconds), "--json"] + (["-u", "-b", rate] if udp else []), capture_output=True, text=True, timeout=120)
    if r.returncode != 0: raise HTTPException(422, (r.stderr or r.stdout).strip()[-400:])
    return json.loads(r.stdout)


@app.get("/api/steering", tags=["steering"], summary="Steering policies present on the PEs")
def steering_list(): return state.steering()


TEST_LOCK = "/tmp/srv6-core-test.lock"      # held by tests/run.sh for a whole test run (shell, portal or CI)


def test_run_active():
    """True while a test run holds the lab: the suites change the routers and assert on what they find, so anything
    else changing them at the same time fails tests that are not broken (CI run 16 lost 7 cases that way)."""
    import fcntl
    try:
        with open(TEST_LOCK, "a") as f:
            try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: return True
            fcntl.flock(f, fcntl.LOCK_UN); return False
    except OSError:
        return False


def refuse_while_testing(request: Request, what):
    """409 while a test run holds the lab — unless the request comes from the suites themselves (they say so in a
    header: a guard against accidents, not an access control)."""
    if request.headers.get("x-srv6-test-run") == "1": return
    if test_run_active():
        raise HTTPException(409, f"a test run is using the lab right now (it holds {TEST_LOCK}): {what} would change the routers "
                                 f"under the suites and fail them. Try again when it has finished (see Runs, or CI).")


@app.get("/api/lab/busy", tags=["state"], summary="Whether a test run holds the lab (the portal then refuses changes)")
def lab_busy(): return {"test_run": test_run_active()}


@app.post("/api/steering", tags=["steering"], summary="Add an explicit-path policy (applied now)", responses={409: {"description": "a test run holds the lab"}})
def steering_add(spec: SteerSpec, request: Request):
    refuse_while_testing(request, "adding a steering policy")
    r = subprocess.run([PY, str(LAB / "tools" / "steer.py"), "add", spec.pe, spec.tenant, spec.prefix, *spec.via], capture_output=True, text=True, timeout=300)
    if r.returncode != 0: raise HTTPException(422, (r.stderr or r.stdout).strip()[-500:])
    grafana.annotate(f"srv6-core: steering {spec.tenant} {spec.prefix} on {spec.pe} via {' > '.join(spec.via)}", tags=["srv6-core", "steering", spec.tenant])
    state._cache = None; return {"output": r.stdout}


@app.delete("/api/steering", tags=["steering"], summary="Remove a policy (applied now)", responses={409: {"description": "a test run holds the lab"}})
def steering_del(pe: str, tenant: str, prefix: str, request: Request):
    refuse_while_testing(request, "removing a steering policy")
    r = subprocess.run([PY, str(LAB / "tools" / "steer.py"), "del", pe, tenant, prefix], capture_output=True, text=True, timeout=300)
    if r.returncode != 0: raise HTTPException(422, (r.stderr or r.stdout).strip()[-500:])
    grafana.annotate(f"srv6-core: steering removed for {tenant} {prefix} on {pe}", tags=["srv6-core", "steering", tenant])
    state._cache = None; return {"output": r.stdout}


@app.post("/api/runs", tags=["runs"], summary="Start a pipeline run", responses={409: {"description": "a run is already in progress, or a test run holds the lab"}, 422: {"description": "validation problems"}})
def start_run(body: RunRequest, request: Request):
    """Modes: **tenant** (a TenantSpec: new tenant with its sites), **site** (a TenantSpec naming an existing tenant with the new site(s)),
    **remove** (`name`), **restore** (`name`: a backup file; see /api/backups/{file}/plan), **test** (Robot suites; `options.suites`, default the VPN / end-to-end / Nautobot suites, `["all"]` for everything)."""
    mode = body.mode; spec = None
    if mode != "test": refuse_while_testing(request, f"a {mode} run")
    if mode in ("tenant", "site"):
        if body.tenant is None: raise HTTPException(422, {"problems": ["tenant spec required"]})
        spec = body.tenant.model_dump(); problems = T.validate(spec, new_tenant=mode == "tenant")
        if problems: raise HTTPException(422, {"problems": problems})
    elif mode == "remove":
        problems, _ = T.removal_plan(body.name or "")
        if problems: raise HTTPException(422, {"problems": problems})
        spec = {"name": body.name}
    elif mode == "restore":
        try: p = BK.plan(BK.load(body.name or ""), state.steering())
        except ValueError as e: raise HTTPException(422, {"problems": [str(e)]})
        if p["problems"]: raise HTTPException(422, {"problems": p["problems"]})
        spec = {"name": f"restore {body.name}", "backup": body.name}
    elif mode != "test": raise HTTPException(400, "mode must be tenant, site, remove, restore or test")
    try: return registry.start(Run(mode, spec, body.options))
    except RuntimeError as e: raise HTTPException(409, str(e))


# ---- tenant SLA probes ------------------------------------------------------------------------------------------
def _host_run(ip, cmd, timeout):
    import paramiko
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, username=os.environ.get("HOST_USERNAME", "lab"), password=os.environ.get("HOST_PASSWORD", "lab"), timeout=20,
              look_for_keys=False, allow_agent=False)
    try:
        _, out, err = c.exec_command(cmd, timeout=timeout); text = out.read().decode(errors="replace"); rc = out.channel.recv_exit_status()
        return rc, text
    finally:
        c.close()


def _router_run(node, cmd, timeout):
    import paramiko
    n = next(x for x in T.inventory()["nodes"] if x["name"] == node)
    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(n["mgmt_ip"], username=os.environ.get("VYOS_USERNAME", "vyos"), password=os.environ.get("VYOS_PASSWORD", "vyos"), timeout=20,
              look_for_keys=False, allow_agent=False)
    try:
        _, out, err = c.exec_command(cmd, timeout=timeout); text = out.read().decode(errors="replace"); out.channel.recv_exit_status()
        return text
    finally:
        c.close()


def _lab_running():
    """Probe unless the last live look found every host down (the lab is stopped)."""
    live = (state._cache or {}).get("hosts_live")
    return not live or any((h or {}).get("reachable") for h in live.values())


prober = SLA.Prober(T.inventory, _host_run, annotate=lambda text, tags: grafana.annotate(text, tags=tags))
_cap = {"at": 0, "data": None, "busy": False}


def _capacity(max_age=120):
    """The capacity picture; max_age=None never blocks (a scrape): it hands back what is cached and refreshes behind."""
    if max_age is not None and (time.time() - _cap["at"] > max_age or _cap["data"] is None):
        _cap["data"], _cap["at"] = CAP.compute(), time.time()
    elif max_age is None and time.time() - _cap["at"] > 300 and not _cap["busy"]:
        def bg():
            _cap["busy"] = True
            try: _cap["data"], _cap["at"] = CAP.compute(), time.time()
            finally: _cap["busy"] = False
        threading.Thread(target=bg, daemon=True).start()
    return _cap["data"]


@app.on_event("startup")
def _start_prober():
    threading.Thread(target=prober.loop, args=(_lab_running,), daemon=True).start()


@app.get("/api/sla", tags=["sla"], summary="Every pair of a tenant's sites: the last probe's delay and loss, against the targets")
def sla_summary(): return prober.summary()


@app.get("/api/sla/history", tags=["sla"], summary="One pair's delay and loss over the last 24 hours (one point a minute)")
def sla_history(tenant: str, src: str, dst: str, since: float | None = None): return {"points": prober.history(tenant, src, dst, since)}


@app.post("/api/sla/probe", tags=["sla"], summary="Probe every pair now (blocks ~5 s)")
def sla_probe_now(): prober.run_once(); return prober.summary()


# ---- capacity -------------------------------------------------------------------------------------------------
@app.get("/api/capacity", tags=["capacity"], summary="Room for more tenants and sites: ports per data centre, tenant letters and blocks, host memory")
def capacity(refresh: bool = False): return _capacity(max_age=0 if refresh else 60)


# ---- backup and restore ---------------------------------------------------------------------------------------
@app.get("/api/backups", tags=["backup"], summary="The backups kept on the lab host, newest first")
def backups(): return BK.listing()


@app.post("/api/backups", tags=["backup"], summary="Back up the lab now (lab.conf, steering, every router's configuration; ~20 s)")
def backup_create(): return BK.create(state.steering())


@app.get("/api/backups/{file}", tags=["backup"], summary="Download a backup", response_class=Response)
def backup_download(file: str):
    try: BK.load(file)
    except ValueError as e: raise HTTPException(404, str(e))
    return FileResponse(str(BK.DIR / file), media_type="application/gzip", filename=file)


@app.get("/api/backups/{file}/plan", tags=["backup"], summary="What restoring a backup would change (nothing changes yet)")
def backup_plan(file: str):
    try: return BK.plan(BK.load(file), state.steering())
    except ValueError as e: raise HTTPException(422, str(e))


@app.post("/api/backups/upload", tags=["backup"], summary="Upload a backup (the .tar.gz as the request body); returns its plan")
async def backup_upload(request: Request):
    data = await request.body()
    try: name, b = BK.save_upload(data)
    except ValueError as e: raise HTTPException(422, str(e))
    return {"file": name, "plan": BK.plan(b, state.steering())}


# ---- steering on the map --------------------------------------------------------------------------------------
@app.get("/api/steering/map", tags=["steering"], summary="Every policy with its steered path and the IGP path it replaces (from the model)")
def steering_map():
    inv = T.inventory(); out = []
    for p in state.steering():
        if "pe" not in p: continue
        try: out.append({**p, **SM.paths_for(inv, p["pe"], p["tenant"], p["prefix"], segments=p.get("segments"))})
        except ValueError as e: out.append({**p, "error": str(e)})
    role = {n["name"]: n["role"] for n in inv["nodes"]}
    core = [{"a": l["a"], "b": l["b"], "a_port": l["a_port"], "b_port": l["b_port"], "prefix": l["prefix"]}
            for l in inv["links"] if role.get(l["a"]) in SM.CORE and role.get(l["b"]) in SM.CORE]
    return {"policies": out, "nodes": [{"name": n["name"], "role": n["role"], "dc": n.get("dc")} for n in inv["nodes"] if n["role"] in SM.CORE], "links": core}


@app.get("/api/steering/plan", tags=["steering"], summary="The path a policy would take, and the IGP path it would replace (nothing is applied)")
def steering_plan(pe: str, tenant: str, prefix: str, via: str = Query(..., description="P routers, comma- or space-separated")):
    try: return SM.paths_for(T.inventory(), pe, tenant, prefix, via=[v for v in re.split(r"[ ,]+", via) if v])
    except ValueError as e: raise HTTPException(422, str(e))


@app.get("/api/steering/measure", tags=["steering"], summary="Delay from the PE in the tenant's VRF: over the policy, and over the IGP (~3 s)")
def steering_measure(pe: str, tenant: str, prefix: str):
    try: return SM.measure(T.inventory(), pe, tenant, prefix, _router_run)
    except ValueError as e: raise HTTPException(422, str(e))


_WI = {"ts": 0, "inv": None, "pols": None, "lock": threading.Lock()}


def _whatif_context(refresh=False):
    """The inventory and the live steering policies, kept for a minute: reading the policies means asking every PE
    (~10 s), and the What-if view asks again on every click — the failures themselves are computed in no time."""
    with _WI["lock"]:
        if refresh or not _WI["inv"] or time.time() - _WI["ts"] > 60:
            inv = T.inventory(); pols = []; read = state.steering()
            if any("error" in p for p in read) and _WI["pols"] is not None:   # a failed read is not "no policies": keep the last good one
                _WI.update(ts=time.time(), inv=inv); return _WI["inv"], _WI["pols"], _WI["ts"]
            for p in read:
                if "pe" not in p: continue
                try: pols.append({**p, **SM.paths_for(inv, p["pe"], p["tenant"], p["prefix"], segments=p.get("segments"))})
                except ValueError: pass
            _WI.update(ts=time.time(), inv=inv, pols=pols)
        return _WI["inv"], _WI["pols"], _WI["ts"]


@app.get("/api/whatif", tags=["what-if"], summary="Fail links or routers on the model: what each tenant pair, the internet breakout and every steering policy would see (nothing is changed)")
def whatif(fail: list[str] = Query([], description="a router (p2) or a link (p1~p2); repeat for several"),
           refresh: bool = Query(False, description="re-read the inventory and the steering policies now (otherwise up to a minute old)")):
    inv, pols, ts = _whatif_context(refresh)
    try: out = WI.analyse(inv, fail, pols)
    except ValueError as e: raise HTTPException(422, str(e))
    role = {n["name"]: n["role"] for n in inv["nodes"]}
    out["nodes"] = [{"name": n["name"], "role": n["role"], "dc": n.get("dc")} for n in inv["nodes"] if n["role"] in SM.CORE]
    out["links"] = [{"a": l["a"], "b": l["b"]} for l in inv["links"] if role.get(l["a"]) in SM.CORE and role.get(l["b"]) in SM.CORE]
    out["rrs"] = inv["service"]["rrs"]; out["internet_pe"] = (inv["service"].get("internet") or {}).get("pe")
    out["as_of"] = ts
    return out


@app.get("/api/traffic", tags=["traffic"], summary="Load on every core link in each direction (interface counters) and what it is made of (sFlow): tenants, egress PEs, steered traffic, IS-IS / BFD / BGP; the tenant traffic matrix; where each steering policy's traffic was seen")
def traffic(window: str = Query("5m", pattern="^(1m|5m|15m|1h)$")):
    inv, pols, _ = _whatif_context()
    try: return TF.collect(inv, window, pols)
    except Exception as e: raise HTTPException(502, f"monitoring not reachable: {e.__class__.__name__}: {e}")   # noqa: BLE001


_HL = {}


def _health(window):
    """Scores are cheap (a few Prometheus and LogsQL queries) but /metrics is scraped every 30 s: keep each for 30 s."""
    c = _HL.get(window)
    if c and time.time() - c[0] < 30: return c[1]
    try: d = HL.collect(_whatif_context()[0], window)
    except Exception: return c[1] if c else None              # noqa: BLE001 — a scrape must not fail because the NMS is slow
    _HL[window] = (time.time(), d); return d


@app.get("/api/health", tags=["health"], summary="A control-plane health score per router (0-100): every deduction with its reason — sessions and adjacencies down now, flaps outside test runs, CPU, memory")
def health(window: str = Query("1h", pattern="^(1h|6h|24h)$")):
    d = _health(window)
    if d is None: raise HTTPException(502, "monitoring (Prometheus / VictoriaLogs) not reachable")
    return d


@app.get("/api/health/{node}/events", tags=["health"], summary="One router's routing-state syslog, newest first (marked when it happened during a test run)")
def health_events(node: str = PathParam(..., pattern=r"^[\w-]{1,40}$"), window: str = Query("24h", pattern="^(1h|6h|24h)$")):
    try: return {"node": node, "events": HL.events(node, window)}
    except Exception as e: raise HTTPException(502, f"VictoriaLogs not reachable: {e}")   # noqa: BLE001


install_runs_api(app, registry, resume_factory=lambda d: Run(d["mode"], d.get("spec"), d.get("options") or {}, resume_of=d))


app.mount("/results", StaticFiles(directory=str(RESULTS), html=True), name="results")
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("WEBAPP_HOST", "0.0.0.0"), port=int(os.environ.get("WEBAPP_PORT", "8091")))
