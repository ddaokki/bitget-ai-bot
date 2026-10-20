"""Static routes and executable browser safety contracts; never contact trading services."""

import shutil
import struct
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bitgetbot.panel.app import PanelDeps, create_app


STATIC = Path(__file__).resolve().parents[2] / "bitgetbot" / "panel" / "static"


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.script_text = []
        self.in_script = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self.in_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.script_text.append(data)


@pytest.fixture
def client(tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("Static routes must not access settings, processes or trading services")

    deps = PanelDeps(
        token="static-test-only", settings_loader=forbidden, reader_factory=forbidden,
        ft_factory=forbidden, runner=forbidden, pin_store_path=tmp_path / "pin.json",
    )
    with TestClient(create_app(deps), base_url="http://127.0.0.1:8090") as browser:
        yield browser


def test_index_uses_external_script_and_csp_safe_assets(client):
    response = client.get("/")
    assert response.status_code == 200
    document = Elements(response.text)
    tags = document.tags
    scripts = [attrs for tag, attrs in tags if tag == "script"]
    assert scripts == [{"src": "/static/app.js", "defer": None}]
    assert not "".join(document.script_text).strip()
    assert not any(tag == "style" for tag, _ in tags)
    assert not any(key == "style" or key.startswith("on") for _, attrs in tags for key in attrs)
    assert any(tag == "link" and attrs.get("href") == "/static/style.css" for tag, attrs in tags)
    assert any(tag == "link" and attrs.get("rel") == "apple-touch-icon" for tag, attrs in tags)
    assert "default-src 'self'" in response.headers["content-security-policy"]
    for _, attrs in tags:
        for key in ("src", "href"):
            if attrs.get(key) and not attrs[key].startswith("https://www.bitget.com/"):
                assert attrs[key].startswith(("/", "#"))


def test_manifest_is_installable_with_both_png_icons(client):
    response = client.get("/manifest.webmanifest")
    assert response.status_code == 200
    manifest = response.json()
    assert manifest["name"] == "비트겟 봇"
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}
    for icon in manifest["icons"]:
        assert icon["type"] == "image/png"
        image = client.get(icon["src"])
        assert image.status_code == 200
        assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
        width, height = struct.unpack(">II", image.content[16:24])
        assert f"{width}x{height}" == icon["sizes"]


