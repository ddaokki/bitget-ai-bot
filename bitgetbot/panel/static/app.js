"use strict";

const STEP_NAMES = {
    preflight: "사전 점검", enter: "진입", check: "손절 확인",
    "restart-check": "재시작 확인", exit: "청산", ledger: "장부 저장"
};
const state = {
    status: null, job: null, authenticated: false, online: false, needsLogin: false,
    pending: false, emergencyPending: false, lastStatusAt: 0, epoch: 0,
    preflightMode: null, preflightJobStart: null, pendingPreflight: null,
    confirmation: null, emergencyDeadline: 0, loginDeadline: 0,
    pollTimer: null, polling: false,
    charts: null, chartsPending: false, chartsTimer: null
};
const SVG_NS = "http://www.w3.org/2000/svg";
const CHART = {width: 600, height: 200, pad: 10};
const $ = id => document.getElementById(id);
const text = (id, value) => { $(id).textContent = value; };

function notify(message, error = false) {
    text("notice", message);
    $("notice").className = error ? "notice error" : "notice";
    $("notice").hidden = false;
}

function clearSecrets() {
    document.querySelectorAll('input[type="password"]').forEach(input => { input.value = ""; });
}

function resetPreflight() {
    state.preflightMode = null;
    state.preflightJobStart = null;
    state.pendingPreflight = null;
}

function isLocal() {
    if (state.status) return state.status.session_kind === "local";
    return ["127.0.0.1", "localhost", "[::1]"].includes(location.hostname);
}

function sessionLost() {
    state.authenticated = false;
    state.online = false;
    state.needsLogin = true;
    resetPreflight();
    closeConfirmation();
    disarmEmergency();
    clearSecrets();
    $("panel").hidden = true;
    $("local-settings").hidden = true;
    $("auth-screen").hidden = isLocal();
    text("connection", isLocal() ? "바탕화면 바로가기로 다시 열어 주세요" : "PIN으로 로그인해 주세요.");
    text("mode-badge", "로그인 필요");
    $("mode-badge").className = "mode-badge unknown";
    updateControls();
}

function disconnected() {
    state.online = false;
    resetPreflight();
    closeConfirmation();
    text("connection", "연결 끊김 · 표시된 값은 이전 조회 결과입니다. 일반 주문은 잠겼지만 긴급 청산은 요청할 수 있습니다.");
    $("connection").className = "connection offline";
    updateControls();
}

async function request(path, body) {
    const post = body !== undefined;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), post ? 150000 : 60000);
    try {
        const response = await fetch(path, {
            method: post ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
            redirect: "error", signal: controller.signal,
            headers: post ? {"Content-Type": "application/json", "X-Panel-Request": "1"} : {},
            ...(post ? {body: JSON.stringify(body)} : {})
        });
        const data = await response.json();
        if (!response.ok) {
            if (response.status === 401 && path !== "/api/login") sessionLost();
            const message = typeof data.detail === "string" ? data.detail : data.message;
            const error = new Error(typeof message === "string" ? message : "요청을 처리하지 못했습니다.");
            error.status = response.status;
            error.retryAfter = Number(data.retry_after || response.headers.get("Retry-After") || 0);
            throw error;
        }
        return data;
    } catch (error) {
        if (error.status) throw error;
        disconnected();
        // A timeout is ambiguous: never repeat a POST, especially a market exit.
        throw new Error(post
            ? "요청 결과를 확인하지 못했습니다. 자동 재시도하지 않습니다. 상태와 비트겟 웹을 직접 확인하세요."
            : "서버에 연결할 수 없습니다. 연결 상태를 확인하세요.");
    } finally {
        clearTimeout(timeout);
    }
}

function canAct() {
    return state.authenticated && state.online && !state.emergencyPending && state.status !== null
        && Date.now() - state.lastStatusAt < 15000;
}

function stepAllowed(name) {
    if (!canAct() || state.pending || state.job?.running) return false;
    const status = state.status;
    if (!["dry", "live"].includes(status.mode)) return false;
    const smoke = status.smoke_state;
    if (!smoke || typeof smoke.exists !== "boolean") return false;
    if (name === "preflight") return !smoke.exists;
    if (name === "enter") {
        return !smoke.exists && state.preflightMode === status.mode
            && status.freqtrade?.reachable === true
            && status.freqtrade?.dry_run === (status.mode === "dry");
    }
    if (["check", "restart-check", "exit"].includes(name)) return smoke.exists;
    return name === "ledger" && smoke.exists && status.mode === "live"
        && smoke.mode === "live" && smoke.close_confirmed === true;
}

function autotradeAllowed(value) {
    if (!canAct() || state.pending || state.job?.running) return false;
    const status = state.status;
    const ft = status.freqtrade;
    if (!["dry", "live"].includes(status.mode) || ft?.reachable !== true
        || ft?.dry_run !== (status.mode === "dry")) return false;
    // Start from stopped or paused; pause only a running bot.
    if (value === "start") return ["stopped", "paused"].includes(ft.state);
    return value === "pause" && ft.state === "running";
}

