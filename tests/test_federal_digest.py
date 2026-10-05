"""Fixture-only digest contracts: version discovery, dates, evidence and errors."""
import json
import secrets

import httpx
import pytest

from congress_api.features import federal_digest as d
from congress_api.remote import ProtectedMCP
from starlette.testclient import TestClient
from starlette.responses import JSONResponse


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("BIRDIE_DIGEST_STATE", str(tmp_path / "digest.sqlite3"))
    monkeypatch.setattr(d, "govinfo_api_key", lambda: "test-placeholder")


@pytest.fixture
def official(monkeypatch):
    calls = {"congress": [], "govinfo": []}
    payloads = {"bills": [], "actions": [], "versions": [], "version_count": None,
                "bill_count": None, "status": 200, "cursor": "next", "detail": {}}

    async def congress(ctx, **kwargs):
        calls["congress"].append(kwargs)
        if kwargs.get("bill_number") and not kwargs.get("sub_endpoint"):
            return {"bill": payloads["detail"]}
        key = "actions" if kwargs.get("sub_endpoint") == "actions" else "bills"
        records = payloads[key]
        offset, limit = kwargs.get("offset", 0), kwargs.get("limit", 250)
        count = payloads["bill_count"] if key == "bills" else None
        return {key: records[offset:offset + limit],
                "pagination": {"count": len(records) if count is None else count}}

    async def govinfo(body):
        calls["govinfo"].append(body)
        records = payloads["versions"]
        count = payloads["version_count"]
        return httpx.Response(payloads["status"], json={
            "results": records, "count": len(records) if count is None else count,
            "offsetMark": payloads["cursor"],
        })

    monkeypatch.setattr(d, "fetch_bill_data", congress)
    monkeypatch.setattr(d, "govinfo_search_post", govinfo)
    return payloads, calls


def version(package="BILLS-119hr1ih", date="2026-10-02", **extra):
    return {"packageId": package, "dateIssued": date, "title": "Fixture bill", **extra}


def bill(t="HR", n=1, **extra):
    return {"congress": 119, "type": t, "number": str(n), "title": "Fixture bill",
            "updateDate": "2026-10-05T03:00:00Z", **extra}


async def run(op="bill_texts_received", start="2026-10-02", **kwargs):
    return json.loads(await d.federal_digest(None, op, start, **kwargs))


@pytest.mark.asyncio
async def test_single_day_and_distinct_dates(official):
    p, calls = official
    p["versions"] = [version(lastModified="2026-10-05T02:00:00Z", dateIngested="2026-10-04")]
    p["detail"] = {"latestAction": {"actionDate": "2026-09-29", "text": "Referred to committee"}}
    result = await run()
    row = result["results"][0]
    assert "publishdate:range(2026-10-02,2026-10-02)" in calls["govinfo"][0]["query"]
    assert row["text_publication_date"] == "2026-10-02"
    assert row["text_receipt_date"] is None
    assert row["legislative_action_date"] == "2026-09-29"
    assert row["source_updated_at"] == "2026-10-05T02:00:00Z"
    assert row["source_ingested_at"] == "2026-10-04"
    assert row["sponsor"] is None
    assert "congress.gov/bill/119th-congress/house-bill/1" in row["official_url"]


@pytest.mark.asyncio
async def test_range_boundaries_versions_dedup_order(official):
    p, _ = official
    p["versions"] = [version("BILLS-119sres8ats", "2026-10-04"), version(), version(),
                     version("BILLS-119hr1eh", "2026-10-03"), version("BILLS-119s2is", "2026-10-01")]
    result = await run(toDateTime="2026-10-04")
    assert [r["text_version_code"] for r in result["results"]] == ["ih", "eh", "ats"]
    assert result["results"][-1]["chamber"] == "Senate"
    assert result["results"][-1]["legislation_id"] == "sres8-119"


@pytest.mark.asyncio
async def test_empty_weekend(official):
    result = await run("daily_legislative_activity", "2026-10-04")
    assert result["results"] == []
    assert all(not x for x in result["categories"].values())
    assert result["next_offset"] is result["next_page_token"] is None


@pytest.mark.asyncio
async def test_text_pagination_reuses_upstream_token(official):
    p, calls = official
    p["versions"], p["version_count"] = [version()], 2
    first = await run(limit=1)
    assert first["next_page_token"]
    p["versions"] = [version("BILLS-119hr1eh")]
    second = await run(limit=1, page_token=first["next_page_token"])
    assert calls["govinfo"][-1]["offsetMark"] == "next"
    assert second["next_page_token"] is None
    assert second["results"][0]["text_version_code"] == "eh"


