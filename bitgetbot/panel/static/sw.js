"use strict";

const CACHE_PREFIX = "bitget-panel-shell-";
const CACHE = `${CACHE_PREFIX}v1`;
const SHELL = [
    "/", "/static/index.html", "/static/style.css", "/static/app.js",
    "/manifest.webmanifest", "/static/icon-192.png", "/static/icon-512.png"
];

self.addEventListener("install", event => {
    event.waitUntil((async () => {
        const cache = await caches.open(CACHE);
        // Never include session cookies or launcher tokens in shell prefetches.
        await cache.addAll(SHELL.map(path => new Request(path, {credentials: "omit", cache: "reload"})));
    })());
});

self.addEventListener("activate", event => {
    event.waitUntil((async () => {
        const names = await caches.keys();
        await Promise.all(names.filter(name => name.startsWith(CACHE_PREFIX) && name !== CACHE)
            .map(name => caches.delete(name)));
        await self.clients.claim();
    })());
});

self.addEventListener("fetch", event => {
    const request = event.request;
    const url = new URL(request.url);
    // Leave all API traffic entirely to the network, including errors and POSTs.
    if (url.pathname === "/api" || url.pathname.startsWith("/api/")) return;
    if (request.method !== "GET" || url.origin !== self.location.origin || url.search) return;
    if (!SHELL.includes(url.pathname)) return;
    event.respondWith((async () => {
        const cache = await caches.open(CACHE);
        if (url.pathname !== "/") {
            const cached = await cache.match(url.pathname);
            if (cached) return cached;
        }
        // Network first for the entry page. Never retain redirects or token URLs.
        try {
            const response = await fetch(request);
            if (response.ok && !response.redirected) await cache.put(url.pathname, response.clone());
            return response;
        } catch (error) {
            const cached = await cache.match(url.pathname);
            if (cached) return cached;
            throw error;
        }
    })());
});
