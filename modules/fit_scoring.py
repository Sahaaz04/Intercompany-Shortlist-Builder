from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from anthropic import Anthropic

FIT_SCORING_CONCURRENCY = 6

DEFAULT_FIT_CONFIG = {
    "revenue_min": 4000000,
    "revenue_max": 8000000,
    "employees_min": 20,
    "employees_max": 1000,
    "net_income_min": 0,
    "net_income_max": 2000000,
    "min_shareholder_age": 55,
    "min_ubo_age": 55,
    "preferred_business_type": "B2B industrial company",
    "preferred_industries": "cosmetics, food, contract manufacturing",
    "additional_instructions": "Prioritize succession situations, simple ownership, industrial/B2B production, and companies with clear operational improvement potential.",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _yes_no_flag(value: Any) -> str:
    text = safe(value)
    lowered = text.lower()

    if lowered in {"yes", "y", "true", "1"}:
        return "Yes"

    if lowered in {"no", "n", "false", "0"}:
        return "No"

    return ""


def _claude_assumption_flag(model_row: dict[str, Any], company: dict[str, Any]) -> str:
    explicit = _yes_no_flag(
        model_row.get("business_segment_2")
        or company.get("claude_business_segment_2")
        or company.get("claude_assumption")
        or company.get("business_segment_2")
    )

    if explicit:
        return explicit

    status = safe(model_row.get("api_status") or company.get("claude_api_status")).upper()

    if status.startswith("FALLBACK") or "FALLBACK" in status:
        return "Yes"

    if model_row or company.get("claude_business_segment"):
        return "No"

    return ""


def _fetch_all_paginated(
    supabase,
    table: str,
    select: str = "*",
    page_size: int = 1000,
    hard_cap: int = 50000,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 0

    while len(rows) < hard_cap:
        end = min(start + page_size - 1, hard_cap - 1)
        res = supabase.table(table).select(select).range(start, end).execute()
        batch = getattr(res, "data", None) or []

        if not batch:
            break

        rows.extend(batch)

        if len(batch) < page_size:
            break

        start += page_size

    return rows


def _fetch_rows(
    supabase,
    table: str,
    column: str,
    value: str,
    limit: int = 200,
) -> list[dict[str, Any]]:
    res = supabase.table(table).select("*").eq(column, value).limit(limit).execute()
    return getattr(res, "data", None) or []


def _latest_model(supabase, register_id: str, company_id: str | None) -> dict[str, Any]:
    rows = _fetch_rows(supabase, "company_models", "company_register_id", register_id, limit=20)

    if not rows and company_id:
        rows = _fetch_rows(supabase, "company_models", "openregister_company_id", company_id, limit=20)

    rows = [r for r in rows if r.get("model_provider") == "claude"]

    if not rows:
        return {}

    return sorted(
        rows,
        key=lambda r: safe(r.get("updated_at") or r.get("created_at")),
        reverse=True,
    )[0]


def _existing_score_exists(supabase, register_id: str) -> bool:
    res = (
        supabase.table("company_fit_scores")
        .select("id")
        .eq("company_register_id", register_id)
        .eq("model_provider", "claude")
        .limit(1)
        .execute()
    )

    return bool(getattr(res, "data", None) or [])


def _delete_existing_score(supabase, register_id: str) -> None:
    (
        supabase.table("company_fit_scores")
        .delete()
        .eq("company_register_id", register_id)
        .eq("model_provider", "claude")
        .execute()
    )


def _summarize_owners(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = []

    for row in rows[:20]:
        out.append({
            "name": row.get("shareholder_name"),
            "type": row.get("owner_type"),
            "relation_type": row.get("relation_type"),
            "age": row.get("age"),
            "nominal_share_eur": row.get("nominal_share_eur"),
            "percentage_share": row.get("percentage_share"),
            "city": row.get("owner_city"),
            "country": row.get("owner_country"),
        })

    return {
        "total": len(rows),
        "natural_person_count": sum(1 for r in rows if r.get("owner_type") == "natural_person"),
        "legal_person_count": sum(1 for r in rows if r.get("owner_type") == "legal_person"),
        "owners": out,
    }


def _summarize_ubos(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = []

    for row in rows[:20]:
        out.append({
            "name": row.get("ubo_name"),
            "type": row.get("ubo_type"),
            "age": row.get("age"),
            "percentage_share": row.get("percentage_share"),
            "max_percentage_share": row.get("max_percentage_share"),
            "city": row.get("ubo_city"),
            "country": row.get("ubo_country"),
        })

    return {
        "total": len(rows),
        "natural_person_count": sum(1 for r in rows if r.get("ubo_type") == "natural_person"),
        "legal_person_count": sum(1 for r in rows if r.get("ubo_type") == "legal_person"),
        "ubos": out,
    }


def build_fit_score_prompt(
    company: dict[str, Any],
    model_row: dict[str, Any],
    owners: list[dict[str, Any]],
    ubos: list[dict[str, Any]],
    fit_config: dict[str, Any],
) -> str:
    company_payload = {
        "identity": {
            "register_id": company.get("register_id"),
            "openregister_company_id": company.get("openregister_company_id"),
            "company_name": company.get("company_name") or company.get("name"),
            "legal_form": company.get("legal_form"),

            # Company-level founding/incorporation year.
            # This is the company's own founding year, not shareholder integration year.
            "founding_year": company.get("founding_year"),

            "city": company.get("city"),
            "website": company.get("website"),
            "active": company.get("active"),
        },
        "business": {
            "purpose": company.get("purpose"),

            # Source-specific industry/WZ fields.
            # Do not treat these as fallback values for each other.
            "openregister_wz_codes": company.get("openregister_wz_codes"),
            "northdata_wz_code": company.get("northdata_wz_code"),

            # Claude business segment is now the final official division label.
            # Claude assumption is a Yes/No flag stored in company_models.business_segment_2.
            # Yes = fallback assumption from purpose + NorthData WZ.
            # No = website-derived analysis.
            "claude_business_segment": (
                model_row.get("business_segment")
                or company.get("claude_business_segment")
            ),
            "claude_assumption": _claude_assumption_flag(model_row, company),
            "claude_business_model": (
                model_row.get("business_model")
                or company.get("claude_business_model")
            ),
            "claude_business_summary": (
                model_row.get("summary")
                or company.get("claude_detailed_business_summary")
            ),
        },
        "financials": {
            # All financial/employee fields are source-specific.
            # Do not mix or fallback one into the other.
            "openregister_revenue_eur": company.get("openregister_revenue_eur"),
            "northdata_revenue_eur": company.get("northdata_revenue_eur"),

            "northdata_employees": company.get("northdata_employees"),
            "openregister_employees": company.get("openregister_employees"),

            "northdata_balance_sheet_total_eur": company.get("northdata_balance_sheet_total_eur"),
            "openregister_balance_sheet_total_eur": company.get("openregister_balance_sheet_total_eur"),

            "northdata_net_income_eur": company.get("northdata_net_income_eur"),
            "openregister_net_income_eur": company.get("openregister_net_income_eur"),

            "northdata_equity_eur": company.get("northdata_equity_eur"),
            "openregister_equity_eur": company.get("openregister_equity_eur"),

            "northdata_cash_eur": company.get("northdata_cash_eur"),
            "openregister_cash_eur": company.get("openregister_cash_eur"),

            "northdata_liabilities_eur": company.get("northdata_liabilities_eur"),
            "openregister_liabilities_eur": company.get("openregister_liabilities_eur"),

            "northdata_real_estate_eur": company.get("northdata_real_estate_eur"),
            "openregister_real_estate_eur": company.get("openregister_real_estate_eur"),

            "northdata_capital_amount_eur": company.get("northdata_capital_amount_eur"),
            "openregister_capital_amount_eur": company.get("openregister_capital_amount_eur"),

            "financials_date": company.get("financials_date"),
            "openregister_financials_date": company.get("openregister_financials_date"),
        },
        "ownership_summary": {
            "number_of_owners": company.get("number_of_owners"),
            "natural_person_owner_count": company.get("natural_person_owner_count"),
            "legal_person_owner_count": company.get("legal_person_owner_count"),
            "youngest_owner_age": company.get("youngest_owner_age"),
            "oldest_owner_age": company.get("oldest_owner_age"),
            "has_sole_owner": company.get("has_sole_owner"),
            "has_majority_owner": company.get("has_majority_owner"),
            "largest_owner_percentage": company.get("largest_owner_percentage"),
            "main_owner_name": company.get("main_owner_name"),
            "main_owner_percentage_share": company.get("main_owner_percentage_share"),
        },
        "direct_owners": _summarize_owners(owners),
        "beneficial_ownership_or_control_chain": _summarize_ubos(ubos),
        "target_criteria": fit_config,
    }

    return f"""
You are scoring German companies for acquisition / succession fit.

Use ONLY the provided company data. Do not invent facts.

Score from 1 to 5:
5 = Very high fit: most criteria fulfilled, strong succession/acquisition potential, healthy or improvable company.
4 = High fit: key criteria fulfilled, succession/acquisition potential visible.
3 = Medium fit: some criteria fit, but important gaps or uncertainty.
2 = Low fit: major criteria missing, weak fit.
1 = No fit: clearly outside target profile or high-risk.

Important scoring guidance:
- Revenue, employee min/max, net income min/max, preferred industries, business type, shareholder age and UBO age targets are driven by user config.
- founding_year is the company's own founding/incorporation year. It is not a shareholder integration year.
- An older founding_year can indicate company maturity, operating history, and possible succession relevance, but do not over-weight it without ownership or management evidence.
- Revenue fields are source-specific:
  - openregister_revenue_eur is OpenRegister revenue.
  - northdata_revenue_eur is NorthData revenue.
  - Do not merge them.
  - Do not pretend one is available if only the other source has it.
  - If both exist and differ, mention the discrepancy as uncertainty when relevant.
- Employee fields are source-specific:
  - northdata_employees is the NorthData employee value.
  - openregister_employees is the OpenRegister-only employee value from OpenRegister indicators.
  - Do not merge them. If both exist and differ, treat that as source discrepancy.
- Balance sheet fields are source-specific:
  - northdata_balance_sheet_total_eur is the NorthData balance sheet total.
  - openregister_balance_sheet_total_eur is the OpenRegister-only balance sheet total value from OpenRegister indicators.
  - Do not merge them. If both exist and differ, mention uncertainty when relevant.
- Net income fields are source-specific:
  - northdata_net_income_eur is the NorthData net income.
  - openregister_net_income_eur is the OpenRegister-only net income value from OpenRegister indicators.
  - Do not merge them. If both exist and differ, mention uncertainty when relevant.
- Equity fields are source-specific:
  - northdata_equity_eur is the NorthData equity value.
  - openregister_equity_eur is the OpenRegister-only equity value from OpenRegister indicators.
  - Do not merge them. If both exist and differ, mention uncertainty when relevant.
- Cash fields are source-specific:
  - northdata_cash_eur is the NorthData cash value.
  - openregister_cash_eur is the OpenRegister-only cash value from OpenRegister indicators.
  - Do not merge them. If both exist and differ, mention uncertainty when relevant.
- Liabilities fields are source-specific:
  - northdata_liabilities_eur is the NorthData liabilities value.
  - openregister_liabilities_eur is the OpenRegister-only liabilities value from OpenRegister indicators.
  - Do not merge them. If both exist and differ, mention uncertainty when relevant.
- Real estate fields are source-specific:
  - northdata_real_estate_eur is the NorthData real estate value.
  - openregister_real_estate_eur is the OpenRegister-only real estate value from OpenRegister indicators.
  - Do not merge them. If both exist and differ, mention uncertainty when relevant.
- Capital amount fields are source-specific:
  - northdata_capital_amount_eur is the NorthData capital amount.
  - openregister_capital_amount_eur is the OpenRegister-only capital amount from OpenRegister.
  - Do not merge them. If both exist and differ, mention uncertainty when relevant.
- Industry/WZ fields are source-specific:
  - openregister_wz_codes is from OpenRegister.
  - northdata_wz_code is from NorthData.
  - Do not merge them.
- Claude business fields:
  - claude_business_segment is the final official division label, either website-derived or fallback-assumed.
  - claude_assumption = "No" means the Claude segment, business model, and summary were derived from website evidence.
  - claude_assumption = "Yes" means the Claude segment, business model, and summary were fallback assumptions from registered purpose + NorthData WZ because website evidence was unavailable, scraping failed, or the website result was incomplete.
  - claude_business_model is the specific activity/model. Treat it as stronger evidence when claude_assumption is "No" and weaker/conservative evidence when claude_assumption is "Yes".
  - claude_business_summary follows the same evidence strength rule as claude_business_model.
  - Do not penalize a company only because claude_assumption is "Yes", but mention uncertainty when the assumption materially affects the score.
- Net income within or above the configured net_income_min/net_income_max range is a positive financial signal; heavy losses are a risk flag unless there is clear turnaround/upside evidence.
- Natural-person direct owners or UBOs at/above the configured minimum shareholder age or minimum UBO age increase succession signal.
- Direct owners are the legal ownership layer; UBOs are beneficial/control-chain evidence.
- Natural-person ownership is stronger for succession; purely corporate/institutional ownership weakens succession signal.
- Missing or empty fields (e.g. no revenue, no employee count, no net income) mean that data point is simply unknown - do not treat an empty field as a negative signal by itself. Reflect it as uncertainty (e.g. "financial data not available") rather than as a weakness or risk flag.
- Only treat data gaps as a real concern when so much is missing that no meaningful assessment is possible at all (e.g. virtually nothing is known about financials, ownership, or business model combined) - in that case, say so explicitly and lower confidence, but still avoid scoring it as if something bad was found.
- Penalize unrelated sectors, distress, unclear business model, too-small size, and very complex ownership - these are separate from missing data and should still be flagged when actually present in the data.

Return ONLY valid JSON. No markdown. No explanation outside JSON.

Required JSON schema:
{{
  "fit_score": 1,
  "fit_label": "No Fit / Low Fit / Medium Fit / High Fit / Very High Fit",
  "fit_comment": "2-4 sentence explanation",
  "succession_signal": "short explanation",
  "financial_signal": "short explanation",
  "shareholder_signal": "short explanation",
  "risk_flags": ["flag 1", "flag 2"],
  "recommended_action": "Reject / Monitor / Manual Review / Prioritize"
}}

Company data:
{json.dumps(company_payload, ensure_ascii=False, indent=2, default=str)}
""".strip()


def _parse_json(text: str) -> dict[str, Any]:
    text = safe(text)

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"\s*```$", "", text).strip()

    start = text.find("{")
    end = text.rfind("}")

    if start >= 0 and end >= start:
        text = text[start : end + 1]

    return json.loads(text)


def score_with_claude(
    client: Anthropic,
    model_name: str,
    company: dict[str, Any],
    model_row: dict[str, Any],
    owners: list[dict[str, Any]],
    ubos: list[dict[str, Any]],
    fit_config: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    request_payload = {
        "model": model_name,
        "max_tokens": 800,
        "messages": [
            {
                "role": "user",
                "content": build_fit_score_prompt(
                    company,
                    model_row,
                    owners,
                    ubos,
                    fit_config,
                ),
            }
        ],
    }

    if "opus-4-7" not in str(model_name).lower():
        request_payload["temperature"] = 0.1

    response = client.messages.create(**request_payload)

    response_text = "\n".join(
        block.text
        for block in response.content
        if getattr(block, "type", "") == "text"
    ).strip()

    if not response_text:
        raise ValueError("Empty Claude response.")

    return _parse_json(response_text), response_text


def _bulk_fetch_by_register_id(
    supabase,
    table: str,
    register_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Fetch every row from `table` where company_register_id is in the given list,
    grouped into {register_id: [rows]}. Paginates past PostgREST's per-request cap
    by chunking the IN filter."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    if not register_ids:
        return grouped

    id_chunk = 200

    for i in range(0, len(register_ids), id_chunk):
        chunk = register_ids[i : i + id_chunk]

        page_size = 1000
        start = 0

        while True:
            end = start + page_size - 1
            res = (
                supabase.table(table)
                .select("*")
                .in_("company_register_id", chunk)
                .range(start, end)
                .execute()
            )
            batch = getattr(res, "data", None) or []
            for row in batch:
                key = row.get("company_register_id")
                if key is None:
                    continue
                grouped.setdefault(key, []).append(row)

            if len(batch) < page_size:
                break
            start += page_size

    return grouped


def _bulk_fetch_latest_models(
    supabase,
    register_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Get the latest Claude model row per company_register_id, one query
    (paginated), then reduced in-memory. Same semantics as _latest_model."""
    grouped = _bulk_fetch_by_register_id(supabase, "company_models", register_ids)

    latest_by_register: dict[str, dict[str, Any]] = {}

    for register_id, rows in grouped.items():
        rows = [r for r in rows if r.get("model_provider") == "claude"]
        if not rows:
            continue
        latest = sorted(
            rows,
            key=lambda r: safe(r.get("updated_at") or r.get("created_at")),
            reverse=True,
        )[0]
        latest_by_register[register_id] = latest

    return latest_by_register


def _bulk_existing_scored_register_ids(supabase) -> set[str]:
    """Return the set of register_ids that already have a successful Claude fit
    score. Paginated. Replaces per-company _existing_score_exists lookups."""
    ids: set[str] = set()
    page_size = 1000
    start = 0

    while True:
        end = start + page_size - 1
        res = (
            supabase.table("company_fit_scores")
            .select("company_register_id")
            .eq("model_provider", "claude")
            .eq("api_status", "success")
            .range(start, end)
            .execute()
        )
        batch = getattr(res, "data", None) or []
        for row in batch:
            rid = row.get("company_register_id")
            if rid:
                ids.add(rid)
        if len(batch) < page_size:
            break
        start += page_size

    return ids


def run_fit_scoring(
    *,
    supabase,
    claude_api_key: str,
    model_name: str = "claude-sonnet-4-5",
    fit_config: dict[str, Any] | None = None,
    update_existing: bool = False,
    progress_callback=None,
) -> dict[str, Any]:
    if not claude_api_key:
        raise ValueError("Claude API key missing.")

    config = {**DEFAULT_FIT_CONFIG, **(fit_config or {})}
    companies = _fetch_all_paginated(supabase, "master_overview")

    # Reuse a single Anthropic client across the whole run - avoids re-doing TLS
    # setup per company. The Anthropic SDK is thread-safe for concurrent .messages.create.
    client = Anthropic(api_key=str(claude_api_key).strip())

    # Bulk prefetch: one paginated read per source instead of 3+ per company.
    register_ids: list[str] = [c.get("register_id") for c in companies if c.get("register_id")]

    already_scored_ids: set[str] = set()
    if not update_existing:
        already_scored_ids = _bulk_existing_scored_register_ids(supabase)

    latest_models_by_register = _bulk_fetch_latest_models(supabase, register_ids)
    owners_by_register = _bulk_fetch_by_register_id(supabase, "shareholders", register_ids)
    ubos_by_register = _bulk_fetch_by_register_id(supabase, "company_ubos", register_ids)

    # Split into "scorable now" vs "skip" up front so parallel workers only do real work.
    to_score: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    skipped = 0

    total = len(companies)
    counts_lock = Lock()
    counters = {"scored": 0, "errors": 0, "done": 0}

    def _tick():
        if progress_callback is None:
            return
        with counts_lock:
            counters["done"] += 1
            done = counters["done"]
        try:
            progress_callback(done, total)
        except Exception:
            pass

    for company in companies:
        register_id = company.get("register_id")
        company_name = company.get("company_name") or company.get("name") or company.get("openregister_company_id")

        if not register_id:
            _tick()
            continue

        if register_id in already_scored_ids and not update_existing:
            skipped += 1
            results.append({
                "company": company_name,
                "status": "skipped",
                "reason": "existing score",
            })
            _tick()
            continue

        to_score.append(company)

    def _score_one(company: dict[str, Any]) -> dict[str, Any]:
        register_id = company.get("register_id")
        company_id = company.get("openregister_company_id")
        company_name = company.get("company_name") or company.get("name") or company_id

        try:
            if update_existing:
                _delete_existing_score(supabase, register_id)

            model_row = latest_models_by_register.get(register_id, {}) or {}
            owners = owners_by_register.get(register_id, []) or []
            ubos = ubos_by_register.get(register_id, []) or []

            parsed, raw_response = score_with_claude(
                client,
                model_name,
                company,
                model_row,
                owners,
                ubos,
                config,
            )

            fit_score = parsed.get("fit_score")
            try:
                fit_score = int(fit_score)
            except Exception:
                fit_score = None

            risk_flags = parsed.get("risk_flags", [])
            risk_flags_text = "; ".join(map(str, risk_flags)) if isinstance(risk_flags, list) else safe(risk_flags)

            row = {
                "company_register_id": register_id,
                "openregister_company_id": company_id,
                "company_name": company_name,
                "fit_score": fit_score,
                "fit_label": safe(parsed.get("fit_label")),
                "fit_comment": safe(parsed.get("fit_comment")),
                "succession_signal": safe(parsed.get("succession_signal")),
                "financial_signal": safe(parsed.get("financial_signal")),
                "shareholder_signal": safe(parsed.get("shareholder_signal")),
                "risk_flags": risk_flags_text,
                "recommended_action": safe(parsed.get("recommended_action")),
                "model_provider": "claude",
                "model_name": model_name,
                "scoring_config": config,
                "api_status": "success",
                "notes": "",
                "raw_data": {"parsed": parsed, "raw_response": raw_response},
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }

            supabase.table("company_fit_scores").upsert(
                row,
                on_conflict="company_register_id,model_provider",
            ).execute()

            with counts_lock:
                counters["scored"] += 1

            return {
                "company": company_name,
                "status": "success",
                "fit_score": fit_score,
                "fit_label": row["fit_label"],
            }

        except Exception as exc:
            msg = str(exc)[:1000]

            err_row = {
                "company_register_id": register_id,
                "openregister_company_id": company_id,
                "company_name": company_name,
                "fit_score": None,
                "fit_label": "ERROR",
                "fit_comment": "",
                "succession_signal": "",
                "financial_signal": "",
                "shareholder_signal": "",
                "risk_flags": "",
                "recommended_action": "",
                "model_provider": "claude",
                "model_name": model_name,
                "scoring_config": config,
                "api_status": "error",
                "notes": msg,
                "raw_data": {"error": msg},
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }

            try:
                supabase.table("company_fit_scores").upsert(
                    err_row,
                    on_conflict="company_register_id,model_provider",
                ).execute()
            except Exception:
                pass

            with counts_lock:
                counters["errors"] += 1

            return {"company": company_name, "status": "error", "error": msg}

    # Fan out per-company scoring across a pool of workers. Anthropic API calls
    # are I/O-bound so threads let us have multiple in flight at once. Workers
    # only wait on the network - no shared state to fight over.
    total_to_score = len(to_score)
    completed = 0

    if progress_callback and to_score:
        try:
            progress_callback(0, total_to_score)
        except Exception:
            pass

    if to_score:
        with ThreadPoolExecutor(max_workers=FIT_SCORING_CONCURRENCY) as pool:
            futures = [pool.submit(_score_one, company) for company in to_score]
            for future in as_completed(futures):
                results.append(future.result())
                completed += 1

                if progress_callback:
                    try:
                        progress_callback(completed, total_to_score)
                    except Exception:
                        pass

    return {
        "companies_seen": len(companies),
        "processed": len(to_score),
        "scored": counters["scored"],
        "skipped": skipped,
        "errors": counters["errors"],
        "results": results,
    }