@pytest.mark.asyncio
async def test_bill_and_action_pagination(official):
    p, _ = official
    p["bills"] = [bill(), bill("S", 2)]
    p["actions"] = [{"actionDate": "2026-10-02", "text": "Passed House.", "actionCode": str(i)}
                    for i in range(251)]
    first = await run("legislative_changes_since", limit=1, include_texts=False)
    assert first["next_offset"] == 1
    # Identical substantive evidence is suppressed despite redundant upstream action codes.
    assert len(first["results"]) == 1
    second = await run("legislative_changes_since", offset=1, limit=1, include_texts=False)
    assert second["next_offset"] is None
    assert second["results"][0]["legislation_id"] == "s2-119"


@pytest.mark.asyncio
async def test_late_text_and_metadata_churn(official):
    p, calls = official
    p["versions"] = [version(date="2026-09-29", dateIngested="2026-10-05", lastModified="2026-10-05T03:00:00Z"),
                     version("BILLS-119hr2ih", "2026-09-28", lastModified="2026-10-05T04:00:00Z")]
    result = await run("legislative_changes_since", "2026-10-05T01:00:00Z", toDateTime="2026-10-05T23:00:00Z")
    assert "lastModified:range(2026-10-05T01:00:00Z,2026-10-05T23:00:00Z)" in calls["govinfo"][0]["query"]
    assert not result["results"]
    assert len(result["late_index_candidates"]) == 1
    assert result["late_index_candidates"][0]["event_date"] == "2026-09-29"


@pytest.mark.asyncio
async def test_late_action_durable_baseline_and_retry(official):
    p, _ = official
    p["bills"] = [bill()]
    p["actions"] = [{"actionDate": "2026-10-01", "text": "Introduced in House"}]
    assert not (await run("legislative_changes_since", "2026-10-05", include_texts=False))["results"]
    p["actions"].append({"actionDate": "2026-10-02", "text": "Reported by committee"})
    second = await run("legislative_changes_since", "2026-10-05", include_texts=False)
    assert len(second["results"]) == 1
    assert second["results"][0]["late_indexed"] is True
    assert second["results"][0]["event_date"] == "2026-10-02"
    replay = await run("legislative_changes_since", "2026-10-05", include_texts=False)
    assert replay["results"] == second["results"]


@pytest.mark.asyncio
async def test_compact_daily_categories(official):
    p, _ = official
    p["bills"] = [bill("HRES", 1)]
    p["actions"] = [{"actionDate": "2026-10-02", "text": "Agreed to in House."},
                    {"actionDate": "2026-10-02", "text": "Message on Senate action sent to House."},
                    {"actionDate": "2026-10-01", "text": "Introduced in House."}]
    result = await run("daily_legislative_activity")
    assert result["categories"]["passed_a_chamber"] == [0]
    assert result["categories"]["resolutions"] == [0]
    assert len(result["results"]) == 1
    assert "full_text" not in json.dumps(result)


@pytest.mark.parametrize("text,expected", [
    ("Became Public Law No: 119-1.", "enacted"), ("Vetoed by President.", "veto"),
    ("Passed Senate with an amendment", "chamber_passage"),
    ("Failed of passage in House", "failed_passage"), ("Amendment rejected", None),
    ("Motion to reconsider not agreed to", None), ("Referred to the Committee on Finance.", "committee_movement"),
    ("Conference report filed", "conference"), ("Enrolled bill signed", "enrollment"),
    ("Amendment agreed to", "amendment"), ("Signed by President", "presidential_action"),
    ("House voted to override veto", "override"), ("Sponsor information updated", None),
])
def test_classification_requires_evidence(text, expected):
    assert d.classify({"text": text}) == expected


@pytest.mark.parametrize("start,kwargs", [
    ("garbage", {}), ("2026-10-02T12:00:00", {}), ("2026-10-04", {"toDateTime": "2026-10-02"}),
    ("2026-10-02", {"limit": 0}), ("2026-10-02", {"offset": -1}),
    ("2026-10-02", {"page_token": "garbage"}), ("2026-10-02", {"bill_type": "s"}),
])
@pytest.mark.asyncio
async def test_validation_no_network(official, start, kwargs):
    _, calls = official
    assert "error" in await run(start=start, **kwargs)
    assert not calls["congress"] and not calls["govinfo"]


