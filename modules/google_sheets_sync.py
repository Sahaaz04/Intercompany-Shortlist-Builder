from __future__ import annotations

import json
import re
from typing import Any

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

from modules.utils import flatten_for_sheet, format_industry_codes

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

MAX_SHEET_CELL_CHARS = 49_000

EXCLUDED_SHEET_COLUMNS = {
    "raw_search_result",
    "raw_company_details",
    "raw_financials",
    "raw_data",
    "sources_json",
}

DISPLAY_EXCLUDED_COLUMNS = EXCLUDED_SHEET_COLUMNS | {
    "register_id",
    "company_register_id",
    "lei",
    "recommended_action",

    "phone",

    # Old temporary naming. Keep hidden if an older DB still has it.
    "openregister_assets_eur",

    # Old shareholder integration fields.
    "relation_start_date",
    "relation_start_year",
    "main_owner_year_integrated",
}

# Columns excluded from specific sheets only (unlike DISPLAY_EXCLUDED_COLUMNS,
# which hides a column everywhere). The Overview tab's current format drops
# real estate/capital amount and the "main owner"/"main UBO" detail fields -
# those single-row picks are superseded by the dynamic Shareholder/UBO
# dropdown columns and the new UBO aggregate counts.
SHEET_SPECIFIC_EXCLUDED_COLUMNS: dict[str, set[str]] = {
    "Overview": {
        "northdata_real_estate_eur",
        "openregister_real_estate_eur",
        "northdata_capital_amount_eur",
        "openregister_capital_amount_eur",

        "has_sole_owner",
        "has_representative_owner",
        "is_family_owned",
        "has_majority_owner",
        "largest_owner_percentage",

        "main_owner_name",
        "main_owner_type",
        "main_owner_percentage_share",

        "main_ubo_name",
        "main_ubo_age",
        "main_ubo_percentage_share",
        "main_ubo_max_percentage_share",
    },
}

HEADER_LABELS = {
    "id": "Row ID",
    "openregister_company_id": "Company ID",
    "company_name": "Company Name",
    "name": "Company Name",
    "legal_form": "Legal Form",
    "founding_year": "Founding Year",
    "active": "Active",
    "country": "Country",
    "register_number": "Register Number",
    "register_court": "Register Court",
    "register_type": "Register Type",
    "postal_code": "Postal Code",
    "formatted_address": "Formatted Address",
    "vat_id": "VAT ID",
    "purpose": "Northdata Business Model",

    "industry_codes": "Industry Codes",
    "northdata_wz_code": "Northdata WZ Code",
    "openregister_wz_codes": "OpenRegister WZ Code",

    "financials_date": "Northdata Financials Date",
    "openregister_financials_date": "Openregister Financials Date",

    "revenue_eur": "Revenue €",

    "northdata_revenue_eur": "Northdata Revenue €",
    "openregister_revenue_eur": "OpenRegister Revenue €",

    "northdata_employees": "Northdata Employees",
    "openregister_employees": "Openregister Employees",

    "northdata_balance_sheet_total_eur": "Northdata Balance Sheet Total €",
    "openregister_balance_sheet_total_eur": "Openregister Balance Sheet Total €",

    "northdata_net_income_eur": "Northdata Net Income €",
    "openregister_net_income_eur": "Openregister Net Income €",

    "northdata_equity_eur": "Northdata Equity €",
    "openregister_equity_eur": "Openregister Equity €",

    "northdata_cash_eur": "Northdata Cash €",
    "openregister_cash_eur": "Openregister Cash €",

    "northdata_liabilities_eur": "Northdata Liabilities €",
    "openregister_liabilities_eur": "Openregister Liabilities €",

    "northdata_real_estate_eur": "Northdata Real Estate €",
    "openregister_real_estate_eur": "Openregister Real Estate €",

    "northdata_capital_amount_eur": "Northdata Capital Amount €",
    "openregister_capital_amount_eur": "Openregister Capital Amount €",

    "number_of_owners": "No. of Shareholders",
    "natural_person_owner_count": "No. of Natural Shareholders",
    "legal_person_owner_count": "No. of Legal Shareholders",
    "youngest_owner_age": "Youngest Shareholders Age",
    "oldest_owner_age": "Oldest Shareholders Age",
    "has_sole_owner": "Has Sole Owner",
    "has_representative_owner": "Owner Managed",
    "is_family_owned": "Family Owned",
    "has_majority_owner": "Has Majority Owner",
    "largest_owner_percentage": "Largest Owner %",

    "number_of_ubos": "No. of UBOs",
    "natural_person_ubo_count": "No. of Natural UBOs",
    "legal_person_ubo_count": "No. of Legal UBOs",
    "oldest_ubo_age": "Oldest UBO Age",
    "youngest_ubo_age": "Youngest UBO Age",

    "main_owner_name": "Main Owner Name",
    "main_owner_type": "Main Owner Type",
    "main_owner_percentage_share": "Main Owner %",

    "main_ubo_name": "Main UBO Name",
    "main_ubo_age": "Main UBO Age",
    "main_ubo_percentage_share": "Main UBO %",
    "main_ubo_max_percentage_share": "Main UBO Max %",

    "claude_business_segment": "Claude Business Segment",
    "claude_business_segment_2": "Claude Assumption",
    "claude_business_model": "Claude Business Model",
    "claude_detailed_business_summary": "Detailed Claude Business Summary",
    "claude_detailed_business_segment": "Detailed Claude Business Segment",

    "fit_score": "Fit Score",
    "fit_label": "Fit Label",
    "fit_comment": "Fit Comment",

    "report_count": "Report Count",
    "latest_report_start_date": "Latest Report Start Date",
    "latest_report_end_date": "Latest Report End Date",
    "api_status": "API Status",

    "model_provider": "Model Provider",
    "model_name": "Model Name",
    "business_segment": "Claude Business Segment",
    "business_segment_2": "Claude Assumption",
    "business_model": "Claude Business Model",
    "summary": "Detailed Claude Business Summary",

    "risk_flags": "Risk Flags",
    "succession_signal": "Succession Signal",
    "financial_signal": "Financial Signal",
    "shareholder_signal": "Shareholder Signal",

    "created_at": "Created At",
    "updated_at": "Updated At",
    "enriched_at": "Enriched At",
    "retrieved_at": "Retrieved At",

    "ubo_name": "UBO Name",
    "ubo_type": "UBO Type",
    "ubo_city": "UBO City",
    "ubo_country": "UBO Country",
    "percentage_share": "Percentage Share",
    "max_percentage_share": "Max Percentage Share",

    "shareholder_name": "Shareholder Name",
    "owner_type": "Owner Type",
    "owner_city": "Owner City",
    "owner_country": "Owner Country",
    "nominal_share_eur": "Nominal Share €",
    "relation_type": "Relation Type",
    "date_of_birth": "Date of Birth",

    "website": "Website",
    "email": "Email",
    "notes": "Notes",
}

