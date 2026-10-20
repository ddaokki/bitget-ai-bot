import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import MappingProxyType

import pytest

from bitgetbot.panel.auth import (
    Lockout,
    Sessions,
    classify,
    csrf_ok,
    effective_scheme,
    hash_pin,
    identity,
    valid_pin,
    verify_pin,
)


@pytest.mark.parametrize("client, host", [
    ("127.0.0.1", "127.0.0.1:8090"),
    ("::1", "localhost:8090"),
])
def test_classify_local_direct(client, host):
    assert classify(client, host, {}) == "local"


@pytest.mark.parametrize("client, host, headers", [
    ("127.0.0.1", "pc.tail1234.ts.net", {}),
    ("127.0.0.1", "127.0.0.1:8090", {"Tailscale-User-Login": "u@x"}),
    ("::1", "localhost:8090", {"tailscale-user-login": ""}),
    ("100.101.1.2", "127.0.0.1:8090", {}),
    ("192.168.1.2", "localhost:8090", {}),
    ("127.0.0.2", "localhost:8090", {}),
    ("127.0.0.1", "localhost:8091", {}),
    ("127.0.0.1", "localhost:8090.evil.example", {}),
    ("", "", {}),
])
def test_classify_defaults_to_tailnet(client, host, headers):
    assert classify(client, host, MappingProxyType(headers)) == "tailnet"


@pytest.mark.parametrize("headers, expected", [
    ({}, "unknown"),
    ({"Tailscale-User-Login": "u@x"}, "u@x"),
    ({"tAiLsCaLe-UsEr-LoGiN": "other@x"}, "other@x"),
    ({"Tailscale-User-Login": ""}, ""),
])
def test_identity_uses_case_insensitive_header(headers, expected):
    assert identity(MappingProxyType(headers)) == expected


@pytest.mark.parametrize("pin, expected", [
    ("123456", True),
    ("123456789012", True),
    ("12345", False),
    ("1234567890123", False),
    ("12a456", False),
    ("123456\n", False),
    (" 123456", False),
    ("", False),
])
def test_pin_rules(pin, expected):
    assert valid_pin(pin) is expected


def test_pin_hash_roundtrip_uses_required_scrypt_parameters_and_random_salt():
    stored = hash_pin("123456")
    algorithm, salt_hex, digest_hex = stored.split("$")
    assert algorithm == "scrypt"
    assert len(bytes.fromhex(salt_hex)) == 16
    expected = hashlib.scrypt(
        b"123456", salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1,
    )
    assert bytes.fromhex(digest_hex) == expected
    assert verify_pin("123456", stored)
    assert not verify_pin("654321", stored)
    assert hash_pin("123456") != stored


@pytest.mark.parametrize("stored", [
    "", "plaintext", "scrypt$aa", "scrypt$zz$zz", "sha256$aa$bb",
    "scrypt$aa$bb", "scrypt$" + "00" * 16 + "$" + "00" * 64 + "$extra",
])
def test_verify_pin_rejects_malformed_hash(stored):
    assert not verify_pin("123456", stored)


def test_lockout_threshold_identity_isolation_and_exact_expiry():
    now = [0.0]
    lockout = Lockout(clock=lambda: now[0])
    assert lockout.check("a") == 0
    for _ in range(4):
        lockout.fail("a")
        assert lockout.check("a") == 0
    lockout.fail("a")
    assert lockout.check("a") == 900
    assert lockout.check("b") == 0
    now[0] = 899.5
    assert lockout.check("a") == 0.5
    now[0] = 900
    assert lockout.check("a") == 0


def test_lockout_escalates_and_caps():
    now = [0.0]
    lockout = Lockout(clock=lambda: now[0])
    for duration in [900, 1800, 3600, 7200, 14400, 28800, 57600, 86400, 86400]:
        for _ in range(5):
            lockout.fail("a")
        assert lockout.check("a") == duration
        now[0] += duration
        assert lockout.check("a") == 0


def test_failures_during_lockout_do_not_extend_or_escalate_it():
    now = [0.0]
    lockout = Lockout(clock=lambda: now[0])
    for _ in range(5):
        lockout.fail("a")
    now[0] = 100
    for _ in range(10):
        lockout.fail("a")
    assert lockout.check("a") == 800
    now[0] = 900
    for _ in range(5):
        lockout.fail("a")
    assert lockout.check("a") == 1800


def test_success_resets_failures_and_escalation():
    now = [0.0]
    lockout = Lockout(clock=lambda: now[0])
    for _ in range(4):
        lockout.fail("a")
    lockout.success("a")
    lockout.fail("a")
    assert lockout.check("a") == 0
    for _ in range(4):
        lockout.fail("a")
    now[0] = 900
    lockout.success("a")
    lockout.success("missing")
    for _ in range(5):
        lockout.fail("a")
    assert lockout.check("a") == 900


def test_sessions_preserve_kind_and_expire_at_default_ttl():
    now = [0.0]
    sessions = Sessions(clock=lambda: now[0])
    local = sessions.create("local")
    tailnet = sessions.create("tailnet")
    assert local != tailnet
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", local)
    assert sessions.get(local) == "local"
    assert sessions.get(tailnet) == "tailnet"
    assert sessions.get("missing") is None
    now[0] = 43199
    assert sessions.get(local) == "local"
    now[0] = 43200
    assert sessions.get(local) is None
    assert sessions.get(local) is None


