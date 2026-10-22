"""Read-only post-mortem report routes in the panel."""

from fastapi.testclient import TestClient

from bitgetbot.panel.app import PanelDeps, create_app
from bitgetbot.postmortem import report
from tests.panel.test_app import PANEL_HEADER, local_login, panel, tailnet_login  # noqa: F401
from tests.postmortem.test_report import new_doc


def stored(panel, tmp_path):
    panel.deps.reports_dir = tmp_path / "reports"
    doc = new_doc()
    report.write_report(panel.deps.reports_dir, doc)
    return doc


def test_reports_need_session(panel, tmp_path):
    stored(panel, tmp_path)
    for client in (panel.local, panel.tailnet):
        assert client.get("/api/reports").status_code == 401
        assert client.get(f"/api/reports/{new_doc()['key']}").status_code == 401


def test_reports_list_and_view(panel, tmp_path):
    doc = stored(panel, tmp_path)
    local_login(panel)
    rows = panel.local.get("/api/reports").json()["reports"]
    assert [row["key"] for row in rows] == [doc["key"]]
    assert rows[0]["status"] == "pending" and rows[0]["trade_id"] == 2
    view = panel.local.get(f"/api/reports/{doc['key']}")
    assert view.status_code == 200
    assert view.json()["markdown"].startswith("# 거래 #2 BTCUSDT 롱") and view.json()["status"] == "pending"
    tailnet_login(panel)
    assert panel.tailnet.get(f"/api/reports/{doc['key']}").status_code == 200


def test_view_falls_back_to_rendering_when_md_missing(panel, tmp_path):
    doc = stored(panel, tmp_path)
    (panel.deps.reports_dir / f"{doc['key']}.md").unlink()
    local_login(panel)
    assert panel.local.get(f"/api/reports/{doc['key']}").json()["markdown"].startswith("# 거래 #2")


def test_list_is_empty_and_writes_nothing_without_reports(panel, tmp_path):
    panel.deps.reports_dir = tmp_path / "none"
    local_login(panel)
    assert panel.local.get("/api/reports").json() == {"reports": []}
    assert not panel.deps.reports_dir.exists()


def test_bad_or_missing_key_404(panel, tmp_path):
    stored(panel, tmp_path)
    local_login(panel)
    for key in ["dry-9-1", "dry-2-1.json", "..%2Findex", "index", "paper-1-1"]:
        response = panel.local.get(f"/api/reports/{key}")
        assert response.status_code == 404
        assert str(tmp_path) not in response.text


def test_no_report_write_routes(panel, tmp_path):
    doc = stored(panel, tmp_path)
    local_login(panel)
    for path in ("/api/reports", f"/api/reports/{doc['key']}"):
        for method in ("post", "put", "delete"):
            response = getattr(panel.local, method)(path, headers=PANEL_HEADER)
            assert response.status_code == 405


def test_reports_page_is_public_shell(panel):
    response = panel.local.get("/reports")
    assert response.status_code == 200 and "거래 리포트" in response.text


def test_lifespan_starts_postmortem_only_when_given(tmp_path):
    started = []

    def forbidden(*args, **kwargs):
        raise AssertionError("no trading access in this test")

    def build(**extra):
        return PanelDeps(token="t", settings_loader=forbidden, reader_factory=forbidden, ft_factory=forbidden,
                         runner=forbidden, pin_store_path=tmp_path / "pin.json", record_on_startup=False,
                         background=started.append, **extra)

    with TestClient(create_app(build())):
        pass
    assert started == []

    def watcher():
        pass

    with TestClient(create_app(build(postmortem=watcher))):
        pass
    assert started == [watcher]