SHEET_TABLES = [
    ("Overview", "master_overview"),
    ("Companies", "companies"),
    ("Management", "company_management"),
    ("Shareholders", "shareholders"),
    ("UBO Control Chain", "company_ubos"),
    ("Company Models", "company_models"),
    ("Fit Scores", "company_fit_scores"),
    ("Search Runs", "openregister_search_runs"),
    ("Logs", "processing_logs"),
]

PREFERRED_COLUMN_ORDER = {
    "Overview": [
        "openregister_company_id",
        "company_name",
        "legal_form",
        "founding_year",
        "active",
        "country",
        "register_number",
        "register_court",
        "register_type",
        "city",
        "postal_code",
        "website",
        "email",

        "northdata_wz_code",
        "openregister_wz_codes",

        "claude_business_segment",
        "claude_business_segment_2",
        "purpose",
        "claude_business_model",
        "claude_detailed_business_summary",

        "northdata_revenue_eur",
        "openregister_revenue_eur",

        "northdata_employees",
        "openregister_employees",

        "northdata_balance_sheet_total_eur",
        "openregister_balance_sheet_total_eur",

        "northdata_net_income_eur",
        "openregister_net_income_eur",

        "northdata_equity_eur",
        "openregister_equity_eur",

        "northdata_cash_eur",
        "openregister_cash_eur",

        "northdata_liabilities_eur",
        "openregister_liabilities_eur",

        "financials_date",
        "openregister_financials_date",

        "number_of_owners",
        "natural_person_owner_count",
        "legal_person_owner_count",
        "youngest_owner_age",
        "oldest_owner_age",

        "number_of_ubos",
        "natural_person_ubo_count",
        "legal_person_ubo_count",
        "oldest_ubo_age",
        "youngest_ubo_age",

        "fit_score",
        "fit_label",
        "fit_comment",
    ],
    "Companies": [
        "openregister_company_id",
        "name",
        "legal_form",
        "founding_year",
        "active",
        "status",
        "country",
        "register_number",
        "register_court",
        "register_type",
        "city",
        "postal_code",
        "street",
        "formatted_address",
        "website",
        "email",
        "vat_id",
        "purpose",

        "northdata_wz_code",
        "openregister_wz_codes",

        "northdata_revenue_eur",
        "openregister_revenue_eur",

        "northdata_employees",
        "openregister_employees",

        "northdata_balance_sheet_total_eur",
        "openregister_balance_sheet_total_eur",

        "northdata_net_income_eur",
        "openregister_net_income_eur",

        "northdata_equity_eur",
        "openregister_equity_eur",

        "northdata_cash_eur",
        "openregister_cash_eur",

        "northdata_liabilities_eur",
        "openregister_liabilities_eur",

        "northdata_real_estate_eur",
        "openregister_real_estate_eur",

        "northdata_capital_amount_eur",
        "openregister_capital_amount_eur",
        "financials_date",
        "openregister_financials_date",

        "number_of_owners",
        "natural_person_owner_count",
        "legal_person_owner_count",
        "youngest_owner_age",
        "oldest_owner_age",
        "has_sole_owner",
        "has_representative_owner",
        "is_family_owned",
        "has_majority_owner",
        "largest_owner_percentage",

        "company_info_enriched_at",
        "financials_enriched_at",
        "ownership_enriched_at",
        "ubos_enriched_at",
        "created_at",
        "updated_at",
    ],
    "Shareholders": [
        "openregister_company_id",
        "company_name",
        "shareholder_name",
        "owner_type",
        "relation_type",
        "percentage_share",
        "nominal_share_eur",
        "age",
        "date_of_birth",
        "natural_person_full_name",
        "natural_person_first_name",
        "natural_person_last_name",
        "legal_person_name",
        "owner_city",
        "owner_country",
        "best_available",
        "api_status",
        "retrieved_at",
        "updated_at",
    ],
    "Company Models": [
        "openregister_company_id",
        "company_name",
        "website",
        "model_provider",
        "model_name",
        "business_segment",
        "business_segment_2",
        "business_model",
        "summary",
        "api_status",
        "notes",
        "created_at",
        "updated_at",
    ],
    "Financials": [
        "openregister_company_id",
        "company_name",

        "northdata_revenue_eur",
        "openregister_revenue_eur",

        "northdata_employees",
        "openregister_employees",

        "northdata_balance_sheet_total_eur",
        "openregister_balance_sheet_total_eur",

        "northdata_net_income_eur",
        "openregister_net_income_eur",

        "northdata_equity_eur",
        "openregister_equity_eur",

        "northdata_cash_eur",
        "openregister_cash_eur",

        "northdata_liabilities_eur",
        "openregister_liabilities_eur",

        "northdata_real_estate_eur",
        "openregister_real_estate_eur",

        "northdata_capital_amount_eur",
        "openregister_capital_amount_eur",

        "financials_date",
        "openregister_financials_date",

        "report_count",
        "latest_report_start_date",
        "latest_report_end_date",
        "api_status",
        "notes",
        "enriched_at",
        "updated_at",
    ],
}


