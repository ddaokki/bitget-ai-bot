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
        record_on_startup=False,
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
        createElementNS: (namespace, tag) => Object.assign(element(Symbol(tag)), {
            namespace, tagName: tag, attributes: {},
            setAttribute(name, value) {
                assert.equal(typeof value, "string");
                assert.ok(!/^on/i.test(name) && name !== "style" && name !== "href", `unsafe SVG attribute: ${name}`);
                this.attributes[name] = value;
            }
        }),
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


EMERGENCY_SCOPE = "거래 봇(Freqtrade)이 연 거래를 시장가로 청산 요청 (현재 모드)"


def test_emergency_copy_names_freqtrade_trades_not_all_positions(client):
    html = client.get("/").text
    script = client.get("/static/app.js").text
    assert EMERGENCY_SCOPE in html
    for text in (html, script):
        assert "모든 포지션" not in text
        assert "전부 청산" not in text


def test_mode_switch_is_locked_while_smoke_state_exists(javascript):
    javascript(r'''
run('applyStatus(fixture)');
assert.equal(element("mode-switch").disabled, false);
assert.equal(run('actionAllowed({kind:"mode",value:"live",fromMode:"dry"})'), true);
run('fixture.smoke_state.exists=true; applyStatus(fixture)');
assert.equal(element("mode-switch").disabled, true);
assert.equal(run('actionAllowed({kind:"mode",value:"live",fromMode:"dry"})'), false);
run('fixture.smoke_state=null; applyStatus(fixture)');
assert.equal(element("mode-switch").disabled, true);
''')


def test_emergency_success_shows_server_scope_message(javascript):
    javascript(r'''
run('applyStatus(fixture); armEmergency()');
assert.doesNotMatch(element("emergency-arm").textContent, /전부/);
response={ok:true,status:200,json:async()=>({ok:true,message:"모의 거래만 청산했습니다. 실거래 포지션은 비트겟 웹에서 확인하세요."}),headers:{get:()=>null}};
await run('confirmEmergency()');
assert.match(element("emergency-message").textContent, /모의 거래만 청산했습니다/);
assert.match(element("emergency-message").textContent, /체결 보장이 아닙니다/);
assert.doesNotMatch(element("emergency-message").textContent, /전부/);
''')


def test_bot_stats_card_is_csp_safe_and_text_only(client):
    document = Elements(client.get("/").text)
    ids = {attrs.get("id") for _, attrs in document.tags}
    assert {"stats-card", "stats-mode", "stats-main", "stats-detail"} <= ids
    script = client.get("/static/app.js").text
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in script


def test_bot_stats_show_current_mode_win_rate(javascript):
    javascript(r'''
status.stats = {closed_trades: 13, wins: 8, losses: 5, win_rate: 8 / 13, avg_profit_pct: 0.8,
                 win_rate_30d: 0.55, closed_trades_30d: 9};
run('applyStatus(fixture)');
assert.equal(element("stats-mode").textContent, "모의");
assert.equal(element("stats-main").textContent, "승률 62% (8승 5패 · 13건)");
assert.equal(element("stats-detail").textContent, "최근 30일 55% · 평균 +0.8%");
run('fixture.mode="live"; fixture.freqtrade.dry_run=false; fixture.stats.avg_profit_pct=-1.25; applyStatus(fixture)');
assert.equal(element("stats-mode").textContent, "실전");
assert.equal(element("stats-detail").textContent, "최근 30일 55% · 평균 -1.25%");
run('fixture.stats.win_rate_30d=null; fixture.stats.closed_trades_30d=0; applyStatus(fixture)');
assert.match(element("stats-detail").textContent, /^최근 30일 거래 없음 · /);
run('fixture.stats.closed_trades_30d=null; fixture.stats.avg_profit_pct=null; applyStatus(fixture)');
assert.equal(element("stats-detail").textContent, "최근 30일 – · 평균 –");
''')