def test_service_worker_is_public_and_explicitly_excludes_api(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert "/api/" in response.text
    assert "javascript" in response.headers["content-type"]


def test_browser_code_has_csrf_header_and_no_persistent_secret_storage(client):
    response = client.get("/static/app.js")
    assert response.status_code == 200
    assert "X-Panel-Request" in response.text
    assert 'credentials: "same-origin"' in response.text
    for forbidden in ("localStorage", "sessionStorage", "indexedDB"):
        assert forbidden not in response.text
    assert client.get("/static/style.css").status_code == 200


@pytest.fixture
def javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for executable browser safety checks")

    def run(code, *, worker=False):
        script = STATIC / ("sw.js" if worker else "app.js")
        assert script.is_file(), f"Missing browser implementation: {script.name}"
        harness = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const listeners = {};
const elements = new Map();
const ids = new Set([...fs.readFileSync(require("node:path").join(require("node:path").dirname(process.argv[1]), "index.html"), "utf8").matchAll(/\bid="([^"]+)"/g)].map(match => match[1]));
const timerDelays = [];
function element(id) {
    if (typeof id === "string") assert.ok(ids.has(id), `Unknown HTML element: ${id}`);
    if (!elements.has(id)) elements.set(id, {
        value: "", textContent: "", hidden: false, disabled: false, open: false,
        className: "", dataset: {}, children: [],
        classList: {toggle() {}, add() {}, remove() {}},
        addEventListener() {}, setAttribute() {}, removeAttribute() {}, focus() {},
        replaceChildren(...children) {this.children = children;},
        append(...children) {this.children.push(...children);},
        showModal() {this.open = true;}, close() {this.open = false;}
    });
    return elements.get(id);
}
const requests = [];
let response = {ok: true, status: 200, json: async () => ({ok: true}), headers: {get: () => null}};
const context = vm.createContext({
    console, URL, Date, Intl, Number, Math, Set, Map, Promise, AbortController,
    location: {hostname: "127.0.0.1", origin: "http://127.0.0.1:8090"},
    document: {
        getElementById: element, querySelectorAll: () => [],
        createElement: tag => element(Symbol(tag)),
        addEventListener: (name, callback) => {listeners[name] = callback;},
        hidden: false
    },
    navigator: {onLine: true},
    window: {addEventListener() {}},
    setTimeout: (callback, delay) => {timerDelays.push(delay); return 1;}, clearTimeout() {},
    setInterval: () => 1, clearInterval() {},
    fetch: async (url, options) => {
        requests.push({url, options});
        if (url === "/api/status" && response.ok) return {...response, json: async () => status};
        if (url === "/api/job" && response.ok) return {...response, json: async () => status.job};
        return response;
    },
    self: {location: {origin: "http://127.0.0.1:8090"},
           addEventListener: (name, callback) => {listeners[name] = callback;}},
});
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
const run = code => vm.runInContext(code, context);
const status = {
    mode: "dry", freqtrade: {reachable: true, state: "running", dry_run: true},
    account: null, positions: [], stops: null,
    smoke_state: {exists: false, trade_id: null, mode: null, close_confirmed: false},
    keys_configured: {key: true, secret: true, password: true}, pin_set: true,
    docker_running: true, session_kind: "local",
    job: {name: null, running: false, exit_code: null, lines: [], started_at: null, finished_at: null},
    tailscale: {installed: true, logged_in: true, dns_name: "pc.tail1234.ts.net", serving: false, funnel: false}
};
context.fixture = status;
context.element = element;
context.requests = requests;
(async () => {
'''
        result = subprocess.run(
            [node, "-e", harness + code + '\n})().catch(error => {console.error(error); process.exitCode = 1;});', str(script)],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert not result.stderr, result.stderr

    return run


def test_steps_require_current_screen_preflight_and_correct_smoke_state(javascript):
    javascript(r'''
run("applyStatus(fixture)");
assert.equal(run('stepAllowed("preflight")'), true);
assert.equal(run('stepAllowed("enter")'), false);
assert.equal(run('stepAllowed("check")'), false);
// A historical successful preflight must not unlock this page.
run('fixture.job = {name:"preflight",running:false,exit_code:0,started_at:5,lines:[]}; applyStatus(fixture)');
assert.equal(run('stepAllowed("enter")'), false);
// Only a new preflight accepted in this page can unlock entry.
run('state.pendingPreflight = {mode:"dry", previous:5}; acceptJob({...fixture.job,started_at:6})');
assert.equal(run('stepAllowed("enter")'), true);
run('fixture.smoke_state.exists = true; applyStatus(fixture)');
assert.equal(run('stepAllowed("enter")'), false);
assert.equal(run('stepAllowed("preflight")'), false);
for (const name of ["check", "restart-check", "exit"]) assert.equal(run(`stepAllowed("${name}")`), true);
assert.equal(run('stepAllowed("ledger")'), false);
run('fixture.mode="live"; fixture.freqtrade.dry_run=false; fixture.smoke_state.mode="live"; fixture.smoke_state.close_confirmed=true; applyStatus(fixture)');
assert.equal(run('stepAllowed("ledger")'), true);
run('state.job.running=true');
for (const name of ["preflight", "enter", "check", "restart-check", "exit", "ledger"]) assert.equal(run(`stepAllowed("${name}")`), false);
run('state.job.running=false; state.online=false');
assert.equal(run('stepAllowed("ledger")'), false);
''')


def test_unknown_mode_or_mode_mismatch_never_allows_entry(javascript):
    javascript(r'''
run('applyStatus(fixture); state.preflightMode="dry"');
assert.equal(run('stepAllowed("enter")'), true);
run('fixture.freqtrade.dry_run=false; applyStatus(fixture)');
assert.equal(run('stepAllowed("enter")'), false);
run('fixture.mode=null; applyStatus(fixture)');
assert.equal(run('stepAllowed("enter")'), false);
assert.match(element("mode-help").textContent, /모드.*확인/);
assert.doesNotMatch(element("mode-help").textContent, /모의 거래입니다/);
run('fixture.mode="live"; fixture.freqtrade.dry_run=false; applyStatus(fixture)');
assert.equal(run('stepAllowed("enter")'), false);
''')


def test_live_modal_requires_exact_text_and_never_submits_on_open(javascript):
    javascript(r'''
run('fixture.mode="live"; fixture.freqtrade.dry_run=false; applyStatus(fixture); state.preflightMode="live"; openConfirmation({kind:"step",value:"enter",live:true})');
assert.equal(requests.length, 0);
for (const value of ["", "live", " LIVE", "LIVE "]) {
    element("live-confirm").value=value;
    run("updateConfirmation()");
    assert.equal(element("confirm-action").disabled, true);
    await run("confirmAction()");
    assert.equal(requests.length, 0);
}
element("live-confirm").value="LIVE";
run("updateConfirmation()");
assert.equal(element("confirm-action").disabled, false);
// A job starting elsewhere while the dialog is open must invalidate confirmation.
run('state.job.running=true');
await run("confirmAction()");
assert.equal(requests.length, 0);
''')


def test_posts_have_csrf_credentials_and_are_never_retried(javascript):
    javascript(r'''
await run('request("/api/steps/enter", {confirm:"LIVE"})');
assert.equal(requests.length, 1);
assert.equal(requests[0].options.method, "POST");
assert.equal(requests[0].options.headers["X-Panel-Request"], "1");
assert.equal(requests[0].options.credentials, "same-origin");
assert.deepEqual(JSON.parse(requests[0].options.body), {confirm:"LIVE"});
context.fetch = async () => {requests.push({failed:true}); throw Error("network");};
await assert.rejects(run('request("/api/emergency/close-all", {})'));
assert.equal(requests.length, 2);
''')


def test_emergency_in_flight_blocks_normal_actions(javascript):
    javascript(r'''
run('applyStatus(fixture); state.emergencyPending=true; updateControls()');
assert.equal(run('stepAllowed("preflight")'), false);
assert.equal(element("mode-switch").disabled, true);
assert.equal(element("docker-up").disabled, true);
assert.equal(element("save-keys").disabled, true);
''')


def test_emergency_requires_separate_confirmation_within_three_seconds(javascript):
    javascript(r'''
run('applyStatus(fixture)');
await run('confirmEmergency()');
assert.equal(requests.length, 0);
run('armEmergency()');
assert.equal(requests.length, 0);
run('state.emergencyDeadline=Date.now()-1');
await run('confirmEmergency()');
assert.equal(requests.length, 0);
run('armEmergency()');
await run('confirmEmergency()');
assert.equal(requests.filter(r => r.url==="/api/emergency/close-all").length, 1);
await run('confirmEmergency()');
assert.equal(requests.filter(r => r.url==="/api/emergency/close-all").length, 1);
''')


@pytest.mark.parametrize("authenticated", [False, True])
def test_emergency_remains_available_after_status_failure_while_offline(javascript, authenticated):
    javascript(('run("applyStatus(fixture)");' if authenticated else '') + r'''
run('state.online=false');
response={ok:false,status:503,json:async()=>({detail:"상태 조회 실패"}),headers:{get:()=>null}};
await run('poll()');
assert.equal(run('state.online'), false);
assert.equal(run('state.needsLogin'), false);
assert.equal(element("emergency-arm").disabled, false);
run('armEmergency()');
assert.ok(run('state.emergencyDeadline') > Date.now());
assert.equal(element("emergency-confirm").hidden, false);
assert.equal(element("emergency-confirm").disabled, false);
assert.equal(requests.filter(r => r.options.method === "POST").length, 0);
await Promise.all([run('confirmEmergency()'), run('confirmEmergency()')]);
await run('confirmEmergency()');
const exits = requests.filter(r => r.url === "/api/emergency/close-all");
assert.equal(exits.length, 1);
assert.equal(exits[0].options.method, "POST");
assert.equal(exits[0].options.headers["X-Panel-Request"], "1");
assert.equal(exits[0].options.credentials, "same-origin");
assert.match(element("emergency-message").textContent, /자동 재시도하지 않습니다/);
''')


def test_status_failure_does_not_cancel_armed_emergency(javascript):
    javascript(r'''
run('applyStatus(fixture); armEmergency()');
const deadline = run('state.emergencyDeadline');
response={ok:false,status:503,json:async()=>({detail:"상태 조회 실패"}),headers:{get:()=>null}};
await run('poll()');
assert.equal(run('state.emergencyDeadline'), deadline);
assert.equal(element("emergency-confirm").hidden, false);
assert.equal(element("emergency-confirm").disabled, false);
await run('confirmEmergency()');
assert.equal(requests.filter(r => r.url === "/api/emergency/close-all").length, 1);
''')


def test_emergency_stays_disabled_after_401_even_if_status_later_fails(javascript):
    javascript(r'''
run('applyStatus(fixture); armEmergency()');
response={ok:false,status:401,json:async()=>({detail:"로그인이 필요합니다"}),headers:{get:()=>null}};
await run('poll()');
assert.equal(run('state.needsLogin'), true);
assert.equal(element("emergency-arm").disabled, true);
assert.equal(element("emergency-confirm").hidden, true);
response={ok:false,status:503,json:async()=>({detail:"상태 조회 실패"}),headers:{get:()=>null}};
await run('poll()');
run('armEmergency()');
assert.equal(run('state.emergencyDeadline'), 0);
assert.equal(element("emergency-arm").disabled, true);
run('state.emergencyDeadline=Date.now()+3000');
await run('confirmEmergency()');
assert.equal(element("emergency-confirm").disabled, true);
assert.equal(requests.filter(r => r.options.method === "POST").length, 0);
''')


def test_submit_clears_passwords_even_when_action_is_blocked(javascript):
    javascript(r'''
run('applyStatus(fixture); state.pending=true');
for (const id of ["api-key", "api-secret", "api-password"]) element(id).value="fake-test-input";
await run('saveCredentials({preventDefault(){}}, "keys")');
for (const id of ["api-key", "api-secret", "api-password"]) assert.equal(element(id).value, "");
element("login-pin").value="123456";
await run('login({preventDefault(){}})');
assert.equal(element("login-pin").value, "");
assert.equal(requests.length, 0);
''')


def test_observed_smoke_state_invalidates_old_preflight(javascript):
    javascript(r'''
run('applyStatus(fixture); fixture.job={name:"preflight",running:false,exit_code:0,started_at:6,lines:[]}; state.pendingPreflight={mode:"dry",previous:5}; applyStatus(fixture)');
assert.equal(run('stepAllowed("enter")'), true);
run('fixture.smoke_state.exists=true; applyStatus(fixture)');
run('fixture.smoke_state.exists=false; applyStatus(fixture)');
assert.equal(run('stepAllowed("enter")'), false);
''')


def test_live_enter_and_mode_send_exact_confirmation_once(javascript):
    javascript(r'''
run('fixture.mode="live"; fixture.freqtrade.dry_run=false; applyStatus(fixture); state.preflightMode="live"; openConfirmation({kind:"step",value:"enter",live:true})');
element("live-confirm").value="LIVE";
await Promise.all([run('confirmAction()'), run('confirmAction()')]);
const enter = requests.filter(r => r.url === "/api/steps/enter");
assert.equal(enter.length, 1);
assert.deepEqual(JSON.parse(enter[0].options.body), {confirm:"LIVE"});
run('fixture.mode="dry"; fixture.freqtrade.dry_run=true; applyStatus(fixture); openConfirmation({kind:"mode",value:"live",live:true})');
element("live-confirm").value="LIVE";
await run('confirmAction()');
const mode = requests.filter(r => r.url === "/api/mode");
assert.equal(mode.length, 1);
assert.deepEqual(JSON.parse(mode[0].options.body), {mode:"live",confirm:"LIVE"});
assert.equal(element("live-confirm").value, "");
''')


def test_session_expiry_uses_local_reopen_or_tailnet_login(javascript):
    javascript(r'''
run('applyStatus(fixture); sessionLost()');
assert.equal(element("auth-screen").hidden, true);
assert.match(element("connection").textContent, /바탕화면 바로가기/);
assert.equal(element("emergency-arm").disabled, true);
run('fixture.session_kind="tailnet"; applyStatus(fixture); sessionLost()');
assert.equal(element("auth-screen").hidden, false);
assert.equal(element("local-settings").hidden, true);
assert.equal(element("panel").hidden, true);
assert.equal(run('stepAllowed("enter")'), false);
''')


def test_login_lockout_and_missing_pin_clear_inputs_without_retry(javascript):
    javascript(r'''
run('state.needsLogin=true');
response={ok:false,status:429,json:async()=>({detail:"잠김",retry_after:900}),headers:{get:()=>"900"}};
element("login-pin").value="123456";
await run('login({preventDefault(){}})');
assert.equal(element("login-pin").value, "");
assert.equal(element("login-button").disabled, true);
assert.ok(run('state.loginDeadline-Date.now()') > 890000);
await run('login({preventDefault(){}})');
assert.equal(requests.length, 1);
run('state.loginDeadline=0');
response={ok:false,status:409,json:async()=>({detail:"PC에서 PIN을 먼저 설정하세요"}),headers:{get:()=>null}};
element("login-pin").value="123456";
await run('login({preventDefault(){}})');
assert.match(element("login-message").textContent, /PC에서 PIN을 먼저 설정/);
assert.equal(element("login-pin").value, "");
assert.equal(requests.length, 2);
''')


def test_poll_intervals_and_completed_job_refresh_status(javascript):
    javascript(r'''
run('applyStatus(fixture); schedulePoll()');
assert.equal(timerDelays.at(-1), 5000);
run('state.job={name:"check",running:true,exit_code:null,started_at:1,lines:[]}; schedulePoll()');
assert.equal(timerDelays.at(-1), 1000);
await run('poll()');
assert.deepEqual(requests.map(r=>r.url), ["/api/job", "/api/status"]);
assert.equal(timerDelays.at(-1), 5000);
''')


def test_keys_are_local_only_and_funnel_unknown_blocks_phone(javascript):
    javascript(r'''
run('applyStatus(fixture)');
assert.equal(element("phone-enable").disabled, false);
run('fixture.tailscale.funnel=null; applyStatus(fixture)');
assert.equal(element("phone-enable").disabled, true);
assert.equal(element("phone-address").hidden, true);
assert.match(element("ts-funnel").textContent, /확인 불가/);
run('fixture.session_kind="tailnet"; applyStatus(fixture)');
assert.equal(element("local-settings").hidden, true);
element("api-key").value="fake-test-input";
await run('saveCredentials({preventDefault(){}},"keys")');
assert.equal(requests.length, 0);
''')


@pytest.mark.parametrize("path", [
    "/", "/static/index.html", "/static/style.css", "/static/app.js",
    "/manifest.webmanifest", "/static/icon-192.png", "/static/icon-512.png",
])
def test_service_worker_shell_install_and_network_first(javascript, path):
    javascript("const path = " + repr(path) + ";" + r'''
const writes = [], prefetched = [], order = [];
const stale = {old:true}, fresh = {ok:true,redirected:false,clone(){return this;}};
const stored = new Map([[path, stale]]);
context.Request = class {
    constructor(path, options) {this.url=new URL(path,"http://127.0.0.1:8090").href; this.options=options;}
};
context.caches = {open:async()=>({
    addAll:async paths=>prefetched.push(...paths),
    match:async path=>{order.push("cache"); return stored.get(path);},
    put:async(path,value)=>{writes.push({path,value}); stored.set(path,value);}
})};
let installed;
listeners.install({waitUntil(promise){installed=promise;}});
await installed;
assert.equal(prefetched.length, 7);
for (const req of prefetched) {
    assert.equal(req.options.credentials, "omit");
    assert.equal(new URL(req.url).search, "");
    assert.equal(new URL(req.url).pathname.startsWith("/api"), false);
}
let result;
const event={request:{url:new URL(path,"http://127.0.0.1:8090").href,method:"GET"}, respondWith(promise){result=promise;}};
context.fetch=async()=>{order.push("network"); return fresh;};
listeners.fetch(event);
assert.equal(await result, fresh);
assert.deepEqual(order, ["network"]);
assert.deepEqual(writes.map(write=>write.path), [path]);
assert.equal(stored.get(path), fresh);
context.fetch=async()=>{order.push("network"); throw Error("offline");};
order.length=0;
listeners.fetch(event);
assert.equal(await result, fresh);
assert.deepEqual(order, ["network", "cache"]);
context.fetch=async()=>({...fresh,redirected:true});
listeners.fetch(event);
await result;
assert.equal(writes.length, 1);
''', worker=True)


def test_service_worker_never_intercepts_api_posts_tokens_or_foreign_requests(javascript):
    javascript(r'''
for (const [path, method] of [["/api/status","GET"],["/api/job","GET"],
    ["/api/login","POST"],["/static/app.js","POST"],["/?t=do-not-cache","GET"],
    ["/static/app.js?token=do-not-cache","GET"],["https://other.invalid/","GET"],
    ["/not-shell","GET"]]) {
    let intercepted=false;
    listeners.fetch({request:{url:new URL(path,"http://127.0.0.1:8090").href, method}, respondWith(){intercepted=true;}});
    assert.equal(intercepted, false, path);
}
''', worker=True)
