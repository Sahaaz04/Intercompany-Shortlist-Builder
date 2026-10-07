from __future__ import annotations

import math
import re
from difflib import SequenceMatcher
from typing import Any

import pandas as pd

from modules.openregister_client import get_openregister_client
from modules.utils import model_to_dict


# Extra NorthData-only columns are ignored.
COLUMN_ALIASES = {
    "name": ["Name"],
    "legal_form": ["Legal form", "Legal Form"],
    "country": ["Country"],
    "postal_code": ["Postal code", "Postal Code", "Postcode", "Zip", "ZIP"],
    "city": ["City"],
    "street": ["Street"],
    "register_court": ["Register court", "Register Court"],
    "northdata_register_id": ["Register ID", "Register Id", "Register id"],
    "status": ["Status"],
    "phone": ["Phone"],
    "email": ["Email"],
    "website": ["Website"],
    "vat_id": ["VAT Id", "VAT ID", "Vat Id", "VAT"],
    "purpose": ["Subject", "Purpose"],

    # NorthData industry/WZ source column.
    # This is kept separate from OpenRegister industry_codes/openregister_wz_codes.
    "northdata_wz_code": [
        "Industry segment (NACE)",
        "Industry Segment (NACE)",
        "Industry segment (UKSIC)",
        "Industry Segment (UKSIC)",
        "Industry segment",
        "Industry Segment",
        "WZ code",
        "WZ Code",
        "WZ Codes",
        "NorthData WZ Code",
    ],

    "financials_date": ["Financials date", "Financials Date", "Financial Date"],

    "northdata_capital_amount_eur": [
        "Base/share capital EUR",
        "Base/share capital €",
        "Share capital EUR",
        "Share capital €",
        "Capital amount EUR",
        "Capital amount €",
    ],

    "northdata_balance_sheet_total_eur": [
        "Total assets EUR",
        "Total assets €",
        "Balance sheet total EUR",
        "Balance sheet total €",
        "Balance Sheet Total EUR",
        "Balance Sheet Total €",
        "Balance Sheet Tot",
    ],

    "northdata_net_income_eur": [
        "Earnings EUR",
        "Earnings €",
        "Net income EUR",
        "Net income €",
        "Net Income EUR",
        "Net Income €",
    ],

    # NorthData revenue now goes only into northdata_revenue_eur.
    "northdata_revenue_eur": [
        "Revenue EUR",
        "Revenue €",
        "Revenue",
    ],

    "northdata_equity_eur": [
        "Equity EUR",
        "Equity €",
    ],

    "northdata_employees": [
        "Employee number",
        "Employee Number",
        "Employees",
    ],

    "northdata_cash_eur": [
        "Cash on hand EUR",
        "Cash on hand €",
        "Cash EUR",
        "Cash €",
    ],

    "northdata_liabilities_eur": [
        "Liabilities EUR",
        "Liabilities €",
    ],

    "northdata_real_estate_eur": [
        "Real estate EUR",
        "Real estate €",
        "Real Estate EUR",
        "Real Estate €",
    ],
}


LEGAL_FORM_MAP = {
    "gmbh": "gmbh",
    "gesellschaftmitbeschrankterhaftung": "gmbh",

    "ug": "ug",
    "ughaftungsbeschrankt": "ug",
    "unternehmergesellschaft": "ug",
    "unternehmergesellschaftmbh": "ug",

    "kg": "kg",
    "kommanditgesellschaft": "kg",
    "gmbhcokg": "kg",
    "gmbhcompkg": "kg",

    "ohg": "ohg",
    "offenehandelsgesellschaft": "ohg",

    "ek": "ek",
    "eingetragenerkaufmann": "ek",
    "eingetragenekauffrau": "ek",

    "ag": "ag",
    "aktiengesellschaft": "ag",

    "se": "se",
    "societaseuropaea": "se",
}


REGISTER_TYPE_MAP = {
    "HRB": "HRB",
    "HRA": "HRA",
    "PR": "PR",
    "GNR": "GnR",
    "VR": "VR",
}