@pytest.mark.parametrize("stats, main", [
    ("{closed_trades: 0, wins: 0, losses: 0, win_rate: null, avg_profit_pct: null, win_rate_30d: null, closed_trades_30d: 0}",
     "아직 닫힌 거래가 없습니다"),
    ("null", "–"),
    ("undefined", "–"),
    ('{closed_trades: "<img src=x onerror=alert(1)>", wins: 1, losses: 0, win_rate: 1}', "–"),
], ids=["no-closed-trades", "null", "missing", "malformed"])
def test_bot_stats_empty_or_unavailable(javascript, stats, main):
    javascript(r'''
status.stats = ''' + stats + r''';
run('applyStatus(fixture)');
assert.equal(element("stats-main").textContent, ''' + repr(main) + r''');
assert.equal(element("stats-detail").textContent, "");
assert.doesNotMatch(element("stats-main").textContent, /img/);
''')


def test_autotrade_buttons_follow_bot_state_and_live_needs_exact_confirmation(javascript):
    javascript(r'''
run('fixture.freqtrade.state="running"; applyStatus(fixture)');
assert.equal(run('autotradeAllowed("start")'), false);
assert.equal(run('autotradeAllowed("pause")'), true);
await run('command("/api/autotrade", {action:"pause"})');
let calls = requests.filter(r => r.url === "/api/autotrade");
assert.equal(calls.length, 1);
assert.deepEqual(JSON.parse(calls[0].options.body), {action:"pause"});
run('fixture.freqtrade.state="stopped"; applyStatus(fixture); openConfirmation({kind:"autotrade",value:"start",live:false})');
await run('confirmAction()');
calls = requests.filter(r => r.url === "/api/autotrade");
assert.equal(calls.length, 2);
assert.deepEqual(JSON.parse(calls[1].options.body), {action:"start"});
run('fixture.mode="live"; fixture.freqtrade.dry_run=false; fixture.freqtrade.state="paused"; applyStatus(fixture); openConfirmation({kind:"autotrade",value:"start",live:true})');
element("live-confirm").value="LIV";
await run('confirmAction()');
assert.equal(requests.filter(r => r.url === "/api/autotrade").length, 2);
element("live-confirm").value="LIVE";
await run('confirmAction()');
calls = requests.filter(r => r.url === "/api/autotrade");
assert.equal(calls.length, 3);
assert.deepEqual(JSON.parse(calls[2].options.body), {action:"start", confirm:"LIVE"});
run('fixture.freqtrade.dry_run=true; applyStatus(fixture)');
assert.equal(run('autotradeAllowed("start")'), false);
''')


def test_container_button_shows_only_when_bot_is_down(client, javascript):
    document = Elements(client.get("/").text)
    button = next(attrs for tag, attrs in document.tags if attrs.get("id") == "docker-up")
    assert "hidden" in button
    assert "거래 봇 컨테이너 켜기" in client.get("/").text
    assert ">거래 봇 시작<" not in client.get("/").text
    javascript(r'''
run('applyStatus(fixture)');
assert.equal(element("docker-up").hidden, true);
run('fixture.freqtrade.reachable=false; applyStatus(fixture)');
assert.equal(element("docker-up").hidden, false);
run('fixture.freqtrade.reachable=true; fixture.docker_running=false; applyStatus(fixture)');
assert.equal(element("docker-up").hidden, false);
run('fixture.docker_running=true; fixture.freqtrade=null; applyStatus(fixture)');
assert.equal(element("docker-up").hidden, false);
''')


def test_live_switch_dialog_says_real_money_autotrade_starts_right_after(javascript):
    javascript(r'''
run('applyStatus(fixture); openConfirmation({kind:"mode",value:"live",live:true})');
assert.match(element("confirm-title").textContent, /자동매매/);
assert.match(element("confirm-description").textContent, /실제 돈으로 자동매매가 시작됩니다/);
assert.match(element("confirm-description").textContent, /준비되는 즉시/);
assert.equal(element("live-field").hidden, false);
run('closeConfirmation(); fixture.mode="live"; fixture.freqtrade.dry_run=false; applyStatus(fixture); openConfirmation({kind:"mode",value:"dry",live:false})');
assert.match(element("confirm-description").textContent, /모의 자동매매가 저절로 켜집니다/);
assert.equal(element("live-field").hidden, true);
''')


