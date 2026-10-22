"use strict";

// Read-only post-mortem viewer. Report text comes from files the watcher wrote with model output in
// them, so it is only ever placed with textContent; links are built element by element (http/https).

const KEY = /^(dry|live)-[0-9]{1,12}-[0-9]{1,16}$/;
const STATUS = {complete: "완료", pending: "조사 대기", incomplete: "사실만 기록"};
const DRIVER = {rule: "규칙", event: "사건", mixed: "혼합", unclear: "불명"};

const $ = id => document.getElementById(id);

async function getJson(path) {
    const response = await fetch(path, {credentials: "same-origin", cache: "no-store"});
    if (response.status === 401) throw new Error("login");
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
}

function showStatus(text) {
    const node = $("reports-status");
    node.textContent = text;
    node.hidden = !text;
}

function money(value, digits) {
    return typeof value === "number" && isFinite(value) ? (value >= 0 ? "+" : "") + value.toFixed(digits) : "?";
}

function rowText(row) {
    const side = row.side === "short" ? "숏" : "롱";
    const mode = row.mode === "live" ? "실전" : "모의";
    const pct = typeof row.pnl_pct_margin === "number" ? ` (${money(row.pnl_pct_margin, 2)}%)` : "";
    const when = typeof row.close_kst === "string" ? row.close_kst.slice(5) : "?";
    return [
        `${when} · #${row.trade_id} ${row.symbol || row.pair || ""} ${side} · ${mode}`,
        `${money(row.pnl_usdt, 2)} USDT${pct}`,
        `${row.exit_label_ko || row.exit_reason || ""} · 판정 ${DRIVER[row.driver] || "?"} · ${STATUS[row.status] || row.status}`,
    ];
}

function renderList(rows) {
    const list = $("report-list");
    list.replaceChildren();
    for (const row of rows) {
        if (!row || !KEY.test(row.key)) continue;
        const [head, pnl, tail] = rowText(row);
        const button = document.createElement("button");
        button.type = "button";
        const amount = document.createElement("strong");
        amount.textContent = pnl;
        amount.className = typeof row.pnl_usdt === "number" && row.pnl_usdt < 0 ? "down" : "up";
        button.append(`${head} · `, amount, ` · ${tail}`);
        button.addEventListener("click", () => { location.hash = row.key; });
        const item = document.createElement("li");
        item.append(button);
        list.append(item);
    }
    showStatus(list.children.length ? "" : "아직 리포트가 없습니다. 거래가 청산되면 몇 분 안에 생깁니다.");
}

function inline(parent, text) {
    // `code` spans and bare http(s) URLs; everything else is plain text.
    const parts = text.split(/(`[^`]*`|https?:\/\/[^\s)]+)/);
    for (const part of parts) {
        if (!part) continue;
        if (part.startsWith("`") && part.endsWith("`") && part.length > 1) {
            const code = document.createElement("code");
            code.textContent = part.slice(1, -1);
            parent.append(code);
        } else if (/^https?:\/\//.test(part)) {
            const link = document.createElement("a");
            link.href = part;
            link.textContent = part;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            parent.append(link);
        } else {
            parent.append(part);
        }
    }
}

function renderMarkdown(markdown) {
    const view = $("report-view");
    view.replaceChildren();
    let list = null;
    for (const line of String(markdown).split(/\r?\n/)) {
        if (line.startsWith("- ")) {
            if (!list) { list = document.createElement("ul"); view.append(list); }
            const item = document.createElement("li");
            inline(item, line.slice(2));
            list.append(item);
            continue;
        }
        list = null;
        if (!line.trim()) continue;
        const heading = line.startsWith("## ") ? "h3" : line.startsWith("# ") ? "h2" : null;
        const node = document.createElement(heading || "p");
        inline(node, heading ? line.replace(/^#+ /, "") : line);
        view.append(node);
    }
}

async function showReport(key) {
    $("list-card").hidden = true;
    $("view-card").hidden = false;
    showStatus("불러오는 중입니다.");
    try {
        const data = await getJson(`/api/reports/${encodeURIComponent(key)}`);
        renderMarkdown(data.markdown || "");
        showStatus("");
    } catch (error) {
        $("report-view").replaceChildren();
        showStatus(error.message === "login" ? "로그인이 필요합니다. 제어판에서 로그인한 뒤 다시 여세요."
            : "리포트를 불러오지 못했습니다.");
    }
}

async function showList() {
    $("view-card").hidden = true;
    $("list-card").hidden = false;
    showStatus("불러오는 중입니다.");
    try {
        const data = await getJson("/api/reports");
        renderList(Array.isArray(data.reports) ? data.reports : []);
    } catch (error) {
        showStatus(error.message === "login" ? "로그인이 필요합니다. 제어판에서 로그인한 뒤 다시 여세요."
            : "리포트 목록을 불러오지 못했습니다.");
    }
}

function route() {
    const key = decodeURIComponent(location.hash.slice(1));
    if (KEY.test(key)) showReport(key); else showList();
}

$("back-to-list").addEventListener("click", event => {
    event.preventDefault();
    history.pushState(null, "", location.pathname);
    showList();
});
window.addEventListener("hashchange", route);
route();