function actionAllowed(action) {
    if (!action || !canAct() || state.pending || state.job?.running) return false;
    if (action.fromMode !== state.status.mode) return false;
    if (action.kind === "step") return stepAllowed(action.value);
    if (action.kind === "autotrade") return autotradeAllowed(action.value);
    // A mode switch would hide an open smoke trade from status and emergency close.
    return action.kind === "mode" && ["dry", "live"].includes(state.status.mode)
        && state.status.smoke_state?.exists === false
        && ["dry", "live"].includes(action.value) && action.value !== state.status.mode;
}

function updateControls() {
    document.querySelectorAll("[data-step]").forEach(button => {
        button.disabled = !stepAllowed(button.dataset.step);
    });
    const busy = !canAct() || state.pending || state.job?.running === true;
    $("mode-switch").disabled = busy || !["dry", "live"].includes(state.status?.mode)
        || state.status?.smoke_state?.exists !== false;
    // Only needed when the container (or Docker) is down; the launcher starts it otherwise.
    $("docker-up").hidden = state.status?.docker_running !== false && state.status?.freqtrade?.reachable === true;
    $("docker-up").disabled = busy;
    $("autotrade-start").disabled = !autotradeAllowed("start");
    $("autotrade-pause").disabled = !autotradeAllowed("pause");
    const localBusy = busy || state.status?.session_kind !== "local";
    for (const id of ["save-keys", "save-pin"]) $(id).disabled = localBusy;
    const tailscale = state.status?.tailscale;
    $("phone-enable").disabled = localBusy || !state.status?.pin_set
        || tailscale?.installed !== true || tailscale?.logged_in !== true || tailscale?.funnel !== false;
    // Emergency close remains available while a normal job runs or Freqtrade is down.
    // A failed status lookup is not a logout; the server authorizes each emergency POST.
    $("emergency-arm").disabled = state.needsLogin || state.emergencyPending;
    $("emergency-confirm").disabled = $("emergency-arm").disabled || Date.now() >= state.emergencyDeadline;
    updateConfirmation();
}

function number(value, dollar = false) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "조회 불가";
    const formatted = new Intl.NumberFormat("ko-KR", {maximumFractionDigits: 8}).format(value);
    return dollar ? `$${formatted}` : formatted;
}

function node(tag, content, className) {
    const element = document.createElement(tag);
    if (content !== undefined) element.textContent = content;
    if (className) element.className = className;
    return element;
}

function stopSummary(stops) {
    if (!Array.isArray(stops)) return "조회 불가";
    return stops.length ? stops.map(stop => number(stop.trigger_price, true)).join(" · ") : "손절 주문 없음";
}

const direction = position => position.side === "long" ? 1 : position.side === "short" ? -1 : null;
const positionValue = position => finite(position.size) && finite(position.mark) ? position.size * position.mark : null;
const positionMargin = position => finite(position.size) && finite(position.entry) && finite(position.leverage) && position.leverage > 0
    ? position.size * position.entry / position.leverage : null;

function krwRate() {
    const rate = state.charts?.asset?.krw_per_usdt;
    return finite(rate) && rate > 0 ? rate : null;
}

// USD with the won value beside it when the Upbit rate is known.
function money(value) {
    if (!finite(value)) return "조회 불가";
    const rate = krwRate();
    return rate === null ? usd(value) : `${usd(value)} (약 ${krw(value * rate)})`;
}

function signedMoney(value) {
    if (!finite(value)) return "조회 불가";
    const rate = krwRate();
    return rate === null ? signed(value, usd) : `${signed(value, usd)} (약 ${signed(value * rate, krw)})`;
}

function unrealized(position) {
    const sign = direction(position);
    if (sign === null || !finite(position.size) || !finite(position.entry) || !finite(position.mark)) return "조회 불가";
    const pnl = (position.mark - position.entry) * position.size * sign;
    const margin = positionMargin(position);
    if (!finite(margin) || margin <= 0) return signedMoney(pnl);
    const percent = new Intl.NumberFormat("ko-KR", {maximumFractionDigits: 2, signDisplay: "exceptZero"}).format(pnl / margin * 100);
    return `${signedMoney(pnl)} · 증거금 대비 ${percent}%`;
}

// The stop that closes the position: the exchange stop in live mode (the one furthest from the
// price, as the widget does), the bot's own stop in dry mode.
function protectiveStop(position, status) {
    const sign = direction(position);
    if (sign === null) return null;
    if (status.mode === "dry") return status.exit_plans?.[position.symbol]?.bot_stop ?? null;
    if (position.symbol !== "BTCUSDT" || !Array.isArray(status.stops)) return null;
    const triggers = status.stops.map(stop => stop.trigger_price).filter(price => finite(price) && price > 0);
    if (!triggers.length) return null;
    return sign > 0 ? Math.min(...triggers) : Math.max(...triggers);
}

function stopOutcome(position, stop) {
    const sign = direction(position);
    if (sign === null || !finite(stop) || !finite(position.size) || !finite(position.entry)) return null;
    return (stop - position.entry) * position.size * sign;
}