def test_manual_autotrade_controls_remain(client):
    html = client.get("/").text
    assert "자동매매 켜기" in html and "자동매매 멈추기" in html


def test_stats_exclude_manual_and_smoke_trades(javascript):
    javascript(r'''
status.stats = {closed_trades: 0, wins: 0, losses: 0, win_rate: null, avg_profit_pct: null,
                win_rate_30d: null, closed_trades_30d: 0, other_trades: 1, partial: false};
run('fixture.mode="live"; fixture.freqtrade.dry_run=false; applyStatus(fixture)');
assert.equal(element("stats-main").textContent, "아직 전략 거래가 없습니다");
assert.equal(element("stats-other").textContent, "수동·점검 거래 1건 별도 (승률 제외)");
assert.doesNotMatch(element("stats-main").textContent, /패/);
run('fixture.stats = {closed_trades: 2, wins: 1, losses: 1, win_rate: 0.5, avg_profit_pct: 0.1, win_rate_30d: 0.5, closed_trades_30d: 2, other_trades: 0, partial: true}; applyStatus(fixture)');
assert.equal(element("stats-main").textContent, "승률 50% (1승 1패 · 2건)");
assert.equal(element("stats-other").textContent, "");
assert.match(element("stats-detail").textContent, /최근 500건 기준$/);
''')


def test_saved_keys_are_never_shown_back_and_status_says_saved(client, javascript):
    html = client.get("/").text
    assert "다시 보여 주지 않아" in html
    document = Elements(html)
    for name in ("api-key", "api-secret", "api-password"):
        attrs = next(attrs for tag, attrs in document.tags if attrs.get("id") == name)
        assert attrs["type"] == "password" and "value" not in attrs
    javascript(r'''
run('applyStatus(fixture)');
assert.match(element("keys-state").textContent, /^저장됨/);
for (const id of ["api-key", "api-secret", "api-password"]) assert.equal(element(id).value, "");
run('fixture.keys_configured.secret=false; applyStatus(fixture)');
assert.match(element("keys-state").textContent, /^미설정/);
''')


def test_key_hint_names_the_word_the_status_line_shows(client):
    html = client.get("/").text
    script = client.get("/static/app.js").text
    assert '위 상태가 "저장됨"이면 다시 넣을 필요가 없습니다.' in html
    assert '"저장됨 · 다시 넣지 않아도 됩니다"' in script
    assert '"설정됨"이면' not in html


def test_charts_card_is_at_the_top_and_csp_safe(client):
    html = client.get("/").text
    document = Elements(html)
    ids = [attrs.get("id") for _, attrs in document.tags if attrs.get("id")]
    for name in ("charts-card", "asset-main", "asset-sub", "asset-change", "asset-chart",
                 "btc-last", "btc-levels", "btc-chart", "stats-other"):
        assert name in ids
    assert ids.index("charts-card") < ids.index("docker-state") < ids.index("stats-card")
    assert "<svg" not in html                     # charts are drawn by DOM APIs at runtime
    script = client.get("/static/app.js").text
    assert "createElementNS" in script and "http://www.w3.org/2000/svg" in script
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", ".style."):
        assert forbidden not in script


CHARTS = r'''
const hour = 3600000, start = 1792000000000;
const chartData = {
    mode: "live",
    asset: {mode: "live", equity_usdt: 114.25, krw_per_usdt: 1400, equity_krw: 159950,
            history: [[1792000000, 100], [1792000600, 105]], since: 1792000000,
            change_usdt: 14.25, change_pct: 14.25},
    btc: {pair: "BTCUSDT", timeframe: "1h", closes: Array.from({length: 72}, (_, i) => [start + i * hour, 60000 + i]),
          entry: 60050, stop: 58000, entries: [start + 50 * hour + 1, start - hour]}
};
const svgNodes = id => element(id).children[0]?.children || [];
'''