def nice_sheet_header(column_name: str) -> str:
    if column_name in HEADER_LABELS:
        return HEADER_LABELS[column_name]

    label = str(column_name or "").replace("_", " ").strip().title()
    fixes = {
        " Id": " ID",
        "Id ": "ID ",
        "Id": "ID",
        "Api": "API",
        "Url": "URL",
        "Ubo": "UBO",
        "Lei": "LEI",
        "Eur": "€",
        "Vat": "VAT",
        "Json": "JSON",
        "Wz": "WZ",
    }

    for src, dst in fixes.items():
        label = label.replace(src, dst)

    return re.sub(r"\s+", " ", label).strip()


def _round_if_needed(value: Any, column_name: str | None = None) -> Any:
    if column_name in {"main_ubo_max_percentage_share", "max_percentage_share"} and value not in (None, ""):
        try:
            return round(float(value), 2)
        except Exception:
            return value
    return value


def _get_credentials() -> Credentials:
    raw = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON in Streamlit secrets.")

    info = json.loads(raw) if isinstance(raw, str) else dict(raw)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def _get_sheet_id() -> str:
    sheet_id = st.secrets.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID in Streamlit secrets.")
    return sheet_id


def _fetch_all(supabase, table_or_view: str, page_size: int = 1000, hard_cap: int = 200000) -> list[dict[str, Any]]:
    """Fetch all rows from a table/view, paginating past PostgREST's per-request cap.

    PostgREST caps a single response at 1000 rows regardless of any .limit() hint,
    so we read in explicit .range() pages until the table is exhausted. hard_cap is
    only a safety valve against runaway loops.
    """
    rows: list[dict[str, Any]] = []
    start = 0

    while len(rows) < hard_cap:
        end = start + page_size - 1

        res = (
            supabase.table(table_or_view)
            .select("*")
            .range(start, end)
            .execute()
        )

        batch = getattr(res, "data", None) or []
        if not batch:
            break

        rows.extend(batch)

        if len(batch) < page_size:
            break

        start += page_size

    return rows