// The strategy sets no take-profit price: it trails the stop up and exits on a 4h close below
// the prior 10-candle low. Show those two levels instead of an invented target.
function exitPlanRows(position, plan) {
    if (!plan || position.side !== "long") return [];
    const rows = [["익절가", "고정 익절 없음 · 가격이 오르면 손절선을 따라 올립니다"]];
    if (finite(plan.exit_line)) rows.push(["청산 신호선", `4시간봉 종가가 ${number(plan.exit_line, true)} 아래로 마감하면 청산`]);
    if (finite(plan.bot_stop) && finite(position.entry) && plan.bot_stop >= position.entry) {
        rows.push(["본전 전환", "손절선이 이미 진입가 이상입니다"]);
    } else if (finite(plan.trail_distance) && finite(position.entry)) {
        rows.push(["본전 전환", `고점이 약 ${number(Math.round(position.entry + plan.trail_distance), true)}를 넘으면 손절선이 진입가 위로 올라갑니다`]);
    }
    return rows;
}

function renderPositions(status) {
    const target = $("positions");
    target.replaceChildren();
    if (!Array.isArray(status.positions)) {
        target.append(node("p", "포지션 조회 실패 · 포지션이 없다는 뜻이 아닙니다.", "muted"));
    } else if (!status.positions.length) {
        target.append(node("p", "열린 포지션이 없습니다.", "muted"));
    } else {
        for (const position of status.positions) {
            const card = node("article", undefined, "position");
            const heading = node("h3", position.symbol || "코인 확인 불가");
            const side = position.side === "long" ? "매수 · 롱" : position.side === "short" ? "매도 · 숏" : "방향 확인 불가";
            heading.append(node("span", side, `side ${position.side === "long" ? "long" : position.side === "short" ? "short" : ""}`));
            card.append(heading);
            const values = node("dl");
            const rows = [
                ["수량", number(position.size)],
                ["포지션 금액", money(positionValue(position))],
                ["증거금 (묶인 돈)", money(positionMargin(position))],
                ["레버리지", typeof position.leverage === "number" ? `${number(position.leverage)}배` : "조회 불가"],
                ["진입가", number(position.entry, true)], ["현재가", number(position.mark, true)],
                ["평가손익", unrealized(position)],
                ["강제청산가", number(position.liq, true)],
                ["손절가", position.symbol === "BTCUSDT" ? (status.mode === "dry" ? "모의 모드 · 거래소 조회 안 함" : stopSummary(status.stops)) : "조회 대상 아님 · BTCUSDT만 지원"],
                ["손절 시 손익", signedMoney(stopOutcome(position, protectiveStop(position, status)))],
                ...exitPlanRows(position, status.exit_plans?.[position.symbol])
            ];
            for (const [label, value] of rows) {
                const row = node("div");
                row.append(node("dt", label), node("dd", value));
                values.append(row);
            }
            card.append(values);
            target.append(card);
        }
    }
    const stops = $("stops");
    stops.replaceChildren();
    if (status.mode === "dry" || !Array.isArray(status.stops) || !status.stops.length) {
        stops.append(node("p", status.mode === "dry" ? "모의 모드에서는 거래소 손절 주문을 조회하지 않습니다." : stopSummary(status.stops), "muted"));
    } else {
        for (const stop of status.stops) {
            const plan = {pos_loss: "포지션 손절", loss_plan: "예약 손절"}[stop.plan_type] || "유형 확인 불가";
            const trigger = {mark_price: "시장 평균가 기준", fill_price: "최근 체결가 기준"}[stop.trigger_type] || "기준 확인 불가";
            stops.append(node("p", `${number(stop.trigger_price, true)} · ${plan} · ${trigger}`, "stop-row"));
        }
    }
}

const isCount = value => Number.isInteger(value) && value >= 0;
const isRatio = value => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;

function percent(ratio) {
    return `${new Intl.NumberFormat("ko-KR", {maximumFractionDigits: 0}).format(ratio * 100)}%`;
}

function renderStats(status) {
    // Stats come from the running Freqtrade database, so label them by its mode.
    const dryRun = status.freqtrade?.dry_run;
    const source = dryRun === true ? "dry" : dryRun === false ? "live" : "";
    text("stats-mode", source === "dry" ? "모의" : source === "live" ? "실전" : "모드 불명");
    $("stats-mode").className = `stats-mode ${source}`.trim();
    text("stats-detail", "");
    text("stats-other", "");
    const stats = status.stats;
    if (!stats || typeof stats !== "object" || !isCount(stats.closed_trades)) { text("stats-main", "–"); return; }
    // Manual and smoke-test trades never count as strategy wins or losses.
    const others = isCount(stats.other_trades) ? stats.other_trades : 0;
    if (others) text("stats-other", `수동·점검 거래 ${others}건 별도 (승률 제외)`);
    if (stats.closed_trades === 0) { text("stats-main", others ? "아직 전략 거래가 없습니다" : "아직 닫힌 거래가 없습니다"); return; }
    const record = isCount(stats.wins) && isCount(stats.losses) ? `${stats.wins}승 ${stats.losses}패 · ` : "";
    text("stats-main", `승률 ${isRatio(stats.win_rate) ? percent(stats.win_rate) : "–"} (${record}${stats.closed_trades}건)`);
    const recent = stats.closed_trades_30d === 0 ? "거래 없음"
        : isCount(stats.closed_trades_30d) && isRatio(stats.win_rate_30d) ? percent(stats.win_rate_30d) : "–";
    const average = typeof stats.avg_profit_pct === "number" && Number.isFinite(stats.avg_profit_pct)
        ? `${new Intl.NumberFormat("ko-KR", {maximumFractionDigits: 2, signDisplay: "exceptZero"}).format(stats.avg_profit_pct)}%`
        : "–";
    text("stats-detail", `최근 30일 ${recent} · 평균 ${average}${stats.partial === true ? " · 최근 500건 기준" : ""}`);
}