def test_asset_chart_shows_krw_large_usd_below_and_change(javascript):
    javascript(CHARTS + r'''
run('applyStatus(fixture)');
context.chartData = chartData;
run('renderCharts(chartData)');
assert.equal(element("asset-mode").textContent, "실전");
assert.equal(element("asset-main").textContent, "159,950원");
assert.equal(element("asset-sub").textContent, "$114.25 · 1 USDT = 1,400원");
assert.match(element("asset-change").textContent, /^기록 시작\(.+\) 대비 \+\$14\.25 \(\+14\.25%\)$/);
const root = element("asset-chart").children[0];
assert.equal(root.tagName, "svg");
assert.equal(root.namespace, "http://www.w3.org/2000/svg");
const line = root.children.find(child => child.tagName === "polyline");
assert.equal(line.attributes.points.split(" ").length, 3);   // two history points + now
assert.ok(root.children.some(child => child.tagName === "line" && /base/.test(child.attributes.class)));
''')


def test_asset_chart_falls_back_to_usd_only_and_waits_for_history(javascript):
    javascript(CHARTS + r'''
run('applyStatus(fixture)');
chartData.asset = {mode: "dry", equity_usdt: 100, krw_per_usdt: null, equity_krw: null, history: [], since: null,
                   change_usdt: null, change_pct: null};
context.chartData = chartData;
run('renderCharts(chartData)');
assert.equal(element("asset-mode").textContent, "모의");
assert.equal(element("asset-main").textContent, "$100.00");
assert.match(element("asset-sub").textContent, /원화 환율 조회 불가/);
assert.match(element("asset-chart").children[0].textContent, /기록을 모으는 중/);
''')


def test_btc_chart_draws_closes_entry_stop_and_markers(javascript):
    javascript(CHARTS + r'''
run('applyStatus(fixture)');
context.chartData = chartData;
run('renderCharts(chartData)');
assert.equal(element("btc-last").textContent, "$60,071");
assert.match(element("btc-levels").textContent, /진입 \$60,050 · 손절 \$58,000/);
const nodes = svgNodes("btc-chart");
assert.equal(nodes.find(n => n.tagName === "polyline").attributes.points.split(" ").length, 72);
const levels = nodes.filter(n => n.tagName === "line").map(n => n.attributes.class);
assert.deepEqual(levels, ["chart-level entry", "chart-level stop"]);
const markers = nodes.filter(n => n.tagName === "circle");
assert.equal(markers.length, 1);                              // the entry before the window is skipped
assert.ok(nodes.every(n => Object.values(n.attributes).every(v => !/NaN|undefined/.test(v))));
chartData.btc.entry = null; chartData.btc.stop = null; chartData.btc.entries = [];
run('renderCharts(chartData)');
assert.equal(element("btc-levels").textContent, "열린 전략 거래 없음");
assert.equal(svgNodes("btc-chart").filter(n => n.tagName === "line").length, 0);
''')


@pytest.mark.parametrize("btc", ["null", '"<img src=x onerror=alert(1)>"', "{closes: 'x'}",
                                 "{closes: [[1, NaN], [2, '3']], entry: '<b>'}"])
def test_btc_chart_malformed_data_is_text_only(javascript, btc):
    javascript(CHARTS + "chartData.btc = " + btc + ";" + r'''
run('applyStatus(fixture)');
context.chartData = chartData;
run('renderCharts(chartData)');
assert.equal(element("btc-chart").children[0].tagName, undefined);  // a muted <p>, not an <svg>
assert.doesNotMatch(element("btc-levels").textContent + element("btc-last").textContent, /<|NaN/);
''')


