from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from modules.utils import model_to_dict  # noqa: F401  (kept for parity with other import modules)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _parse_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, float) and pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value: Any) -> int | None:
    num = _parse_number(value)
    return int(num) if num is not None else None


def _clean_numeric_text(value: Any) -> str | None:
    """Like _clean_text, but strips a trailing '.0' pandas adds when a
    numeric-looking column (e.g. postal code) is read in as a float."""
    text = _clean_text(value)
    if text is None:
        return None
    if re.match(r"^-?\d+\.0$", text):
        text = text[:-2]
    return text


def _parse_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and pd.isna(value):
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _unix_to_iso_date(value: Any) -> str | None:
    """OpenRegister bulk exports store dates as unix timestamps (seconds)."""
    num = _parse_number(value)
    if num is None:
        return None
    try:
        return datetime.fromtimestamp(num, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _unix_to_year(value: Any) -> int | None:
    iso_date = _unix_to_iso_date(value)
    if not iso_date:
        return None
    try:
        return int(iso_date[:4])
    except ValueError:
        return None


# id example: "DE-HRB-R3101-27929" or "DE-HRB-P2305-120008-P2303"
# Segment 2 is the register type. Segment 3 is an internal OpenRegister
# court code (not decodable to a human-readable court name without a
# lookup table we don't have, so register_court is intentionally left
# blank for these rows). Segment 4 is the register number.
_ID_PATTERN = re.compile(r"^[A-Z]{2}-([A-Za-z]+)-[^-]+-([A-Za-z0-9]+)")


def _parse_register_id(company_id: str) -> tuple[str | None, str | None]:
    match = _ID_PATTERN.match(company_id or "")
    if not match:
        return None, None
    return match.group(1).upper(), match.group(2)


def _industry_codes_to_wz_json(value: Any) -> dict[str, Any] | None:
    text = _clean_text(value)
    if not text:
        return None

    codes = [c.strip() for c in re.split(r"[;,]", text) if c.strip()]
    if not codes:
        return None

    return {"WZ2025": [{"code": code} for code in codes]}


def _row_to_company_payload(row: dict[str, Any]) -> dict[str, Any] | None:
    company_id = _clean_text(row.get("id"))
    if not company_id:
        return None

    register_type, register_number = _parse_register_id(company_id)

    payload: dict[str, Any] = {
        "openregister_company_id": company_id,
        "register_id": company_id,

        "name": _clean_text(row.get("name")),
        "legal_form": _clean_text(row.get("legal_form")),
        "active": _parse_bool(row.get("active")),
        "country": _clean_text(row.get("address.country")),

        "register_type": register_type,
        "register_number": register_number,

        "city": _clean_text(row.get("address.city")),
        "postal_code": _clean_numeric_text(row.get("address.zip")),
        "street": _clean_text(row.get("address.street")),
        "website": _clean_text(row.get("contact_info.website_url")),
        "email": _clean_text(row.get("contact_info.email")),
        "phone": _clean_text(row.get("contact_info.phone")),

        "founding_year": _unix_to_year(row.get("incorporated_at")),

        "openregister_wz_codes": _industry_codes_to_wz_json(row.get("industry_codes")),

        "openregister_capital_amount_eur": _parse_number(row.get("capital.amount")),
        "openregister_financials_date": _unix_to_iso_date(row.get("indicator.date")),
        "openregister_revenue_eur": _parse_number(row.get("indicator.revenue")),
        "openregister_employees": _parse_int(row.get("indicator.employees")),
        "openregister_balance_sheet_total_eur": _parse_number(row.get("indicator.balance_sheet_total")),
        "openregister_net_income_eur": _parse_number(row.get("indicator.net_income")),
        "openregister_equity_eur": _parse_number(row.get("indicator.equity")),
        "openregister_cash_eur": _parse_number(row.get("indicator.cash")),
        "openregister_liabilities_eur": _parse_number(row.get("indicator.liabilities")),
        "openregister_real_estate_eur": _parse_number(row.get("indicator.real_estate")),

        "source": "openregister_import",
        "company_info_enriched_at": now_iso(),
        # OpenRegister imports carry their financial indicators directly from the
        # export file, which comes from the same OpenRegister source the financials
        # API endpoint would. Mark financials as already done so enrichment skips
        # the redundant /financials call for these companies.
        "financials_enriched_at": now_iso(),
    }

    # Do not overwrite existing DB values with blanks.
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        cleaned[key] = value

    return cleaned


def _read_excel(uploaded_file: Any) -> pd.DataFrame:
    try:
        uploaded_file.seek(0)
    except Exception:
        pass

    return pd.read_excel(uploaded_file, engine="openpyxl")


def _existing_company_by_openregister_id(supabase, company_id: str) -> dict[str, Any] | None:
    response = (
        supabase.table("companies")
        .select("id,openregister_company_id")
        .eq("openregister_company_id", company_id)
        .limit(1)
        .execute()
    )
    rows = getattr(response, "data", None) or []
    return rows[0] if rows else None


def run_openregister_import(
    uploaded_file: Any,
    supabase,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """
    Imports OpenRegister's own bulk-export .xlsx directly.

    Unlike NorthData import, no OpenRegister API search/match is needed -
    the file's "id" column already is the real openregister_company_id.
    Rows are upserted straight into companies, source-specific
    (openregister_*) columns only.
    """
    df = _read_excel(uploaded_file)

    if max_rows:
        df = df.head(max_rows)

    total_rows = len(df)
    imported = 0
    updated = 0
    skipped = 0
    errors = 0
    results: list[dict[str, Any]] = []

    for _, row in df.iterrows():
        row_data = row.to_dict()
        company_id = _clean_text(row_data.get("id"))
        name = _clean_text(row_data.get("name")) or "(unnamed)"

        if not company_id:
            skipped += 1
            results.append({"name": name, "company_id": None, "status": "skipped_no_id"})
            continue

        try:
            payload = _row_to_company_payload(row_data)
            if not payload:
                skipped += 1
                results.append({"name": name, "company_id": company_id, "status": "skipped_empty"})
                continue

            existing = _existing_company_by_openregister_id(supabase, company_id)

            supabase.table("companies").upsert(payload, on_conflict="openregister_company_id").execute()

            if existing:
                updated += 1
                results.append({"name": name, "company_id": company_id, "status": "updated"})
            else:
                imported += 1
                results.append({"name": name, "company_id": company_id, "status": "imported"})

        except Exception as exc:
            errors += 1
            results.append({"name": name, "company_id": company_id, "status": "error", "message": str(exc)})

    return {
        "total_rows": total_rows,
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "results": results,
    }