NUMERIC_LOGICAL_FIELDS = [
    "northdata_capital_amount_eur",
    "northdata_balance_sheet_total_eur",
    "northdata_net_income_eur",
    "northdata_revenue_eur",
    "northdata_equity_eur",
    "northdata_employees",
    "northdata_cash_eur",
    "northdata_liabilities_eur",
    "northdata_real_estate_eur",
]


def _norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def _normalize_for_compare(value: Any) -> str:
    text = _clean_text(value) or ""
    text = (
        text.replace("ä", "a")
        .replace("ö", "o")
        .replace("ü", "u")
        .replace("Ä", "A")
        .replace("Ö", "O")
        .replace("Ü", "U")
        .replace("ß", "ss")
    )
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _find_col(row: dict[str, Any], logical_name: str) -> Any:
    aliases = COLUMN_ALIASES.get(logical_name, [])
    normalized_row = {_norm_key(k): v for k, v in row.items()}

    for alias in aliases:
        key = _norm_key(alias)
        if key in normalized_row:
            return normalized_row[key]

    return None


def _has_source_value(value: Any) -> bool:
    return _clean_text(value) is not None


def _parse_number(value: Any) -> float | None:
    """
    Handles both international and German number formats:
    35,307,989.85  -> 35307989.85
    35.307.989,85  -> 35307989.85
    55548.86       -> 55548.86
    55.548,86      -> 55548.86
    (1,234.56)     -> -1234.56
    """
    if value is None:
        return None

    if isinstance(value, float) and math.isnan(value):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "-"}:
        return None

    text = (
        text.replace("€", "")
        .replace("%", "")
        .replace("\u00a0", "")
        .replace(" ", "")
        .strip()
    )

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    text = re.sub(r"[^0-9,.\-]", "", text)

    if not text:
        return None

    # If both comma and dot exist, the last separator is the decimal separator.
    if "," in text and "." in text:
        if text.rfind(".") > text.rfind(","):
            # 35,307,989.85
            text = text.replace(",", "")
        else:
            # 35.307.989,85
            text = text.replace(".", "").replace(",", ".")

    elif "," in text:
        parts = text.split(",")

        if len(parts) == 2 and len(parts[-1]) in {1, 2}:
            # 123,45
            text = text.replace(",", ".")
        else:
            # 1,234,567
            text = text.replace(",", "")

    elif "." in text:
        parts = text.split(".")

        if len(parts) > 2:
            # 1.234.567
            text = text.replace(".", "")

    try:
        number = float(text)
        return -number if negative else number
    except Exception:
        return None


def _parse_int(value: Any) -> int | None:
    number = _parse_number(value)
    if number is None:
        return None
    return int(round(number))


def _parse_date(value: Any) -> str | None:
    if value is None:
        return None

    if isinstance(value, float) and math.isnan(value):
        return None

    # Excel serial date, e.g. 45657
    if isinstance(value, (int, float)):
        try:
            parsed = pd.to_datetime(value, unit="D", origin="1899-12-30", errors="coerce")
            if pd.notna(parsed):
                return parsed.date().isoformat()
        except Exception:
            pass

    try:
        parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
        if pd.isna(parsed):
            return _clean_text(value)
        return parsed.date().isoformat()
    except Exception:
        return _clean_text(value)