def _fetch_financials_sheet_rows(supabase) -> list[dict[str, Any]]:
    companies = _fetch_all(supabase, "companies")
    financials = _fetch_all(supabase, "company_financials")
    fin_by_company = {row.get("openregister_company_id"): row for row in financials}

    rows: list[dict[str, Any]] = []

    for company in companies:
        company_id = company.get("openregister_company_id")
        fin = fin_by_company.get(company_id, {}) or {}

        rows.append({
            "company_register_id": company.get("register_id"),
            "openregister_company_id": company_id,
            "company_name": company.get("name"),

            "northdata_revenue_eur": company.get("northdata_revenue_eur"),
            "openregister_revenue_eur": company.get("openregister_revenue_eur"),

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

            "report_count": fin.get("report_count"),
            "latest_report_start_date": fin.get("latest_report_start_date"),
            "latest_report_end_date": fin.get("latest_report_end_date"),
            "api_status": fin.get("api_status"),
            "notes": fin.get("notes"),
            "enriched_at": fin.get("enriched_at"),
            "updated_at": fin.get("updated_at"),
        })

    return rows


def _get_or_create_worksheet(spreadsheet, title: str, rows: int = 1000, cols: int = 30):
    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


def _sheet_id(worksheet) -> int | None:
    return getattr(worksheet, "id", None) or getattr(worksheet, "_properties", {}).get("sheetId")


def _clear_values(worksheet) -> None:
    worksheet.clear()


def _column_letter(index_1_based: int) -> str:
    letters = ""
    n = index_1_based

    while n:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters

    return letters


def _numeric_format_requests(sheet_id: int, columns: list[str]) -> list[dict[str, Any]]:
    integer_columns = {
        "founding_year",
        "northdata_employees",
        "openregister_employees",
        "number_of_owners",
        "natural_person_owner_count",
        "legal_person_owner_count",
        "youngest_owner_age",
        "oldest_owner_age",
        "number_of_ubos",
        "natural_person_ubo_count",
        "legal_person_ubo_count",
        "youngest_ubo_age",
        "oldest_ubo_age",
        "main_ubo_age",
        "age",
        "report_count",
        "fit_score",
        "requested_max_companies",
        "returned_companies",
        "saved_companies",
        "skipped_existing_companies",
    }

    decimal_columns = {
        "revenue_eur",
        "openregister_revenue_eur",
        "northdata_revenue_eur",
        "northdata_balance_sheet_total_eur",
        "openregister_balance_sheet_total_eur",
        "northdata_net_income_eur",
        "openregister_net_income_eur",
        "northdata_equity_eur",
        "openregister_equity_eur",
        "northdata_cash_eur",
        "openregister_cash_eur",
        "northdata_liabilities_eur",
        "openregister_liabilities_eur",
        "northdata_real_estate_eur",
        "openregister_real_estate_eur",
        "northdata_capital_amount_eur",
        "openregister_capital_amount_eur",
        "largest_owner_percentage",
        "main_owner_percentage_share",
        "main_ubo_percentage_share",
        "main_ubo_max_percentage_share",
        "nominal_share_eur",
        "percentage_share",
        "max_percentage_share",
    }

    requests: list[dict[str, Any]] = []

    for idx, col in enumerate(columns):
        if col not in integer_columns and col not in decimal_columns:
            continue

        pattern = "0" if col in integer_columns else "#,##0.##"

        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "startColumnIndex": idx,
                    "endColumnIndex": idx + 1,
                },
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        })

    return requests