def test_charts_poll_every_minute_after_login_and_refresh_after_mode_change(javascript):
    javascript(CHARTS + r'''
run('pollCharts()');
assert.equal(timerDelays.at(-1), 3000);
assert.equal(requests.length, 0);                             // not logged in yet: no request
run('applyStatus(fixture); pollCharts()');
assert.equal(timerDelays.at(-1), 60000);
assert.deepEqual(requests.map(r => r.url), ["/api/charts"]);
assert.equal(requests[0].options.method, "GET");
await new Promise(resolve => setImmediate(resolve));
run('state.charts = {mode: "dry"}; fixture.mode = "live"; fixture.freqtrade.dry_run = false; applyStatus(fixture)');
assert.equal(timerDelays.at(-1), 0);
''')


def test_chart_failure_shows_message_without_details(javascript):
    javascript(r'''
run('applyStatus(fixture)');
response={ok:false,status:503,json:async()=>({detail:"요청 실패"}),headers:{get:()=>null}};
await run('refreshCharts()');
assert.match(element("asset-change").textContent, /그래프를 불러오지 못했습니다/);
assert.equal(run('state.chartsPending'), false);
''')


def test_first_chart_failure_never_leaves_the_card_checking(javascript):
    javascript(r'''
run('applyStatus(fixture)');
for (const id of ["asset-main", "btc-last"]) element(id).textContent = "확인 중";
response={ok:false,status:503,json:async()=>({detail:"요청 실패"}),headers:{get:()=>null}};
await run('refreshCharts()');
assert.equal(element("asset-main").textContent, "조회 불가");
assert.equal(element("btc-last").textContent, "조회 불가");
assert.match(element("asset-chart").children[0].textContent, /자산을 조회하지 못했습니다/);
assert.match(element("btc-chart").children[0].textContent, /가격을 받지 못했습니다/);
assert.match(element("asset-change").textContent, /그래프를 불러오지 못했습니다/);
''')


def test_later_chart_failure_keeps_the_last_good_charts(javascript):
    javascript(CHARTS + r'''
run('applyStatus(fixture)');
context.chartData = chartData;
run('renderCharts(chartData)');
response={ok:false,status:503,json:async()=>({detail:"요청 실패"}),headers:{get:()=>null}};
await run('refreshCharts()');
assert.equal(element("asset-main").textContent, "159,950원");
assert.equal(element("btc-last").textContent, "$60,071");
''')


def test_entry_after_the_newest_close_is_marked_on_the_last_point(javascript):
    javascript(CHARTS + r'''
run('applyStatus(fixture)');
chartData.btc.entries = [start + 71 * hour + hour + 5000];   // the trade open right now
context.chartData = chartData;
run('renderCharts(chartData)');
const nodes = svgNodes("btc-chart");
const [marker] = nodes.filter(n => n.tagName === "circle");
const points = nodes.find(n => n.tagName === "polyline").attributes.points.split(" ");
assert.equal(`${marker.attributes.cx},${marker.attributes.cy}`, points.at(-1));
''')


POSITION_ROWS = r'''
const rows = () => Object.fromEntries(element("positions").children[0].children[1].children
    .map(row => [row.children[0].textContent, row.children[1].textContent]));
const live = {...status, mode: "live",
    positions: [{symbol: "BTCUSDT", side: "long", size: 0.0003, entry: 83472.1, mark: 83529.9, liq: 42000, leverage: 2}],
    stops: [{plan_type: "pos_loss", trigger_price: 81920.3, trigger_type: "mark_price"}],
    exit_plans: {BTCUSDT: {exit_line: 81163, trail_distance: 2327.7, bot_stop: 81920.3}}};
'''