@pytest.mark.parametrize("status,code", [(429, "rate_limited"), (503, "govinfo_unavailable"),
                                        (403, "govinfo_key_rejected")])
@pytest.mark.asyncio
async def test_upstream_error_not_false_empty(official, status, code):
    p, _ = official
    p["status"] = status
    assert (await run())["error"]["code"] == code


@pytest.mark.parametrize("records,count,cursor", [([None], 1, "next"), ([{}], 1, "next"),
                                                ([version()], "bad", "next"), ([], 2, "next"),
                                                ([version()], 2, "*")])
@pytest.mark.asyncio
async def test_malformed_upstream(official, records, count, cursor):
    p, _ = official
    p.update(versions=records, version_count=count, cursor=cursor)
    assert (await run())["error"]["code"] == "malformed_upstream"


@pytest.mark.asyncio
async def test_congress_error_envelope_preserved(official, monkeypatch):
    async def error(*a, **k):
        return {"error": json.dumps({"error": {"code": "rate_limited", "message": "Quota exceeded"}})}
    monkeypatch.setattr(d, "fetch_bill_data", error)
    result = await run("daily_legislative_activity", include_texts=False)
    assert result["error"]["code"] == "rate_limited"


@pytest.mark.asyncio
async def test_transport_error_sanitized(official, monkeypatch):
    async def broken(body):
        raise httpx.ConnectError("sensitive detail must not escape")
    monkeypatch.setattr(d, "govinfo_search_post", broken)
    assert "sensitive detail" not in json.dumps(await run())


def test_remote_auth_and_health():
    token = secrets.token_urlsafe(32)

    async def app(scope, receive, send):
        await JSONResponse({"path": scope["path"]})(scope, receive, send)

    client = TestClient(ProtectedMCP(app, token))
    try:
        assert client.get("/healthz").status_code == 200
        assert client.post("/mcp").status_code == 401
        assert client.post("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.post("/mcp", headers={"Authorization": f"Bearer {token}"}).json() == {"path": "/mcp"}
        assert client.post(f"/connect/{token}/mcp").json() == {"path": "/mcp"}
        assert client.post(f"/connect/{token}/mcp/extra").status_code == 401
    finally:
        client.close()


def test_remote_fail_closed():
    with pytest.raises(ValueError):
        ProtectedMCP(None, "")


def test_real_remote_mcp_handshake(monkeypatch):
    from congress_api.remote import create_app
    for name in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy"):
        monkeypatch.delenv(name, raising=False)
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("CONGRESS_API_KEY", "test-placeholder")
    monkeypatch.setenv("MCP_ACCESS_TOKEN", token)
    with TestClient(create_app(), base_url="http://localhost:8000") as client:
        headers = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {token}"}
        response = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "fixture-client", "version": "1"},
            },
        })
        assert response.status_code == 200
        assert "protocolVersion" in response.text
        tools = client.post(f"/connect/{token}/mcp", headers={"Accept": headers["Accept"]},
                            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert tools.status_code == 200
        assert '"name":"federal_digest"' in tools.text.replace(" ", "")
        assert token not in tools.text


@pytest.mark.asyncio
async def test_historical_daily_discovery_retains_old_event_date(official):
    p, calls = official
    p["bills"] = [bill()]
    p["actions"] = [{"actionDate": "2026-10-02", "text": "Passed House"},
                    {"actionDate": "2026-09-01", "text": "Introduced in House"}]
    result = await run("daily_legislative_activity", include_texts=False)
    assert calls["congress"][0]["toDateTime"] > "2026-10-02T23:59:59Z"
    assert len(result["results"]) == 1
    assert result["results"][0]["event_date"] == "2026-10-02"
    assert result["results"][0]["source_updated_at"] == "2026-10-05T03:00:00Z"


@pytest.mark.asyncio
async def test_missing_key_is_explicit(official, monkeypatch):
    monkeypatch.setattr(d, "govinfo_api_key", lambda: "")
    assert (await run())["error"]["code"] == "api_key_missing"
    assert not official[1]["govinfo"]


def test_official_identifier_ordinal():
    assert "/101st-congress/senate-joint-resolution/2" in d.identity(101, "SJRES", 2)["official_url"]


@pytest.mark.asyncio
async def test_free_host_reports_ephemeral_observation_limit(official, monkeypatch):
    monkeypatch.setenv("BIRDIE_DIGEST_EPHEMERAL", "true")
    result = await run("daily_legislative_activity")
    assert result["observation_state"]["ephemeral"] is True
    assert "reset" in result["observation_state"]["note"]