def _apply_sheet_formatting(worksheet, columns: list[str], row_count: int) -> None:
    sheet_id = _sheet_id(worksheet)
    if sheet_id is None or not columns:
        return

    column_count = len(columns)
    _ = row_count

    requests: list[dict[str, Any]] = [
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id},
                "cell": {"userEnteredFormat": {}},
                "fields": "userEnteredFormat",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 0}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {"clearBasicFilter": {"sheetId": sheet_id}},
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": column_count,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 0.1098, "green": 0.3608, "blue": 0.3608},
                        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                        "horizontalAlignment": "CENTER",
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
            }
        },
    ]

    requests.extend(_numeric_format_requests(sheet_id, columns))

    try:
        worksheet.spreadsheet.batch_update({"requests": requests})
    except Exception:
        pass


def _delete_worksheet_if_exists(spreadsheet, title: str) -> None:
    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return

    try:
        spreadsheet.del_worksheet(worksheet)
    except Exception:
        pass


def _drop_sheet_excluded_columns(rows: list[dict[str, Any]], sheet_name: str) -> list[dict[str, Any]]:
    if not rows:
        return rows

    excluded = DISPLAY_EXCLUDED_COLUMNS | SHEET_SPECIFIC_EXCLUDED_COLUMNS.get(sheet_name, set())

    cleaned: list[dict[str, Any]] = []

    for row in rows:
        cleaned.append({k: v for k, v in row.items() if k not in excluded})

    return cleaned


def _order_columns_for_sheet(rows: list[dict[str, Any]], sheet_name: str) -> list[dict[str, Any]]:
    if not rows:
        return rows

    preferred = PREFERRED_COLUMN_ORDER.get(sheet_name)
    if not preferred:
        return rows

    ordered_rows: list[dict[str, Any]] = []

    for row in rows:
        ordered: dict[str, Any] = {}

        for col in preferred:
            if col in row:
                ordered[col] = row[col]

        for col, value in row.items():
            if col not in ordered:
                ordered[col] = value

        ordered_rows.append(ordered)

    return ordered_rows


def _safe_sheet_cell(value: Any, *, column_name: str | None = None) -> Any:
    if column_name in {"industry_codes", "openregister_wz_codes"}:
        value = format_industry_codes(value)
    else:
        value = flatten_for_sheet(value)

    value = _round_if_needed(value, column_name)

    if isinstance(value, str) and len(value) > MAX_SHEET_CELL_CHARS:
        return value[:MAX_SHEET_CELL_CHARS] + "… [truncated for Google Sheets cell limit; full value stays in Supabase]"

    return value


def _write_rows(worksheet, rows: list[dict[str, Any]], *, sheet_name: str) -> int:
    _clear_values(worksheet)

    rows = _drop_sheet_excluded_columns(rows, sheet_name)
    rows = _order_columns_for_sheet(rows, sheet_name)

    if not rows:
        worksheet.update([["No rows"]], value_input_option="USER_ENTERED")
        return 0

    df = pd.DataFrame(rows)

    for col in df.columns:
        df[col] = df[col].map(lambda value, col=col: _safe_sheet_cell(value, column_name=col))

    headers = [nice_sheet_header(col) for col in df.columns.tolist()]
    values = [headers] + df.astype(object).where(pd.notnull(df), "").values.tolist()

    # Ensure the worksheet grid is large enough to hold everything, otherwise
    # gspread's update() silently truncates to the existing grid size (new sheets
    # default to only 1000 rows).
    needed_rows = len(values) + 10
    needed_cols = max(len(headers), 1)
    try:
        if worksheet.row_count < needed_rows or worksheet.col_count < needed_cols:
            worksheet.resize(rows=max(worksheet.row_count, needed_rows), cols=max(worksheet.col_count, needed_cols))
    except Exception:
        pass

    worksheet.update(values, value_input_option="USER_ENTERED")
    _apply_sheet_formatting(worksheet, df.columns.tolist(), len(rows))

    return len(rows)


def sync_supabase_to_google_sheets(supabase) -> dict[str, int]:
    credentials = _get_credentials()
    gc = gspread.authorize(credentials)
    spreadsheet = gc.open_by_key(_get_sheet_id())

    _delete_worksheet_if_exists(spreadsheet, "UBOs")

    counts: dict[str, int] = {}

    for sheet_name, table in SHEET_TABLES:
        rows = _fetch_all(supabase, table)
        ws = _get_or_create_worksheet(spreadsheet, sheet_name)
        counts[sheet_name] = _write_rows(ws, rows, sheet_name=sheet_name)

        if sheet_name == "Companies":
            fin_ws = _get_or_create_worksheet(spreadsheet, "Financials")
            counts["Financials"] = _write_rows(
                fin_ws,
                _fetch_financials_sheet_rows(supabase),
                sheet_name="Financials",
            )

    return counts