def test_position_card_shows_dollar_value_margin_and_stop_outcome(javascript):
    javascript(POSITION_ROWS + r'''
run("state.charts = {asset: {krw_per_usdt: 1400}}");
run("renderPositions")(live);
const shown = rows();
assert.equal(shown["수량"], "0.0003");
assert.equal(shown["포지션 금액"], "$25.06 (약 35,083원)");
assert.equal(shown["증거금 (묶인 돈)"], "$12.52 (약 17,529원)");
assert.match(shown["평가손익 (수수료 제외)"], /^\+\$0\.02 \(약 \+24원\) · 증거금 대비 \+0\.14%$/);
assert.equal(shown["강제청산가"], "$42,000");
assert.equal(shown["손절 시 손익 (수수료 제외)"], "-$0.47 (약 -652원)");
assert.equal(shown["익절가"], "고정 익절 없음 · 가격이 오르면 손절선을 따라 올립니다");
assert.equal(shown["청산 신호선"], "다음 4시간봉 종가가 $81,163 아래면 청산");
assert.equal(shown["본전 전환"], "고점이 약 $85,800를 넘으면 손절선이 진입가 위로 올라갑니다");
''')


def test_position_card_without_rate_shows_dollars_only_and_uses_lowest_exchange_stop(javascript):
    javascript(POSITION_ROWS + r'''
run("state.charts = null");
run("renderPositions")({...live, stops: [{trigger_price: 82500}, {trigger_price: 81000}]});
const shown = rows();
assert.equal(shown["포지션 금액"], "$25.06");
assert.equal(shown["손절 시 손익 (수수료 제외)"], "-$0.74 · 손절 주문 2개 중 가장 불리한 값");
''')


def test_position_card_dry_mode_uses_bot_stop_and_reports_breakeven_reached(javascript):
    javascript(POSITION_ROWS + r'''
run("state.charts = null");
run("renderPositions")({...live, mode: "dry", stops: null,
    exit_plans: {BTCUSDT: {exit_line: null, trail_distance: 2327.7, bot_stop: 84000}}});
const shown = rows();
assert.equal(shown["손절 시 손익 (수수료 제외)"], "+$0.16");
assert.equal(shown["본전 전환"], "손절선이 이미 진입가 이상입니다");
assert.equal(shown["청산 신호선"], undefined);
''')


def test_position_card_hides_strategy_exit_rows_for_manual_trades(javascript):
    javascript(POSITION_ROWS + r'''
run("state.charts = null");
run("renderPositions")({...live, exit_plans: {}});
const shown = rows();
assert.equal(shown["익절가"], undefined);
assert.equal(shown["손절 시 손익 (수수료 제외)"], "-$0.47");
''')


def test_emergency_close_sits_at_the_page_bottom_not_pinned_over_content():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    rule = css.split(".emergency {", 1)[1].split("}", 1)[0]
    assert "position: fixed" not in rule and "position: sticky" not in rule
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert html.index('<aside class="emergency"') > html.index('id="phone-title"')


def test_position_card_prefers_exchange_margin_and_judges_breakeven_on_the_exchange_stop(javascript):
    javascript(POSITION_ROWS + r"""
run("state.charts = null");
// Live: the bot already raised its stop above entry but the exchange stop is still below it.
run("renderPositions")({...live, positions: [{...live.positions[0], margin: 22.52}],
    exit_plans: {BTCUSDT: {exit_line: 81163, trail_distance: 2327.7, bot_stop: 83500}}});
const shown = rows();
assert.equal(shown["증거금 (묶인 돈)"], "$22.52");
assert.match(shown["평가손익 (수수료 제외)"], /증거금 대비 \+0\.08%$/);
assert.equal(shown["본전 전환"], "고점이 약 $85,800를 넘으면 손절선이 진입가 위로 올라갑니다");
""")


def test_topbar_links_to_the_emergency_block():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    topbar = html.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]
    assert '<a class="emergency-jump" href="#emergency-title">긴급 청산 ↓</a>' in topbar
    assert 'id="emergency-title"' in html



TRADE_FIXTURE = r'''
const trade = {trade_id: 2, symbol: "BTCUSDT", side: "long", amount: 0.0003, current_rate: 83500,
               realized_profit: 0, kind: "strategy", has_open_orders: false, open_exit_price: null};
const withTrade = (extra = {}) => ({...status, mode: "live",
    freqtrade: {...status.freqtrade, dry_run: false}, trades: [{...trade, ...extra}]});
const posts = () => requests.filter(r => r.options?.method === "POST").map(r => [r.url, JSON.parse(r.options.body)]);
'''