def _normalize_legal_form(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None

    key = _normalize_for_compare(text)
    return LEGAL_FORM_MAP.get(key)


def _parse_status(value: Any) -> tuple[str | None, bool | None]:
    status = _clean_text(value)
    if not status:
        return None, None

    key = status.lower()

    inactive_words = [
        "inactive",
        "deleted",
        "dissolved",
        "liquidated",
        "removed",
        "gelöscht",
        "geloescht",
        "aufgelöst",
        "aufgeloest",
        "erloschen",
        "liquidation",
    ]
    active_words = [
        "active",
        "currently registered",
        "registered",
        "aktiv",
        "eingetragen",
        "bestehend",
    ]

    if any(word in key for word in inactive_words):
        return status, False
    if any(word in key for word in active_words):
        return status, True

    return status, None


def parse_register_id(value: Any) -> tuple[str | None, str | None]:
    """
    NorthData example:
    "HRB 30469" -> ("HRB", "30469")
    """
    text = _clean_text(value)
    if not text:
        return None, None

    text = re.sub(r"\s+", " ", text.strip())
    match = re.match(r"^([A-Za-z]+)\s*([A-Za-z0-9./\- ]+)$", text)
    if not match:
        return None, text

    raw_type = match.group(1).strip()
    raw_number = match.group(2).strip()

    register_type = REGISTER_TYPE_MAP.get(raw_type.upper(), raw_type)
    register_number = re.sub(r"\s+", "", raw_number)

    return register_type, register_number


def _name_similarity(a: Any, b: Any) -> float:
    left = _normalize_for_compare(a)
    right = _normalize_for_compare(b)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _candidate_score(candidate: dict[str, Any], row_data: dict[str, Any]) -> float:
    score = 0.0

    if _normalize_for_compare(candidate.get("register_type")) == _normalize_for_compare(row_data.get("register_type")):
        score += 40

    if _normalize_for_compare(candidate.get("register_number")) == _normalize_for_compare(row_data.get("register_number")):
        score += 40

    candidate_court = _normalize_for_compare(candidate.get("register_court"))
    row_court = _normalize_for_compare(row_data.get("register_court"))
    if candidate_court and row_court:
        if candidate_court == row_court:
            score += 30
        elif candidate_court in row_court or row_court in candidate_court:
            score += 18

    if candidate.get("legal_form") and row_data.get("legal_form"):
        if str(candidate.get("legal_form")).lower() == str(row_data.get("legal_form")).lower():
            score += 15

    score += _name_similarity(candidate.get("name"), row_data.get("name")) * 20

    return score


def _search_openregister_company(client, row_data: dict[str, Any]) -> dict[str, Any]:
    register_type = row_data.get("register_type")
    register_number = row_data.get("register_number")
    register_court = row_data.get("register_court")
    legal_form = row_data.get("legal_form")
    name = row_data.get("name")

    if not register_type or not register_number or not register_court:
        return {
            "status": "missing_register_data",
            "company_id": None,
            "candidate": None,
            "message": "Missing register type, register number, or register court.",
        }

    search_attempts: list[dict[str, Any]] = []

    strict_filters = [
        {"field": "register_type", "value": register_type},
        {"field": "register_number", "value": register_number},
        {"field": "register_court", "value": register_court},
    ]

    if legal_form:
        search_attempts.append({
            "label": "strict_with_legal_form",
            "filters": [*strict_filters, {"field": "legal_form", "value": legal_form}],
            "query": None,
        })

    search_attempts.append({
        "label": "strict_without_legal_form",
        "filters": strict_filters,
        "query": None,
    })

    # Original fallback for court wording mismatch.
    # Removes court but keeps type + number and adds name query.
    if name:
        search_attempts.append({
            "label": "register_number_with_name_query",
            "filters": [
                {"field": "register_type", "value": register_type},
                {"field": "register_number", "value": register_number},
            ],
            "query": {"value": name},
        })

    for attempt in search_attempts:
        try:
            kwargs: dict[str, Any] = {
                "filters": attempt["filters"],
                "pagination": {"page": 1, "per_page": 10},
            }
            if attempt["query"]:
                kwargs["query"] = attempt["query"]

            response = client.search.find_companies_v1(**kwargs)
            data = model_to_dict(response)
            results = data.get("results") or []

            if not results:
                continue

            scored = sorted(
                [
                    {
                        "score": _candidate_score(candidate, row_data),
                        "candidate": candidate,
                    }
                    for candidate in results
                ],
                key=lambda x: x["score"],
                reverse=True,
            )

            if len(scored) == 1:
                best = scored[0]
                if best["score"] >= 75:
                    return {
                        "status": "matched",
                        "company_id": best["candidate"].get("company_id"),
                        "candidate": best["candidate"],
                        "message": f"Matched via {attempt['label']}.",
                    }

            if len(scored) > 1:
                best = scored[0]
                second = scored[1]

                if best["score"] >= 75 and best["score"] - second["score"] >= 15:
                    return {
                        "status": "matched",
                        "company_id": best["candidate"].get("company_id"),
                        "candidate": best["candidate"],
                        "message": f"Matched via {attempt['label']}.",
                    }

                return {
                    "status": "multiple_candidates",
                    "company_id": None,
                    "candidate": None,
                    "message": f"Multiple candidates found via {attempt['label']}; skipped to avoid duplicate/wrong match.",
                    "candidates": [item["candidate"] for item in scored],
                }

        except Exception as exc:
            return {
                "status": "openregister_error",
                "company_id": None,
                "candidate": None,
                "message": str(exc),
            }

    return {
        "status": "no_match",
        "company_id": None,
        "candidate": None,
        "message": "No OpenRegister match found.",
    }


def _numeric_parse_warnings(row: dict[str, Any]) -> list[str]:
    warnings: list[str] = []

    for logical_name in NUMERIC_LOGICAL_FIELDS:
        source_value = _find_col(row, logical_name)
        if not _has_source_value(source_value):
            continue

        parsed = _parse_int(source_value) if logical_name == "northdata_employees" else _parse_number(source_value)

        if parsed is None:
            warnings.append(f"{logical_name}: could not parse {source_value!r}")

    return warnings


def _northdata_row_to_company_payload(row: dict[str, Any], company_id: str, candidate: dict[str, Any] | None) -> dict[str, Any]:
    register_type, register_number = parse_register_id(_find_col(row, "northdata_register_id"))
    status, active = _parse_status(_find_col(row, "status"))

    payload: dict[str, Any] = {
        "openregister_company_id": company_id,
        "register_id": company_id,

        "name": _clean_text(_find_col(row, "name")) or (candidate or {}).get("name"),
        "legal_form": _normalize_legal_form(_find_col(row, "legal_form")) or (candidate or {}).get("legal_form"),
        "active": active if active is not None else (candidate or {}).get("active"),
        "country": _clean_text(_find_col(row, "country")) or (candidate or {}).get("country"),

        "register_number": register_number or (candidate or {}).get("register_number"),
        "register_court": _clean_text(_find_col(row, "register_court")) or (candidate or {}).get("register_court"),
        "register_type": register_type or (candidate or {}).get("register_type"),

        "status": status,
        "city": _clean_text(_find_col(row, "city")),
        "postal_code": _clean_text(_find_col(row, "postal_code")),
        "street": _clean_text(_find_col(row, "street")),
        "website": _clean_text(_find_col(row, "website")),
        "email": _clean_text(_find_col(row, "email")),
        "phone": _clean_text(_find_col(row, "phone")),
        "vat_id": _clean_text(_find_col(row, "vat_id")),
        "purpose": _clean_text(_find_col(row, "purpose")),

        # NorthData source-specific fields.
        # Do not write to OpenRegister fields here.
        "northdata_wz_code": _clean_text(_find_col(row, "northdata_wz_code")),
        "northdata_revenue_eur": _parse_number(_find_col(row, "northdata_revenue_eur")),

        "financials_date": _parse_date(_find_col(row, "financials_date")),
        "northdata_capital_amount_eur": _parse_number(_find_col(row, "northdata_capital_amount_eur")),
        "northdata_balance_sheet_total_eur": _parse_number(_find_col(row, "northdata_balance_sheet_total_eur")),
        "northdata_net_income_eur": _parse_number(_find_col(row, "northdata_net_income_eur")),
        "northdata_equity_eur": _parse_number(_find_col(row, "northdata_equity_eur")),
        "northdata_employees": _parse_int(_find_col(row, "northdata_employees")),
        "northdata_cash_eur": _parse_number(_find_col(row, "northdata_cash_eur")),
        "northdata_liabilities_eur": _parse_number(_find_col(row, "northdata_liabilities_eur")),
        "northdata_real_estate_eur": _parse_number(_find_col(row, "northdata_real_estate_eur")),

        "source": "northdata_import",
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
    """
    Reads NorthData .xlsx upload.
    Current requirements include openpyxl, so .xlsx is supported.
    """
    try:
        uploaded_file.seek(0)
    except Exception:
        pass

    return pd.read_excel(uploaded_file, engine="openpyxl")


def _existing_company_by_openregister_id(supabase, company_id: str) -> dict[str, Any] | None:
    response = (
        supabase.table("companies")
        .select("id,openregister_company_id,register_id,name")
        .eq("openregister_company_id", company_id)
        .limit(1)
        .execute()
    )
    rows = getattr(response, "data", None) or []
    return rows[0] if rows else None


def run_northdata_import(
    *,
    uploaded_file: Any,
    openregister_api_key: str,
    supabase,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """
    Import NorthData Excel rows.

    Rules:
    - No temp company ID.
    - No inserting unmatched companies.
    - Every saved company must have real OpenRegister company_id.
    - Matching logic is the original project logic:
      strict_with_legal_form -> strict_without_legal_form -> register_number_with_name_query.
    - If OpenRegister ID already exists, update existing row.
    - NorthData revenue/WZ/financials write only to their dedicated
      northdata_ prefixed columns (northdata_revenue_eur, northdata_wz_code,
      northdata_employees, northdata_balance_sheet_total_eur,
      northdata_net_income_eur, northdata_equity_eur, northdata_cash_eur,
      northdata_liabilities_eur, northdata_real_estate_eur,
      northdata_capital_amount_eur). They never write to the shared legacy
      columns or to OpenRegister's openregister_ columns.
    """
    if not openregister_api_key:
        raise ValueError("OpenRegister API key is required.")

    df = _read_excel(uploaded_file)
    df = df.where(pd.notnull(df), None)

    if max_rows is not None and max_rows > 0:
        df = df.head(max_rows)

    client = get_openregister_client(openregister_api_key)

    results: list[dict[str, Any]] = []
    imported = 0
    updated = 0
    skipped = 0
    errors = 0
    rows_with_parse_warnings = 0

    for index, row in df.iterrows():
        raw_row = row.to_dict()

        register_type, register_number = parse_register_id(_find_col(raw_row, "northdata_register_id"))
        row_data = {
            "row_number": int(index) + 2,
            "name": _clean_text(_find_col(raw_row, "name")),
            "legal_form": _normalize_legal_form(_find_col(raw_row, "legal_form")),
            "register_court": _clean_text(_find_col(raw_row, "register_court")),
            "register_type": register_type,
            "register_number": register_number,
        }

        try:
            parse_warnings = _numeric_parse_warnings(raw_row)
            if parse_warnings:
                rows_with_parse_warnings += 1

            match = _search_openregister_company(client, row_data)

            if match.get("status") != "matched" or not match.get("company_id"):
                skipped += 1
                results.append({
                    **row_data,
                    "status": match.get("status"),
                    "message": match.get("message"),
                    "parse_warnings": "; ".join(parse_warnings) if parse_warnings else None,
                })
                continue

            company_id = str(match["company_id"])
            candidate = match.get("candidate") or {}

            payload = _northdata_row_to_company_payload(raw_row, company_id, candidate)

            existing = _existing_company_by_openregister_id(supabase, company_id)

            supabase.table("companies").upsert(
                payload,
                on_conflict="openregister_company_id",
            ).execute()

            if existing:
                updated += 1
                action = "updated_existing"
            else:
                imported += 1
                action = "inserted_new"

            results.append({
                **row_data,
                "openregister_company_id": company_id,
                "status": action,
                "message": match.get("message"),
                "parse_warnings": "; ".join(parse_warnings) if parse_warnings else None,
            })

        except Exception as exc:
            errors += 1
            results.append({
                **row_data,
                "status": "error",
                "message": str(exc),
            })

    return {
        "total_rows": len(df),
        "imported": imported,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "rows_with_parse_warnings": rows_with_parse_warnings,
        "results": results,
    }