const finite = value => typeof value === "number" && Number.isFinite(value);

function krw(value) {
    return `${new Intl.NumberFormat("ko-KR", {maximumFractionDigits: 0}).format(value)}원`;
}

function usd(value, digits = 2) {
    return `$${new Intl.NumberFormat("en-US", {minimumFractionDigits: digits, maximumFractionDigits: digits}).format(value)}`;
}

function signed(value, format) {
    return `${value > 0 ? "+" : value < 0 ? "-" : ""}${format(Math.abs(value))}`;
}

function svg(tag, attributes) {
    const element = document.createElementNS(SVG_NS, tag);
    for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
    return element;
}

// Draw a line chart with DOM-created SVG nodes only (CSP: no inline styles, no HTML strings).
function drawChart(id, points, {levels = [], markers = [], empty = "표시할 데이터가 없습니다."} = {}) {
    const target = $(id);
    target.replaceChildren();
    const valid = (Array.isArray(points) ? points : [])
        .filter(point => Array.isArray(point) && finite(point[0]) && finite(point[1]));
    if (valid.length < 2) {
        target.append(node("p", empty, "muted chart-empty"));
        return false;
    }
    const lines = levels.filter(level => finite(level.value));
    const values = [...valid.map(point => point[1]), ...lines.map(level => level.value)];
    let low = Math.min(...values), high = Math.max(...values);
    const margin = (high - low) * 0.08 || Math.abs(high) * 0.01 || 1;
    low -= margin;
    high += margin;
    const first = valid[0][0], span = valid.at(-1)[0] - first || 1;
    const {width, height, pad} = CHART;
    const x = value => (pad + (value - first) / span * (width - 2 * pad)).toFixed(1);
    const y = value => (pad + (high - value) / (high - low) * (height - 2 * pad)).toFixed(1);
    const root = svg("svg", {viewBox: `0 0 ${width} ${height}`, class: "chart-svg", "aria-hidden": "true", focusable: "false"});
    for (const level of lines) {
        root.append(svg("line", {x1: 0, x2: width, y1: y(level.value), y2: y(level.value),
            class: `chart-level ${level.kind}`, "vector-effect": "non-scaling-stroke"}));
    }
    root.append(svg("polyline", {points: valid.map(point => `${x(point[0])},${y(point[1])}`).join(" "),
        class: "chart-line", fill: "none", "vector-effect": "non-scaling-stroke"}));
    for (const time of markers.filter(finite)) {
        // An entry after the newest close (the trade open right now) is drawn on the last point.
        if (time < first) continue;
        // Mark the close of the candle the entry happened in.
        const near = valid.reduce((best, point) => point[0] <= time ? point : best, valid[0]);
        root.append(svg("circle", {cx: x(near[0]), cy: y(near[1]), r: 9, class: "chart-marker"}));
    }
    target.append(root);
    return true;
}