def test_trade_controls_show_for_a_bot_trade_only(javascript):
    javascript(TRADE_FIXTURE + r'''
run("applyStatus")(withTrade());
assert.equal(element("trade-controls").hidden, false);
assert.equal(element("trade-summary").textContent, "거래 #2 · BTCUSDT 롱 0.0003 · 현재 $83,500");
assert.equal(element("close-market").disabled, false);
assert.equal(element("close-limit").disabled, false);
assert.equal(element("cancel-order").hidden, true);
run("applyStatus")(withTrade({kind: "smoke"}));
assert.equal(element("trade-controls").hidden, true);
run("applyStatus")(withTrade({has_open_orders: true, open_exit_price: 86000}));
assert.equal(element("close-market").disabled, false);
assert.equal(element("cancel-order").hidden, false);
assert.equal(element("open-order").textContent, "걸려 있는 지정가 종료 주문: $86,000");
''')


def test_market_close_goes_through_confirmation_then_one_post(javascript):
    javascript(TRADE_FIXTURE + r'''
run("applyStatus")(withTrade());
run('openConfirmation({kind: "trade", value: "market", tradeId: 2, live: false})');
assert.equal(element("confirmation").open, true);
assert.equal(element("confirm-title").textContent, "시장가 종료");
assert.match(element("confirm-description").textContent, /되돌릴 수 없습니다/);
assert.equal(element("live-field").hidden, true);
assert.equal(element("confirm-action").disabled, false);
await run("confirmAction()");
assert.deepEqual(posts(), [["/api/trades/2/close", {type: "market"}]]);
''')


def test_limit_close_above_the_price_is_a_take_profit_order(javascript):
    javascript(TRADE_FIXTURE + r'''
run("applyStatus")(withTrade());
const submit = () => run("requestLimitClose")({preventDefault() {}});
element("limit-price").value = "200000";
submit();
assert.equal(element("confirmation").open, false);
assert.match(element("notice").textContent, /50% 안쪽/);
element("limit-price").value = "86000";
submit();
assert.equal(element("confirmation").open, true);
assert.match(element("confirm-description").textContent, /\$86,000 지정가로 닫는 주문을 겁니다.*손절과 손절선 끌어올리기는 그대로/);
assert.doesNotMatch(element("confirm-description").textContent, /바로 체결/);
await run("confirmAction()");
assert.deepEqual(posts(), [["/api/trades/2/close", {type: "limit", price: 86000}]]);
''')


def test_limit_close_at_or_below_the_price_warns_it_fills_now(javascript):
    javascript(TRADE_FIXTURE + r'''
run("applyStatus")(withTrade());
element("limit-price").value = "83400";
run("requestLimitClose")({preventDefault() {}});
assert.match(element("confirm-description").textContent, /바로 체결될 수 있습니다/);
''')


def test_cancel_order_posts_for_the_trade(javascript):
    javascript(TRADE_FIXTURE + r'''
run("applyStatus")(withTrade({has_open_orders: true, open_exit_price: 86000}));
const handler = run("controlledTrade") && true;
await run(`command("/api/trades/2/cancel-order", {})`);
assert.deepEqual(posts(), [["/api/trades/2/cancel-order", {}]]);
''')


def test_position_card_shows_a_resting_limit_close_as_the_take_profit(javascript):
    javascript(POSITION_ROWS + r'''
run("state.charts = null");
run("renderPositions")({...live, trades: [{trade_id: 2, symbol: "BTCUSDT", kind: "strategy", open_exit_price: 86000, realized_profit: -0.01}]});
const shown = rows();
assert.equal(shown["익절가 (지정가 주문)"], "$86,000 · 비트겟에 걸린 지정가 종료 주문");
assert.equal(shown["익절가"], undefined);
assert.equal(shown["실현손익"], "-$0.01");
assert.equal(shown["청산 신호선"], "다음 4시간봉 종가가 $81,163 아래면 청산");
''')
