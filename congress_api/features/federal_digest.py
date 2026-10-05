"""Federal digest composition over the existing Congress.gov/GovInfo clients.

Publication searches enumerate VERSION packages, not recently active bills.
Update searches expose late-indexed evidence as candidates, never as newly
introduced legislation. No full text is downloaded by this tool.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from mcp.server.mcpserver import Context

from ..core.exceptions import (
    APIErrorResponse, CommonErrors, CongressionalAPIError, ErrorType,
    format_error_response,
)
from ..core.response_utils import ResponseProcessor
from ..mcp_app import mcp
from .digest_state import observe, action_key
from .bill_text import trace
from .bill_text.client import govinfo_api_key, govinfo_search_post, VERSION_TYPE_MAP
from .buckets.bills import govinfo_search as search
from .buckets.bills.helpers import fetch_bill_data

TYPE_NAMES = {
    "hr": "house-bill", "s": "senate-bill", "hjres": "house-joint-resolution",
    "sjres": "senate-joint-resolution", "hconres": "house-concurrent-resolution",
    "sconres": "senate-concurrent-resolution", "hres": "house-resolution", "sres": "senate-resolution",
}
VERSION_NAMES = {code: name.title() for name, code in VERSION_TYPE_MAP.items()}
CATEGORIES = (
    "new_legislation", "new_bill_texts", "passed_a_chamber", "advancing",
    "failed", "presidential_action", "resolutions", "procedural",
)


def reject(code: str, message: str, kind: ErrorType = ErrorType.SERVER_ERROR) -> None:
    raise CongressionalAPIError(APIErrorResponse(kind, message, ["Retry or check the requested range."], code))


def bound(value: str, end: bool = False) -> datetime:
    """Date bounds are whole inclusive UTC days; timestamps retain precision."""
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if len(value) == 10 and end:
            result += timedelta(days=1, microseconds=-1)
        if len(value) != 10 and result.tzinfo is None:
            raise ValueError("Timezone required")
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        reject("invalid_parameters", "Use an ISO date or a timezone-aware ISO timestamp.", ErrorType.VALIDATION)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def api_iso(value: datetime) -> str:
    """Official query bounds use seconds; preserve original precision in range metadata."""
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def in_range(value: Any, start: datetime, end: datetime) -> bool:
    return bool(value) and start <= bound(str(value)) <= end


def identity(congress: Any, bill_type: Any, number: Any) -> dict[str, Any]:
    c = search.validate_congress(congress)
    t = search.validate_bill_type(bill_type)
    if c is None or t is None or isinstance(number, bool):
        reject("malformed_upstream", "Legislation identity is missing or invalid.")
    try:
        n = int(number)
    except (TypeError, ValueError):
        reject("malformed_upstream", "Legislation number is invalid.")
    if n <= 0:
        reject("malformed_upstream", "Legislation number is invalid.")
    suffix = "th" if 10 <= c % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(c % 10, "th")
    return {
        "congress": c, "legislation_type": t, "number": n,
        "legislation_id": f"{t}{n}-{c}", "chamber": "House" if t.startswith("h") else "Senate",
        "official_url": f"https://www.congress.gov/bill/{c}{suffix}-congress/{TYPE_NAMES[t]}/{n}",
    }


async def congress_page(ctx: Context, key: str, **kwargs) -> dict:
    data = await fetch_bill_data(ctx, **kwargs)
    if isinstance(data, dict) and "error" in data:
        error = data["error"]
        try:
            envelope = json.loads(error) if isinstance(error, str) else error
            if isinstance(envelope, dict) and isinstance(envelope.get("error"), dict):
                e = envelope["error"]
                raise CongressionalAPIError(APIErrorResponse(
                    ErrorType.GENERAL, e["message"], [e.get("remediation") or "Retry later."],
                    e["code"], e.get("detail"),
                ))
        except (ValueError, TypeError, KeyError):
            pass
        reject("congress_unavailable", "Congress.gov request failed.")
    if not isinstance(data, dict) or key not in data:
        reject("malformed_upstream", f"Congress.gov response lacks {key}.")
    expected = dict if key == "bill" else list
    if not isinstance(data[key], expected) or (expected is list and any(not isinstance(x, dict) for x in data[key])):
        reject("malformed_upstream", f"Congress.gov {key} has an unexpected shape.")
    return data


def next_offset(data: dict, offset: int, size: int, key: str) -> int | None:
    paging = data.get("pagination", {})
    if not isinstance(paging, dict):
        reject("malformed_upstream", "Invalid Congress.gov pagination.")
    count = paging.get("count")
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
        reject("malformed_upstream", "Invalid Congress.gov count.")
    more = bool(paging.get("next")) or (count is not None and offset + len(data[key]) < count)
    if more and not data[key]:
        reject("malformed_upstream", "Congress.gov pagination made no progress.")
    return offset + len(data[key]) if more else None


async def actions_for(ctx: Context, ident: dict) -> list[dict]:
    """Enumerate actions via the existing offset-based API; never silently truncate."""
    items, offset = [], 0
    for _ in range(40):
        page = await congress_page(ctx, "actions", congress=ident["congress"],
                                   bill_type=ident["legislation_type"], bill_number=ident["number"],
                                   sub_endpoint="actions", limit=250, offset=offset)
        items.extend(page["actions"])
        offset = next_offset(page, offset, 250, "actions")
        if offset is None:
            return items
    reject("scan_limit_exceeded", "Action history exceeded 10,000 records; narrow the bill scope.")


def classify(action: dict) -> str | None:
    """Conservative positive evidence only. No inactivity=>stalled inference."""
    text = str(action.get("text") or "").lower()
    rules = (
        ("override", r"overrid|veto.*(overridden|sustained)"),
        ("veto", r"vetoed|veto message"),
        ("enacted", r"became (public|private) law"),
        ("presidential_action", r"signed by president|presented to president"),
        ("failed_passage", r"failed of passage|bill failed to pass|resolution failed to pass"),
        ("chamber_passage", r"^(passed (house|senate)|agreed to in (house|senate))\b"),
        ("enrollment", r"enrolled|enrollment"),
        ("conference", r"conference|conferees"),
        ("committee_movement", r"referred to|reported by|reported (with|without)|ordered to be reported|discharged"),
        ("amendment", r"amendment.*(agreed to|adopted)|concurred in.*amendment"),
        ("introduced", r"^introduced in|^introduced\b"),
    )
    for kind, pattern in rules:
        if re.search(pattern, text):
            return kind
    return None


async def text_page(ctx: Context, start: datetime, end: datetime, limit: int,
                    page_token: str | None, congress: int | None, bill_type: str | None,
                    updates: bool = False) -> dict:
    if not govinfo_api_key():
        raise CongressionalAPIError(search.keyless_error())
    state = search.default_page_state() if page_token is None else search.decode_page_token(page_token)
    if state is None or state["skip"]:
        reject("invalid_parameters", "Use the next_page_token returned by this operation.", ErrorType.VALIDATION)
    query = search.build_query("", congress, bill_type,
                               None if updates else start.date().isoformat(),
                               None if updates else end.date().isoformat()).strip()
    if updates:
        query += f" lastModified:range({api_iso(start)},{api_iso(end)})"
    body = search.build_search_body(query, limit, state["offsetMark"])
    body["pageSize"] = limit  # one result per version, no bill-level grouping/overfetch
    body["sorts"] = [{"field": "lastModified" if updates else "publishdate", "sortOrder": "ASC"}]
    response = await govinfo_search_post(body)
    if response.status_code in (401, 403):
        raise CongressionalAPIError(search.key_rejected_error(response.status_code))
    if response.status_code == 429:
        reject("rate_limited", "GovInfo rate limit persisted after upstream transport retries.", ErrorType.RATE_LIMIT)
    if response.status_code != 200:
        reject("govinfo_unavailable", "GovInfo publication search failed.")
    try:
        data = response.json()
    except ValueError:
        reject("malformed_upstream", "GovInfo returned invalid JSON.")
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        reject("malformed_upstream", "GovInfo search lacks a results list.")
    records, count = data["results"], data.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        reject("malformed_upstream", "GovInfo search lacks a valid count.")
    consumed = state["records_consumed"] + len(records)
    cursor = data.get("offsetMark")
    if consumed < count and (not records or not isinstance(cursor, str) or cursor == state["offsetMark"]):
        reject("malformed_upstream", "GovInfo pagination cannot continue without progress.")
    token, _ = search.compute_next_token(count, state["records_consumed"], 0, len(records), True,
                                         state["offsetMark"], cursor)
    results, details = [], {}
    for record in records:
        if not isinstance(record, dict):
            reject("malformed_upstream", "GovInfo returned a non-object version.")
        parsed = search.parse_package_id(record.get("packageId"))
        if parsed is None:
            reject("malformed_upstream", "GovInfo returned an invalid BILLS package identity.")
        c, t, n, version = parsed
        ident = identity(c, t, n)
        if ident["legislation_id"] not in details:
            detail = await congress_page(ctx, "bill", congress=c, bill_type=t, bill_number=n)
            details[ident["legislation_id"]] = detail["bill"]
        bill = details[ident["legislation_id"]]
        publication = record.get("dateIssued")
        if not publication:
            reject("malformed_upstream", "GovInfo version is missing its publication date.")
        bound(publication)
        # GovInfo publishdate is date precision: do not imply an intraday receipt time.
        if not updates and not start.date().isoformat() <= publication[:10] <= end.date().isoformat():
            continue
        action = bill.get("latestAction") or {}
        package = record["packageId"]
        results.append({
            **ident, "title": bill.get("title") or record.get("title"),
            "sponsor": (bill.get("sponsors") or [None])[0],
            "text_version_code": version, "text_version_name": VERSION_NAMES.get(version),
            "package_id": package, "text_publication_date": publication,
            "text_receipt_date": None, "source_updated_at": record.get("lastModified"),
            "source_ingested_at": record.get("dateIngested"),
            "congress_updated_at": bill.get("updateDate"),
            "legislative_action_date": action.get("actionDate"), "latest_action": action,
            "current_status": action.get("text"), "committees": bill.get("committees"),
            "official_text_urls": {
                "pdf": f"https://www.govinfo.gov/content/pkg/{package}/pdf/{package}.pdf",
                "xml": f"https://www.govinfo.gov/content/pkg/{package}/xml/{package}.xml",
                "details": f"https://www.govinfo.gov/app/details/{package}",
            },
        })
    results = ResponseProcessor.deduplicate_results(results, ["package_id"])
    results.sort(key=lambda x: (x["text_publication_date"], x["legislation_id"], x["text_version_code"]))
    return {"results": results, "results_count": len(results), "total_version_matches": count,
            "next_page_token": token, "search_source": "govinfo_publication" if not updates else "govinfo_updates",
            "date_semantics": "Official version publication date; Congress.gov receipt date is unavailable."}


async def changes_page(ctx: Context, start: datetime, end: datetime, limit: int, offset: int,
                       congress: int | None, bill_type: str | None, historical: bool = False) -> dict:
    discovery_end = max(end, datetime.now(timezone.utc)) if historical else end
    data = await congress_page(ctx, "bills", congress=congress, bill_type=bill_type,
                              fromDateTime=api_iso(start), toDateTime=api_iso(discovery_end), sort="updateDate+asc",
                              limit=limit, offset=offset)
    events, candidates = [], []
    for bill in data["bills"]:
        ident = identity(bill.get("congress"), bill.get("type"), bill.get("number"))
        # updateDateIncludingText explicitly includes text-only bill updates.
        updated = bill.get("updateDateIncludingText") or bill.get("updateDate")
        actions = await actions_for(ctx, ident)
        known_bill, newly_seen, observed_at = observe(ident["legislation_id"], actions, iso(start))
        for action in actions:
            kind = classify(action)
            if kind is None:
                continue
            date = action.get("actionDate")
            if not date:
                reject("malformed_upstream", "A substantive action lacks its date.")
            # Action dates have day precision. A timestamp watermark includes that
            # entire day to avoid dropping morning/evening actions with unknown times.
            dated_in_range = start.date().isoformat() <= date[:10] <= end.date().isoformat()
            late = not historical and not dated_in_range and date[:10] < start.date().isoformat()
            new_observation = action_key(action) in newly_seen
            if not dated_in_range and not (late and known_bill and new_observation):
                if late and not known_bill and date == (bill.get("latestAction") or {}).get("actionDate"):
                    candidates.append({**ident, "event_type": "unbaselined_late_action", "event_date": date,
                                       "source_updated_at": updated, "action_text": action.get("text"),
                                       "title": bill.get("title")})
                continue
            events.append({**ident, "late_indexed": late, "observed_at": observed_at[action_key(action)],
                           "event_type": kind, "event_date": date,
                           "source_updated_at": updated, "title": bill.get("title"),
                           "action_text": action.get("text"), "action_code": action.get("actionCode"),
                           "action_chamber": (action.get("sourceSystem") or {}).get("name"),
                           "status": (bill.get("latestAction") or {}).get("text")})
    events = ResponseProcessor.deduplicate_results(
        events, ["legislation_id", "event_type", "event_date", "action_text"])
    events.sort(key=lambda x: (x["event_date"], x["legislation_id"], x["event_type"], x["action_text"] or ""))
    return {"results": events, "results_count": len(events),
            "next_offset": next_offset(data, offset, limit, "bills"),
            "bills_scanned": len(data["bills"]), "late_index_candidates": candidates}


def category(event: dict) -> str:
    kind = event["event_type"]
    if kind == "introduced":
        return "new_legislation"
    if kind == "new_text_version":
        return "new_bill_texts"
    if kind == "chamber_passage":
        return "passed_a_chamber"
    if kind in {"veto", "override", "enacted", "presidential_action"}:
        return "presidential_action"
    if kind == "failed_passage":
        return "failed"
    return "advancing"


async def _digest_impl(
    ctx: Context, operation: str, fromDateTime: str, toDateTime: str | None = None,
    congress: int | None = None, bill_type: str | None = None, limit: int = 20,
    offset: int = 0, page_token: str | None = None, include_actions: bool = True,
    include_texts: bool = True,
) -> str:
    started = time.perf_counter()
    args = {"operation": operation, "fromDateTime": fromDateTime, "toDateTime": toDateTime,
            "congress": congress, "bill_type": bill_type, "limit": limit, "offset": offset,
            "page_token": page_token, "include_actions": include_actions, "include_texts": include_texts}
    try:
        if operation not in {"bill_texts_received", "legislative_changes_since", "daily_legislative_activity"}:
            reject("invalid_parameters", "Unknown federal_digest operation.", ErrorType.VALIDATION)
        start = bound(fromDateTime)
        end = (bound(toDateTime, True) if toDateTime else datetime.now(timezone.utc)
               if operation == "legislative_changes_since" else bound(start.date().isoformat(), True))
        if start > end:
            reject("invalid_parameters", "Start must not be after end.", ErrorType.VALIDATION)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            reject("invalid_parameters", "limit must be 1..50 candidate records.", ErrorType.VALIDATION)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            reject("invalid_parameters", "offset must be a nonnegative integer.", ErrorType.VALIDATION)
        congress = search.validate_congress(congress)
        bill_type = search.validate_bill_type(bill_type)
        if bill_type and congress is None:
            reject("invalid_parameters", "bill_type requires congress for Congress.gov scoping.", ErrorType.VALIDATION)
        if operation == "bill_texts_received":
            result = await text_page(ctx, start, end, limit, page_token, congress, bill_type)
        else:
            activity = (await changes_page(ctx, start, end, limit, offset, congress, bill_type,
                                           historical=operation == "daily_legislative_activity")
                        if include_actions else {"results": [], "next_offset": None, "bills_scanned": 0,
                                                 "late_index_candidates": []})
            texts = (await text_page(ctx, start, end, limit, page_token, congress, bill_type,
                                    updates=operation == "legislative_changes_since")
                     if include_texts else {"results": [], "next_page_token": None})
            events, candidates = activity["results"], activity["late_index_candidates"]
            for text in texts["results"]:
                if start.date().isoformat() <= text["text_publication_date"][:10] <= end.date().isoformat():
                    events.append({**text, "event_type": "new_text_version",
                                   "event_date": text["text_publication_date"]})
                elif in_range(text.get("source_ingested_at"), bound(start.date().isoformat()), end):
                    candidates.append({**text, "event_type": "late_indexed_text",
                                       "event_date": text["text_publication_date"]})
                # lastModified-only churn is deliberately omitted.
            events.sort(key=lambda x: (x["event_date"], x["legislation_id"], x["event_type"],
                                       x.get("package_id") or x.get("action_text") or ""))
            result = {"results": events, "results_count": len(events), "late_index_candidates": candidates,
                      "next_offset": activity["next_offset"], "next_page_token": texts["next_page_token"],
                      "bills_scanned": activity["bills_scanned"],
                      "coverage_note": "Source updates discover candidates; late actions use a durable baseline. "
                                       "First-seen old actions are uncertain. Dedup identities between runs."}
            if operation == "daily_legislative_activity":
                groups = {name: [] for name in CATEGORIES}
                for index, event in enumerate(events):
                    groups[category(event)].append(index)
                    if "res" in event["legislation_type"]:
                        groups["resolutions"].append(index)
                result["categories"] = groups  # indices prevent repeated metadata
        result["range"] = {"start": iso(start), "end": iso(end)}
        result["operation"] = operation
        output = json.dumps(result, separators=(",", ":"))
    except CongressionalAPIError as exc:
        output = format_error_response(exc.error_response)
    except (OSError, sqlite3.Error):
        output = format_error_response(APIErrorResponse(
            ErrorType.SERVER_ERROR, "Digest observation store is unavailable.",
            ["Check BIRDIE_DIGEST_STATE permissions and disk space."], "digest_state_unavailable"))
    except httpx.HTTPError:
        output = format_error_response(CommonErrors.api_server_error("federal_digest transport"))
    except (ValueError, TypeError, KeyError, AttributeError):
        output = format_error_response(APIErrorResponse(
            ErrorType.SERVER_ERROR, "Unexpected official-data response shape.",
            ["Retry later; report a reproducible upstream shape."], "malformed_upstream"))
    if trace.enabled():
        trace.write("federal_digest", args, output, round((time.perf_counter() - started) * 1000, 1))
    return output


@mcp.tool("federal_digest", title="Birdie federal legislative digest")
async def federal_digest(
    ctx: Context, operation: str, fromDateTime: str, toDateTime: str | None = None,
    congress: int | None = None, bill_type: str | None = None, limit: int = 20,
    offset: int = 0, page_token: str | None = None, include_actions: bool = True,
    include_texts: bool = True,
) -> str:
    """Official, compact federal digest evidence (no full texts or editorial inference).

    Operations: bill_texts_received (GovInfo version publication dates, NOT an
    exact Congress.gov receipt-date export); legislative_changes_since (updated
    bills' substantive dated actions + newly ingested/published text evidence);
    daily_legislative_activity (inclusive date range categories).
    fromDateTime is required: ISO date or timezone-aware timestamp. Omit
    toDateTime for a single publication/activity day; changes_since defaults
    to now. Date ranges include both ends. Publication/action dates have day
    precision; source update timestamps are kept separately. Changes_since
    uses source updates for discovery and returns older indexed text as
    late_index_candidates, not as new publication. Newly observed old actions
    are returned after an initial durable baseline;
    first-run old latest actions are uncertain late_index_candidates.
    limit caps candidate bills AND version packages per page (1..50), not
    events. A bill may yield many substantive events. Pass next_offset and
    next_page_token independently; set include_actions/include_texts false
    when that source is exhausted. Walk BOTH sources before advancing the
    digest watermark. Dedup across runs by event identity or package_id.
    Categories overlap: resolutions is a secondary index. Ceremonial intent
    and stalled status are not inferred. Every record links to official data.
    """
    return await _digest_impl(
        ctx, operation=operation, fromDateTime=fromDateTime, toDateTime=toDateTime,
        congress=congress, bill_type=bill_type, limit=limit, offset=offset,
        page_token=page_token, include_actions=include_actions, include_texts=include_texts,
    )