function renderAsset(asset) {
    const mode = asset?.mode;
    text("asset-mode", mode === "dry" ? "모의" : mode === "live" ? "실전" : "모드 불명");
    $("asset-mode").className = `stats-mode ${["dry", "live"].includes(mode) ? mode : ""}`.trim();
    text("asset-source", mode === "live" ? "비트겟 선물 계정 · 미실현 손익 포함" : mode === "dry" ? "모의 지갑 · 거래 봇 기준" : "");
    const equity = finite(asset?.equity_usdt) ? asset.equity_usdt : null;
    const inKrw = finite(asset?.equity_krw) ? asset.equity_krw : null;
    // KRW large with USD below; USD only when the Upbit rate is unavailable.
    text("asset-main", inKrw !== null ? krw(inKrw) : equity !== null ? usd(equity) : "조회 불가");
    text("asset-sub", inKrw !== null && equity !== null
        ? `${usd(equity)}${finite(asset.krw_per_usdt) ? ` · 1 USDT = ${krw(asset.krw_per_usdt)}` : ""}`
        : equity !== null ? "원화 환율 조회 불가 · 달러만 표시" : "");
    const history = Array.isArray(asset?.history) ? asset.history.filter(point => Array.isArray(point)) : [];
    const points = history.map(([time, value]) => [time * 1000, value]);
    if (equity !== null) points.push([Date.now(), equity]);
    const change = asset?.change_usdt, pct = asset?.change_pct;
    if (finite(change) && finite(asset?.since)) {
        const since = new Date(asset.since * 1000).toLocaleString("ko-KR", {month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"});
        const rate = finite(pct) ? ` (${signed(pct, value => `${new Intl.NumberFormat("ko-KR", {maximumFractionDigits: 2}).format(value)}%`)})` : "";
        text("asset-change", `기록 시작(${since}) 대비 ${signed(change, usd)}${rate}`);
    } else text("asset-change", "자산 기록은 10분마다 한 점씩 쌓입니다.");
    const first = points.find(point => finite(point[1]));
    drawChart("asset-chart", points, {
        levels: first ? [{value: first[1], kind: "base"}] : [],
        empty: asset && typeof asset === "object"
            ? "기록을 모으는 중입니다. 패널을 열어 두면 10분마다 한 점씩 늘어납니다."
            : "자산을 조회하지 못했습니다."
    });
}

function renderBtc(btc) {
    if (!btc || typeof btc !== "object") {
        text("btc-last", "조회 불가");
        text("btc-levels", "거래 봇에 연결되면 표시합니다.");
        drawChart("btc-chart", [], {empty: "거래 봇에서 가격을 받지 못했습니다."});
        return;
    }
    const closes = Array.isArray(btc.closes) ? btc.closes : [];
    const last = closes.at(-1);
    text("btc-last", Array.isArray(last) && finite(last[1]) ? usd(last[1], 0) : "조회 불가");
    const entry = finite(btc.entry) ? btc.entry : null, stop = finite(btc.stop) ? btc.stop : null;
    text("btc-levels", entry !== null
        ? `열린 전략 거래 · 진입 ${usd(entry, 0)} · 손절 ${stop !== null ? usd(stop, 0) : "확인 불가"}`
        : "열린 전략 거래 없음");
    drawChart("btc-chart", closes, {
        levels: [{value: entry, kind: "entry"}, {value: stop, kind: "stop"}],
        markers: Array.isArray(btc.entries) ? btc.entries : []
    });
}

function renderCharts(data) {
    if (!data || typeof data !== "object") throw new Error("그래프 응답을 확인할 수 없습니다.");
    state.charts = data;
    renderAsset(data.asset);
    renderBtc(data.btc);
}

async function refreshCharts() {
    if (!state.authenticated || state.needsLogin || state.chartsPending) return;
    state.chartsPending = true;
    try {
        renderCharts(await request("/api/charts"));
    } catch (error) {
        if (!state.charts) {
            // Never leave the card on "확인 중": show the unavailable states first.
            renderAsset(null);
            renderBtc(null);
        }
        text("asset-change", "그래프를 불러오지 못했습니다. 1분 뒤 다시 시도합니다.");
    } finally {
        state.chartsPending = false;
    }
}

function pollCharts() {
    clearTimeout(state.chartsTimer);
    const ready = state.authenticated && !state.needsLogin;
    if (ready) refreshCharts();
    state.chartsTimer = setTimeout(pollCharts, ready ? 60000 : 3000);
}

function renderSettings(status) {
    const local = status.session_kind === "local";
    $("local-settings").hidden = !local;
    if (!local) { clearSecrets(); return; }
    const keys = status.keys_configured;
    // Saved keys are never sent back, so the inputs stay empty by design; this line says whether they are saved.
    text("keys-state", keys?.key === true && keys?.secret === true && keys?.password === true
        ? "저장됨 · 다시 넣지 않아도 됩니다" : "미설정 · 키를 입력하세요");
    text("pin-state", status.pin_set ? "설정됨" : "미설정");
    const ts = status.tailscale;
    text("ts-installed", ts?.installed === true ? "설치됨" : "설치 필요");
    text("ts-login", ts?.logged_in === true ? "로그인됨" : "로그인 필요");
    text("ts-serving", ts?.serving === true ? "켜짐" : "꺼짐");
    text("ts-funnel", ts?.funnel === true ? "위험 · 켜짐" : ts?.funnel === false ? "꺼짐 · 확인됨" : "확인 불가 · 접속 켜기 차단");
    text("phone-help", ts?.funnel !== false ? "인터넷 전체 공개가 꺼졌는지 확인할 수 없습니다. 공개 설정을 직접 확인하세요."
        : !status.pin_set ? "휴대폰 접속 전에 PIN을 설정하세요." : "인터넷 전체가 아닌 내 테일스케일 기기에만 연결합니다.");
    const address = $("phone-address");
    const safeName = typeof ts?.dns_name === "string"
        && /^[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?\.[a-zA-Z0-9-]+\.ts\.net$/.test(ts.dns_name);
    address.hidden = !(safeName && ts.serving === true && ts.funnel === false);
    address.removeAttribute("href");
    address.textContent = "";
    if (!address.hidden) {
        address.href = `https://${ts.dns_name}`;
        address.textContent = address.href;
    }
}

function acceptJob(job) {
    if (!job || typeof job.running !== "boolean") return;
    state.job = job;
    const pending = state.pendingPreflight;
    if (pending && job.started_at !== pending.previous) {
        if (job.name === "preflight" && pending.mode === state.status?.mode) {
            if (!job.running) {
                state.preflightMode = job.exit_code === 0 ? pending.mode : null;
                state.preflightJobStart = job.exit_code === 0 ? job.started_at : null;
                state.pendingPreflight = null;
            }
        } else resetPreflight();
    } else if (state.preflightMode && (job.name !== "preflight" || job.started_at !== state.preflightJobStart || job.exit_code !== 0)) {
        resetPreflight();
    }
    const name = STEP_NAMES[job.name] || "작업";
    text("job-state", job.running ? `${name} 실행 중` : job.name ? `${name} ${job.exit_code === 0 ? "완료" : "실패"}` : "대기 중");
    const lines = Array.isArray(job.lines) ? job.lines.filter(line => typeof line === "string") : [];
    text("job-lines", lines.length ? lines.join("\n") : job.running ? "작업 결과를 기다리는 중입니다." : "아직 실행한 작업이 없습니다.");
    updateControls();
}

function applyStatus(status) {
    if (!status || !["local", "tailnet"].includes(status.session_kind)) throw new Error("상태 응답을 확인할 수 없습니다.");
    if ((state.status && state.status.mode !== status.mode) || status.smoke_state?.exists === true) resetPreflight();
    // Charts follow the mode: refresh soon after a switch instead of waiting a full minute.
    if (state.charts && state.charts.mode !== status.mode && !state.chartsPending) {
        clearTimeout(state.chartsTimer);
        state.chartsTimer = setTimeout(pollCharts, 0);
    }
    state.status = status;
    state.authenticated = true;
    state.online = true;
    state.needsLogin = false;
    state.lastStatusAt = Date.now();
    $("panel").hidden = false;
    $("auth-screen").hidden = true;
    $("connection").className = "connection";
    text("connection", `연결됨 · ${status.session_kind === "local" ? "이 PC" : "휴대폰"} · 마지막 상태 조회 ${new Date().toLocaleTimeString("ko-KR")}`);
    const mode = status.mode;
    $("mode-badge").className = `mode-badge ${["dry", "live"].includes(mode) ? mode : "unknown"}`;
    text("mode-badge", mode === "dry" ? "모의 거래" : mode === "live" ? "실전 거래" : "모드 확인 불가");
    text("mode-switch", mode === "dry" ? "실전으로 전환" : mode === "live" ? "모의로 전환" : "모드 확인 불가");
    const ft = status.freqtrade;
    text("docker-state", status.docker_running === true ? "실행 중" : "중지 또는 확인 불가");
    const ftNames = {running: "자동매매 중", paused: "새 진입 멈춤", stopped: "중지됨", reload_config: "설정 적용 중"};
    const ftMode = ft?.dry_run === true ? "모의" : ft?.dry_run === false ? "실전" : "모드 불명";
    text("ft-state", ft?.reachable === true ? `${ftNames[ft.state] || "상태 불명"} · ${ftMode}` : "연결 안 됨");
    text("account-kind", mode === "dry" ? "모의 계정" : ({classic: "일반 계정", uta: "통합 계정"}[status.account?.kind] || "확인 불가"));
    text("balance", mode === "dry" ? "모의 · 조회 안 함" : `${number(status.account?.usdt_available)} USDT`);
    text("mode-help", !["dry", "live"].includes(mode) || ft?.dry_run !== (mode === "dry") || ft?.reachable !== true
        ? "거래 봇 모드를 확인할 수 없거나 설정과 다릅니다. 진입을 차단합니다."
        : mode === "live" ? "실제 자금으로 거래합니다. 진입 전 LIVE 확인이 필요합니다." : "모의 거래입니다. 거래소 실전 주문은 내지 않습니다.");
    const smoke = status.smoke_state;
    text("smoke-state", smoke?.exists ? `진행 상태 파일 있음 · ${smoke.close_confirmed ? "청산 확인됨" : "청산 미확인"}` : "진행 상태 파일 없음");
    renderPositions(status);
    renderStats(status);
    renderSettings(status);
    acceptJob(status.job);
    text("step-help", state.job?.running ? "작업 중입니다. 다른 단계와 모드 전환을 잠갔습니다."
        : smoke?.exists ? "진행 중인 거래의 확인·청산 단계를 실행하세요."
        : state.preflightMode === mode ? "이번 화면의 사전 점검이 성공했습니다. 진입할 수 있습니다."
        : "이번 화면에서 사전 점검을 통과해야 진입할 수 있습니다.");
    updateControls();
}

async function refreshStatus() {
    const epoch = state.epoch;
    const status = await request("/api/status");
    if (epoch === state.epoch) applyStatus(status);
}

function schedulePoll() {
    clearTimeout(state.pollTimer);
    if (!state.needsLogin) state.pollTimer = setTimeout(poll, state.job?.running ? 1000 : 5000);
}

async function poll() {
    if (state.polling || state.pending) { schedulePoll(); return; }
    state.polling = true;
    const epoch = state.epoch;
    try {
        if (state.authenticated && state.job?.running) {
            const job = await request("/api/job");
            if (epoch === state.epoch) {
                acceptJob(job);
                if (!job.running) await refreshStatus();
            }
        } else await refreshStatus();
    } catch (error) {
        if (!state.needsLogin) disconnected();
    } finally {
        state.polling = false;
        schedulePoll();
    }
}

async function command(path, body, preflight = false) {
    if (state.pending) return;
    const pending = preflight ? {mode: state.status.mode, previous: state.job?.started_at ?? null} : null;
    resetPreflight();
    state.pending = true;
    state.epoch += 1;
    closeConfirmation();
    disarmEmergency();
    updateControls();
    try {
        const result = await request(path, body);
        state.pendingPreflight = pending;
        notify(typeof result.message === "string" ? result.message : "요청을 접수했습니다.");
        await refreshStatus();
    } catch (error) {
        resetPreflight();
        notify(error.message, true);
        if (error.status === 409) {
            try { await refreshStatus(); } catch { disconnected(); }
        }
    } finally {
        state.pending = false;
        updateControls();
        schedulePoll();
    }
}

function closeConfirmation() {
    state.confirmation = null;
    $("live-confirm").value = "";
    $("confirm-action").disabled = true;
    if ($("confirmation").open) $("confirmation").close();
}

function openConfirmation(action) {
    action = {...action, fromMode: state.status?.mode};
    if (!actionAllowed(action)) return;
    state.confirmation = action;
    $("live-confirm").value = "";
    $("live-field").hidden = !action.live;
    text("confirm-title", action.kind === "mode" ? (action.value === "live" ? "실전 전환 · 자동매매 시작" : "모의 거래로 전환")
        : action.kind === "autotrade" ? (action.live ? "실전 자동매매 켜기" : "모의 자동매매 켜기") : `${STEP_NAMES[action.value]} 확인`);
    // A switch is one step: the bot switches automatic trading on by itself once it is ready.
    text("confirm-description", action.kind === "mode" ? (action.value === "live"
        ? "전환 직후 거래 봇이 다시 시작되고, 준비되는 즉시(약 1~2분) 실제 돈으로 자동매매가 시작됩니다. BTC 롱만, 2배, 거래당 손실 한도 지갑의 0.5%, 하루 최대 3회. 수익을 보장하지 않습니다. 멈추려면 '자동매매 멈추기'를 누르세요."
        : "모의 모드로 바꾸고 거래 봇을 다시 시작합니다. 준비되면 모의 자동매매가 저절로 켜집니다. 실거래 포지션은 비트겟 웹에서 따로 확인하세요.")
        : action.kind === "autotrade" ? "전략이 스스로 진입·청산합니다. BTC 롱만, 2배, 거래당 손실 한도 지갑의 0.5%, 하루 최대 3회. 수익을 보장하지 않습니다."
        : action.value === "enter" ? "새 포지션 진입을 요청합니다. 모드와 사용 자금을 확인하세요." : "현재 거래의 청산을 요청합니다. 체결 결과를 직접 확인하세요.");
    updateConfirmation();
    $("confirmation").showModal();
    $("cancel-action").focus();
}

function updateConfirmation() {
    const action = state.confirmation;
    $("confirm-action").disabled = !actionAllowed(action) || (action.live && $("live-confirm").value !== "LIVE");
}

async function confirmAction() {
    const action = state.confirmation;
    if (!actionAllowed(action) || (action.live && $("live-confirm").value !== "LIVE")) return;
    const body = action.kind === "mode" ? {mode: action.value} : action.kind === "autotrade" ? {action: action.value} : {};
    if (action.live) body.confirm = "LIVE";
    await command(action.kind === "mode" ? "/api/mode" : action.kind === "autotrade" ? "/api/autotrade" : `/api/steps/${action.value}`, body);
}

function runStep(name) {
    if (!stepAllowed(name)) return;
    if ((name === "enter" && state.status.mode === "live") || name === "exit") {
        openConfirmation({kind: "step", value: name, live: name === "enter"});
    } else command(`/api/steps/${name}`, {}, name === "preflight");
}

function disarmEmergency() {
    state.emergencyDeadline = 0;
    $("emergency-confirm").hidden = true;
    $("emergency-confirm").disabled = true;
    text("emergency-arm", "봇 거래 청산 준비");
}

function armEmergency() {
    if (state.needsLogin || state.emergencyPending) return;
    state.emergencyDeadline = Date.now() + 3000;
    text("emergency-arm", "청산 준비됨 · 옆 확인 버튼을 누르세요");
    $("emergency-confirm").hidden = false;
    $("emergency-confirm").disabled = false;
    setTimeout(() => {
        if (Date.now() >= state.emergencyDeadline) disarmEmergency();
    }, 3000);
}

async function confirmEmergency() {
    if (state.needsLogin || state.emergencyPending
        || !state.emergencyDeadline || Date.now() >= state.emergencyDeadline) { disarmEmergency(); return; }
    disarmEmergency();
    state.emergencyPending = true;
    resetPreflight();
    updateControls();
    try {
        const result = await request("/api/emergency/close-all", {});
        const scope = typeof result.message === "string" ? result.message
            : "거래 봇(Freqtrade)이 연 거래를 시장가로 청산 요청 (현재 모드).";
        text("emergency-message", `${scope} 접수는 체결 보장이 아닙니다. 포지션을 직접 확인하세요.`);
        await refreshStatus();
    } catch (error) {
        text("emergency-message", `${error.message} 비트겟 웹(PC)에서 직접 시장가 청산하세요. 자동 재시도하지 않습니다.`);
        notify("청산 결과를 확인할 수 없습니다. 비트겟 웹에서 직접 확인하세요.", true);
    } finally {
        state.emergencyPending = false;
        updateControls();
        schedulePoll();
    }
}

function updateLoginLock() {
    const seconds = Math.max(0, Math.ceil((state.loginDeadline - Date.now()) / 1000));
    $("login-button").disabled = seconds > 0 || state.pending;
    if (seconds > 0) text("login-message", `PIN 입력이 잠겼습니다. ${seconds}초 후 다시 시도하세요.`);
    else if (state.loginDeadline) { state.loginDeadline = 0; text("login-message", "PIN을 다시 입력할 수 있습니다."); }
}

async function login(event) {
    event.preventDefault();
    const body = {pin: $("login-pin").value};
    $("login-pin").value = "";
    if (state.pending || Date.now() < state.loginDeadline) return;
    if (!/^[0-9]{6,12}$/.test(body.pin)) { text("login-message", "PIN은 숫자 6~12자리로 입력하세요."); return; }
    state.pending = true;
    updateLoginLock();
    try {
        await request("/api/login", body);
        state.needsLogin = false;
        text("login-message", "");
        await refreshStatus();
    } catch (error) {
        if (error.status === 429) state.loginDeadline = Date.now() + Math.max(1, error.retryAfter || 900) * 1000;
        text("login-message", error.status === 409 ? "PC에서 PIN을 먼저 설정하세요." : error.message);
    } finally {
        body.pin = "";
        state.pending = false;
        updateLoginLock();
        updateControls();
        schedulePoll();
    }
}

async function saveCredentials(event, kind) {
    event.preventDefault();
    const ids = kind === "keys" ? ["api-key", "api-secret", "api-password"] : ["new-pin"];
    const values = ids.map(id => $(id).value);
    ids.forEach(id => { $(id).value = ""; });
    if (!canAct() || state.pending || state.job?.running || state.status.session_kind !== "local") { values.fill(""); return; }
    const body = kind === "keys" ? {key: values[0], secret: values[1], password: values[2]} : {pin: values[0]};
    values.fill("");
    try { await command(`/api/${kind}`, body); }
    finally { for (const key of Object.keys(body)) body[key] = ""; }
}

function boot() {
    document.querySelectorAll("[data-step]").forEach(button => {
        button.addEventListener("click", () => runStep(button.dataset.step));
    });
    $("mode-switch").addEventListener("click", () => {
        if (!canAct()) return;
        const value = state.status.mode === "dry" ? "live" : "dry";
        openConfirmation({kind: "mode", value, live: value === "live"});
    });
    $("docker-up").addEventListener("click", () => {
        if (canAct() && !state.pending && !state.job?.running) command("/api/docker/up", {});
    });
    $("autotrade-start").addEventListener("click", () => {
        if (autotradeAllowed("start")) openConfirmation({kind: "autotrade", value: "start", live: state.status.mode === "live"});
    });
    $("autotrade-pause").addEventListener("click", () => {
        if (autotradeAllowed("pause")) command("/api/autotrade", {action: "pause"});
    });
    $("login-form").addEventListener("submit", login);
    $("keys-form").addEventListener("submit", event => saveCredentials(event, "keys"));
    $("pin-form").addEventListener("submit", event => saveCredentials(event, "pin"));
    $("phone-enable").addEventListener("click", () => {
        if (!$("phone-enable").disabled) command("/api/phone/enable", {});
    });
    $("cancel-action").addEventListener("click", closeConfirmation);
    $("confirmation").addEventListener("cancel", closeConfirmation);
    $("live-confirm").addEventListener("input", updateConfirmation);
    $("live-confirm").addEventListener("keydown", event => { if (event.key === "Enter") event.preventDefault(); });
    $("confirm-action").addEventListener("click", confirmAction);
    $("emergency-arm").addEventListener("click", armEmergency);
    $("emergency-confirm").addEventListener("click", confirmEmergency);
    for (const id of ["emergency-arm", "emergency-confirm", "confirm-action"]) {
        $(id).addEventListener("keydown", event => { if (event.repeat) event.preventDefault(); });
    }
    window.addEventListener("offline", disconnected);
    window.addEventListener("online", () => { if (!state.needsLogin) poll(); });
    document.addEventListener("visibilitychange", () => {
        if (document.hidden) { closeConfirmation(); disarmEmergency(); clearSecrets(); resetPreflight(); updateControls(); }
        else if (!state.needsLogin) poll();
    });
    setInterval(() => { updateLoginLock(); updateControls(); }, 1000);
    pollCharts();
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {
        notify("홈 화면 설치 준비에 실패했습니다. 브라우저에서는 계속 사용할 수 있습니다.", true);
    });
    poll();
}

document.addEventListener("DOMContentLoaded", boot);