def test_sessions_custom_ttl_revoke_and_instance_isolation():
    now = [10.0]
    sessions = Sessions(clock=lambda: now[0], ttl=2)
    sid = sessions.create("local")
    assert Sessions(clock=lambda: now[0]).get(sid) is None
    sessions.revoke(sid)
    sessions.revoke("missing")
    assert sessions.get(sid) is None
    sid = sessions.create("tailnet")
    now[0] = 12
    assert sessions.get(sid) is None
    assert Lockout(clock=lambda: now[0]).check("a") == 0


@pytest.mark.parametrize("request_scheme, host, headers, expected", [
    ("http", "127.0.0.1:8090", {}, "http"),
    ("https", "localhost:8090", {}, "https"),
    ("http", "pc.tail1234.ts.net", {}, "https"),
    ("http", "pc.tail1234.ts.net.evil.example", {}, "http"),
    ("http", "localhost:8090", {"X-Forwarded-Proto": "https"}, "https"),
    ("http", "pc.tail1234.ts.net", {"x-forwarded-proto": "http"}, "http"),
])
def test_effective_scheme(request_scheme, host, headers, expected):
    assert effective_scheme(request_scheme, host, MappingProxyType(headers)) == expected


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_csrf_rules_for_mutations(method):
    host = "127.0.0.1:8090"
    assert not csrf_ok(method, {}, "http", host)
    assert not csrf_ok(method, {"X-Panel-Request": "0"}, "http", host)
    assert csrf_ok(method, {"X-Panel-Request": "1"}, "http", host)
    assert csrf_ok(method, MappingProxyType({
        "x-panel-request": "1", "oRiGiN": "http://127.0.0.1:8090",
    }), "http", host)
    for origin in ["", "null", "https://evil.example", "https://127.0.0.1:8090"]:
        assert not csrf_ok(method, {
            "X-Panel-Request": "1", "Origin": origin,
        }, "http", host)


@pytest.mark.parametrize("name", [
    "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded",
])
@pytest.mark.parametrize("value", ["proxy", ""])
def test_classify_proxy_headers_fail_closed(name, value):
    for spelling in [name, name.lower(), name.swapcase()]:
        headers = MappingProxyType({spelling: value})
        assert classify("127.0.0.1", "127.0.0.1:8090", headers) == "tailnet"


@pytest.mark.parametrize("ident", ["a", "*"])
def test_concurrent_attempts_admit_only_five_logins(ident):
    lockout = Lockout(clock=lambda: 0.0)
    barrier = Barrier(40)

    def attempt(_):
        barrier.wait(timeout=10)
        return lockout.attempt(ident)

    with ThreadPoolExecutor(max_workers=40) as pool:
        results = list(pool.map(attempt, range(40)))
    assert results.count(0.0) == 5
    assert results.count(900.0) == 35
    assert lockout.check(ident) == 900
    assert lockout.attempt("b") == 0


def test_lockout_caps_entries_after_1000_identities_without_evicting_global_lock():
    lockout = Lockout(clock=lambda: 0.0)
    for _ in range(5):
        assert lockout.attempt("*") == 0
    for index in range(1000):
        assert lockout.attempt(f"person-{index}") == 0
    assert len(lockout._attempts) == 256
    assert lockout.check("*") == 900
    assert lockout.attempt("*") == 900


@pytest.mark.parametrize("touch", ["check", "attempt", "fail"])
def test_lockout_evicts_least_recently_used_identity_but_never_global(touch):
    lockout = Lockout(clock=lambda: 0.0)
    for ident in ["*", "recent", "old"]:
        for _ in range(5):
            lockout.attempt(ident)
    for index in range(253):
        lockout.attempt(f"person-{index}")
    getattr(lockout, touch)("recent")
    lockout.attempt("new")
    assert lockout.check("*") == 900
    assert lockout.check("recent") == 900
    assert lockout.check("old") == 0
    assert len(lockout._attempts) == 256


def test_attempt_escalation_expiry_and_success_reset():
    now = [0.0]
    lockout = Lockout(clock=lambda: now[0])
    for duration in [900, 1800, 3600, 7200, 14400, 28800, 57600, 86400, 86400]:
        for _ in range(5):
            assert lockout.attempt("a") == 0
        assert lockout.attempt("a") == duration
        now[0] += duration
    lockout.success("a")
    for _ in range(4):
        assert lockout.attempt("a") == 0
    lockout.success("a")
    for _ in range(5):
        assert lockout.attempt("a") == 0
    assert lockout.check("a") == 900


def test_csrf_get_and_tailnet_origin():
    assert csrf_ok("GET", {}, "http", "127.0.0.1:8090")
    host = "pc.tail1234.ts.net"
    assert csrf_ok("POST", {
        "X-Panel-Request": "1", "Origin": "https://pc.tail1234.ts.net",
    }, effective_scheme("http", host, {}), host)
