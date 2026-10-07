from __future__ import annotations

import json
import os
import re

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from modules.claude_business_model import run_claude_business_model_enrichment
from modules.filtered_workbook_export import (
    build_filtered_workbook_bytes,
    fetch_all_rows_paginated,
)
from modules.fit_scoring import DEFAULT_FIT_CONFIG, run_fit_scoring
from modules.google_sheets_sync import sync_supabase_to_google_sheets
from modules.northdata_import import run_northdata_import
from modules.openregister_enrichment import run_enrichment
from modules.openregister_import import run_openregister_import
from modules.supabase_client import get_supabase_client
from modules.utils import format_industry_codes, parse_csv_values

st.set_page_config(page_title="Succession Analysis OpenRegister", page_icon="📊", layout="wide")

GOOGLE_SHEET_URL = "https://docs.google.com/spreadsheets/d/1HRXTjV2aUN6-QCuZBb-MpZJEzI0BkLeUXHoYJ_n7oJA/edit?gid=1105111803#gid=1105111803"

APPSCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "superbase", "appscript")


def _load_appscript_source() -> str | None:
    try:
        with open(APPSCRIPT_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _render_copy_appscript_button() -> None:
    source = _load_appscript_source()

    if not source:
        st.warning("Could not load the Apps Script source file to copy.")
        return

    source_json = json.dumps(source).replace("</", "<\\/")

    html_out = f"""
    <div style="font-family: 'Source Sans Pro', sans-serif;">
      <button id="copy-appscript-btn" style="
          background-color:#1c5c5c;
          color:#ffffff;
          border:none;
          border-radius:6px;
          padding:0.5em 1em;
          font-weight:600;
          cursor:pointer;
          font-size:14px;
      ">Copy Apps Script code</button>
    </div>
    <script>
      const appscriptSource = {source_json};
      const btn = document.getElementById('copy-appscript-btn');
      btn.addEventListener('click', async () => {{
          try {{
              await navigator.clipboard.writeText(appscriptSource);
          }} catch (err) {{
              const ta = document.createElement('textarea');
              ta.value = appscriptSource;
              ta.style.position = 'fixed';
              ta.style.opacity = '0';
              document.body.appendChild(ta);
              ta.select();
              document.execCommand('copy');
              document.body.removeChild(ta);
          }}
          btn.innerText = 'Copied!';
          setTimeout(() => {{ btn.innerText = 'Copy Apps Script code'; }}, 2000);
      }});
    </script>
    """

    components.html(html_out, height=50)


def description_tab():
    st.header("Intercompany Shortlist Builder")

    st.markdown(
        """
### Functionalities

You bring in a list of companies from either or both of these two sources — **NorthData** and **OpenRegister**.
Once you upload them, they and their information are added on the backend.

Once companies are imported, you can enrich each one with additional information:

- **Shareholders & UBOs** — who owns the company, how old they are, and how much of it they own.
- **Business model** — a short plain-English summary of what the company actually does, written by an AI
  assistant based on their website or other information provided.
- **Fit score** — the same AI assistant reads all of the above and gives each company a score, a label,
  and a one-line comment on how good a succession target it is.

### What you get out

Once you are done with import, enrichment and fit scoring, you can sync the backend to Google Sheets and
see the output there.

### Filtered Workbook

You can also create a custom workbook from the database based on Industry filters.
        """
    )


def configuration_tab():
    st.header("Configuration")

    st.session_state.setdefault("openregister_api_key", "")
    st.session_state.setdefault("claude_api_key", "")
    st.session_state.setdefault("claude_model_name", "claude-sonnet-4-5")

    st.session_state["openregister_api_key"] = st.text_input(
        "OpenRegister API key",
        type="password",
        value=st.session_state["openregister_api_key"],
    )
    st.session_state["claude_api_key"] = st.text_input(
        "Claude / Anthropic API key",
        type="password",
        value=st.session_state["claude_api_key"],
    )
    st.session_state["claude_model_name"] = st.text_input(
        "Claude model",
        value=st.session_state["claude_model_name"],
    )

    st.caption("Add OpenRegister and Anthropic API key to use enrichment features.")


def import_and_enrichment_tab(supabase):
    openregister_api_key = st.session_state.get("openregister_api_key", "")
    claude_api_key = st.session_state.get("claude_api_key", "")
    claude_model_name = st.session_state.get("claude_model_name", "claude-sonnet-4-5")

    if not openregister_api_key or not claude_api_key:
        st.info("Add your OpenRegister and Anthropic API keys on the Configuration page to use enrichment features.")

    st.header("Import + Enrichment")

    st.subheader("Import")
    st.caption(
        "Upload a NorthData file, an OpenRegister file, both, or neither "
        "(to just run enrichment/fit scoring again on what's already saved). "
        "Once you upload them, the companies there will go through the enrichment process. "
        "If you leave max companies to process empty it will go through all companies. "
        "Add a max number of rows if you want to set a limit."
    )

    c1, c2 = st.columns(2)

    with c1:
        st.markdown("**NorthData**")
        st.caption(
            "Each row is matched to OpenRegister first. Only matched companies are "
            "inserted or updated using the real OpenRegister company ID. Requires an API key."
        )

        northdata_file = st.file_uploader(
            "Upload NorthData Excel file",
            type=["xlsx"],
            help="Only .xlsx files are supported.",
            key="northdata_upload",
        )

        northdata_max_rows = st.number_input(
            "Max NorthData rows to process",
            min_value=0,
            value=None,
            step=10,
            placeholder="Leave blank to process all",
            key="northdata_max_rows",
        )

    with c2:
        st.markdown("**OpenRegister**")
        st.caption(
            "The file's own company ID is used directly - no OpenRegister search or "
            "matching needed, and no API key required."
        )

        openregister_file = st.file_uploader(
            "Upload OpenRegister Excel file",
            type=["xlsx"],
            help="Only .xlsx files are supported.",
            key="openregister_upload",
        )

        openregister_max_rows = st.number_input(
            "Max OpenRegister rows to process",
            min_value=0,
            value=None,
            step=10,
            placeholder="Leave blank to process all",
            key="openregister_max_rows",
        )

    for label, f in [("NorthData", northdata_file), ("OpenRegister", openregister_file)]:
        if f is None:
            continue
        try:
            f.seek(0)
            preview_df = pd.read_excel(f, engine="openpyxl").head(20)
            f.seek(0)

            st.caption(f"{label} preview")
            st.dataframe(preview_df, use_container_width=True)
        except Exception as exc:
            st.error(f"Could not read {label} Excel file: {exc}")

    st.divider()

    st.subheader("Enrichment")
    st.caption(
        "These are used to enrich the company information. Tick OpenRegister Financials and Additional company details if you want "
        "to have Additional financial and management information, or else leave it unchecked to save API call credit cost."
    )

    fetch_management = st.checkbox("Additional Company Details", value=True)
    st.caption("Additional management information.")

    fetch_financials = st.checkbox("Openregister Financials", value=False)
    st.caption(
        "Financial information of companies from OpenRegister. You can use this if you are uploading a "
        "NorthData file and want to have Additional OpenRegister financial data for its companies."
    )

    fetch_ownership = st.checkbox("Shareholders", value=True)
    st.caption("Detailed shareholder information for companies.")

    fetch_ubos = st.checkbox("UBOs", value=True)
    st.caption(
        "Ultimate Beneficiary Owner - provides estimated information about a company's true natural "
        "ownership in cases where they have legal shareholders."
    )

    fetch_claude_business_model = st.checkbox("Claude Business Model", value=True)
    st.caption("AI assistant provides a detailed description about the company's work.")

    update_existing_enrichment = st.checkbox("OVERWRITE existing enrichment (READ THE DESCRIPTION)", value=False)
    st.caption(
        
        "Tick this to re-run the enrichment types selected above for ALL companies and "
        "overwrite the current information in the backend. Note: this re-runs the API calls "
        "for every company, so it consumes API credits and is very expensive. only use when it is required to update the exisiting enrichment"
    )

    st.divider()

    st.subheader("Claude Fit Scoring")
    st.caption(
        "The AI assistant reads all imported and enriched company data — revenue, employees, net income, "
        "shareholders, UBOs and the business model — and compares it against the target criteria below. "
        "It returns a fit score, a label, a short comment, and a recommended action for each company. "
        "The ranges below tell the assistant what an ideal succession target looks like; they guide the "
        "score but do not hard-filter companies out."
    )

    r1c1, r1c2 = st.columns(2)
    with r1c1:
        revenue_min = st.number_input(
            "Revenue min EUR",
            min_value=0.0,
            value=float(DEFAULT_FIT_CONFIG["revenue_min"]),
            step=100000.0,
        )
    with r1c2:
        revenue_max = st.number_input(
            "Revenue max EUR",
            min_value=0.0,
            value=float(DEFAULT_FIT_CONFIG["revenue_max"]),
            step=100000.0,
        )

    r2c1, r2c2 = st.columns(2)
    with r2c1:
        employees_min = st.number_input(
            "Employees min",
            min_value=0,
            value=int(DEFAULT_FIT_CONFIG["employees_min"]),
            step=1,
        )
    with r2c2:
        employees_max = st.number_input(
            "Employees max",
            min_value=0,
            value=int(DEFAULT_FIT_CONFIG["employees_max"]),
            step=1,
        )

    r3c1, r3c2 = st.columns(2)
    with r3c1:
        net_income_min = st.number_input(
            "Net income min EUR",
            value=float(DEFAULT_FIT_CONFIG["net_income_min"]),
            step=100000.0,
        )
    with r3c2:
        net_income_max = st.number_input(
            "Net income max EUR",
            value=float(DEFAULT_FIT_CONFIG["net_income_max"]),
            step=100000.0,
        )

    r4c1, r4c2 = st.columns(2)
    with r4c1:
        min_shareholder_age = st.number_input(
            "Shareholder age min",
            min_value=0,
            value=int(DEFAULT_FIT_CONFIG["min_shareholder_age"]),
            step=1,
        )
    with r4c2:
        min_ubo_age = st.number_input(
            "UBO age min",
            min_value=0,
            value=int(DEFAULT_FIT_CONFIG["min_ubo_age"]),
            step=1,
        )

    preferred_business_type = st.text_input(
        "Preferred business type",
        value=str(DEFAULT_FIT_CONFIG["preferred_business_type"]),
    )
    preferred_industries = st.text_input(
        "Preferred industries",
        value=str(DEFAULT_FIT_CONFIG["preferred_industries"]),
    )
    additional_instructions = st.text_area(
        "Additional scoring instructions",
        value=str(DEFAULT_FIT_CONFIG["additional_instructions"]),
        height=120,
    )

    update_existing_fit_scoring = st.checkbox("OVERWRITE existing claude fitscoring", value=False)
    st.caption(
        "Tick this to re-run the fit scoring for ALL companies and overwrite the current "
        "information in the backend.  only use when it is required to update the exisiting fit scores."
    )

    if st.button("Import and Enrich", type="primary"):
        if revenue_min > revenue_max and revenue_max > 0:
            st.error("Revenue minimum cannot be greater than maximum.")
            return

        if employees_min > employees_max and employees_max > 0:
            st.error("Minimum employees cannot be greater than maximum employees.")
            return

        if net_income_min > net_income_max and net_income_max != 0:
            st.error("Net income minimum cannot be greater than maximum.")
            return

        needs_openregister = bool(northdata_file) or fetch_management or fetch_financials or fetch_ownership or fetch_ubos

        if needs_openregister and not openregister_api_key:
            st.error("Please paste your OpenRegister API key in the previous page first.")
            return

        if not claude_api_key:
            st.error("Please paste your Claude / Anthropic API key in the previous page first.")
            return

        if northdata_file is None and openregister_file is None:
            st.info("No files uploaded - running enrichment and fit scoring on companies already saved.")

        if northdata_file is not None:
            northdata_file.seek(0)

            with st.spinner("Importing NorthData rows and matching OpenRegister IDs..."):
                result = run_northdata_import(
                    uploaded_file=northdata_file,
                    openregister_api_key=openregister_api_key,
                    supabase=supabase,
                    max_rows=int(northdata_max_rows) if northdata_max_rows and northdata_max_rows > 0 else None,
                )

            st.success(
                f"NorthData import finished. "
                f"Imported {result['imported']}, updated {result['updated']}, "
                f"skipped {result['skipped']}, errors {result['errors']}, "
                f"parse-warning rows {result.get('rows_with_parse_warnings', 0)}."
            )

            if result.get("results"):
                st.caption("NorthData row results")
                st.dataframe(pd.DataFrame(result["results"]), use_container_width=True)

        if openregister_file is not None:
            openregister_file.seek(0)

            with st.spinner("Importing OpenRegister rows..."):
                result = run_openregister_import(
                    uploaded_file=openregister_file,
                    supabase=supabase,
                    max_rows=int(openregister_max_rows) if openregister_max_rows and openregister_max_rows > 0 else None,
                )

            st.success(
                f"OpenRegister import finished. "
                f"Imported {result['imported']}, updated {result['updated']}, "
                f"skipped {result['skipped']}, errors {result['errors']}."
            )

            if result.get("results"):
                st.caption("OpenRegister row results")
                st.dataframe(pd.DataFrame(result["results"]), use_container_width=True)

        if fetch_management or fetch_financials or fetch_ownership or fetch_ubos:
            with st.spinner("Running OpenRegister enrichment..."):
                enrichment_progress = st.empty()

                def _update_enrichment_progress(done: int, total: int) -> None:
                    enrichment_progress.markdown(f"⏳ OpenRegister enrichment: **{done}/{total}**")

                enrichment_result = run_enrichment(
                    api_key=openregister_api_key,
                    supabase=supabase,
                    update_existing=update_existing_enrichment,
                    fetch_company_info=False,
                    fetch_company_details_fill=False,
                    fetch_financials=fetch_financials,
                    fetch_ownership=fetch_ownership,
                    fetch_ubos=fetch_ubos,
                    fetch_management=fetch_management,
                    progress_callback=_update_enrichment_progress,
                )

                enrichment_progress.markdown(
                    f"✅ OpenRegister enrichment: **{enrichment_result['companies_seen']}/{enrichment_result['companies_seen']}**"
                )

            st.success(f"OpenRegister enrichment finished for {enrichment_result['companies_seen']} backend companies.")

            if enrichment_result["results"]:
                st.dataframe(pd.DataFrame(enrichment_result["results"]), use_container_width=True)

        if fetch_claude_business_model:
            with st.spinner("Running Claude business model enrichment..."):
                claude_progress = st.empty()

                def _update_claude_progress(done: int, total: int) -> None:
                    claude_progress.markdown(f"⏳ Claude business model enrichment: **{done}/{total}**")

                claude_result = run_claude_business_model_enrichment(
                    supabase=supabase,
                    claude_api_key=claude_api_key,
                    model_name=claude_model_name,
                    update_existing=update_existing_enrichment,
                    progress_callback=_update_claude_progress,
                )

                claude_progress.markdown(
                    f"✅ Claude business model enrichment: **{claude_result['companies_seen']}/{claude_result['companies_seen']}**"
                )

            st.success(
                f"Claude business model enrichment finished. "
                f"Processed {claude_result['processed']}, saved {claude_result['saved']}, "
                f"skipped {claude_result['skipped']}, errors {claude_result['errors']}."
            )

            if claude_result["results"]:
                st.dataframe(pd.DataFrame(claude_result["results"]), use_container_width=True)

        fit_config = {
            "revenue_min": revenue_min,
            "revenue_max": revenue_max,
            "employees_min": employees_min,
            "employees_max": employees_max,
            "net_income_min": net_income_min,
            "net_income_max": net_income_max,
            "min_shareholder_age": min_shareholder_age,
            "min_ubo_age": min_ubo_age,
            "preferred_business_type": preferred_business_type,
            "preferred_industries": preferred_industries,
            "additional_instructions": additional_instructions,
        }

        with st.spinner("Running Claude fit scoring..."):
            fit_progress = st.empty()

            def _update_fit_progress(done: int, total: int) -> None:
                fit_progress.markdown(f"⏳ Fit scoring: **{done}/{total}**")

            fit_result = run_fit_scoring(
                supabase=supabase,
                claude_api_key=claude_api_key,
                model_name=claude_model_name,
                fit_config=fit_config,
                update_existing=update_existing_fit_scoring,
                progress_callback=_update_fit_progress,
            )

            fit_progress.markdown(f"✅ Fit scoring: **{fit_result['processed']}/{fit_result['processed']}**")

        st.success(
            f"Fit scoring finished. Scored {fit_result['scored']}, "
            f"skipped {fit_result['skipped']}, errors {fit_result['errors']}."
        )

        if fit_result["results"]:
            st.dataframe(pd.DataFrame(fit_result["results"]), use_container_width=True)


def _filter_dataframe_for_export(df: pd.DataFrame, filters: dict) -> pd.DataFrame:
    if df.empty:
        return df

    def contains_any(series: pd.Series, values: list[str]) -> pd.Series:
        mask = pd.Series(False, index=series.index)

        for value in values:
            text = str(value or "").strip()

            if not text:
                continue

            mask = mask | series.fillna("").astype(str).str.contains(
                text,
                case=False,
                na=False,
                regex=False,
            )

        return mask

    def exact_code_match_any(series: pd.Series, values: list[str]) -> pd.Series:
        terms = {str(v).strip().lower() for v in values if str(v or "").strip()}

        def row_matches(text) -> bool:
            tokens = re.split(r"[,\s]+", str(text or ""))
            tokens = {t.strip().lower() for t in tokens if t.strip()}
            return bool(tokens & terms)

        return series.apply(row_matches)

    legal_form_terms = filters.get("legal_form_terms") or []

    if legal_form_terms and "legal_form" in df.columns:
        df = df[contains_any(df["legal_form"], legal_form_terms)]

    wz_terms = [str(t).strip() for t in (filters.get("wz_terms") or []) if str(t or "").strip()]

    if wz_terms:
        wz_mode = filters.get("wz_search_mode") or "NorthData WZ Code"

        northdata_match = pd.Series(False, index=df.index)
        if "northdata_wz_code" in df.columns:
            northdata_match = exact_code_match_any(df["northdata_wz_code"], wz_terms)

        openregister_match = pd.Series(False, index=df.index)
        if "openregister_wz_codes" in df.columns:
            flattened = df["openregister_wz_codes"].map(format_industry_codes)
            openregister_match = exact_code_match_any(flattened, wz_terms)

        if wz_mode == "NorthData WZ Code":
            df = df[northdata_match]
        elif wz_mode == "OpenRegister WZ Code":
            df = df[openregister_match]
        else:
            df = df[northdata_match | openregister_match]

    for item in filters.get("ranges", []):
        column = item["column"]
        min_value = item.get("min")
        max_value = item.get("max")

        if column not in df.columns:
            continue

        series = pd.to_numeric(df[column], errors="coerce")

        if min_value is not None:
            df = df[series >= min_value]
            series = pd.to_numeric(df[column], errors="coerce")

        if max_value is not None:
            df = df[series <= max_value]

    shareholder_age_min = filters.get("shareholder_age_min")
    shareholder_age_max = filters.get("shareholder_age_max")

    if shareholder_age_min is not None and "youngest_owner_age" in df.columns:
        youngest = pd.to_numeric(df["youngest_owner_age"], errors="coerce")
        df = df[youngest >= shareholder_age_min]

    if shareholder_age_max is not None and "oldest_owner_age" in df.columns:
        oldest = pd.to_numeric(df["oldest_owner_age"], errors="coerce")
        df = df[oldest <= shareholder_age_max]

    return df


def filtered_export_tab(supabase):
    st.header("Filtered Workbook Export")
    st.caption(
        "Generate a downloadable Excel workbook from filtered backend data, based on legal form "
        "and industry (WZ) code."
    )

    with st.form("filtered_export_form"):
        legal_forms_text = st.text_input(
            "Legal forms contains",
            placeholder="Example: gmbh or gmbh, kg, ag",
        )

        wz_search_mode_label = st.selectbox(
            "Industry code (WZ) search based on",
            ["Both (openregister + northdata wz code)", "NorthData WZ Code", "OpenRegister WZ Code"],
            key="export_wz_search_mode",
        )
        wz_text = st.text_input(
            "Industry code contains",
            placeholder="Example: 10.69 or 10.67, 11.51, 12",
            key="export_wz_text",
        )

        submitted = st.form_submit_button("Generate filtered workbook", type="primary")

    if submitted:
        try:
            rows = fetch_all_rows_paginated(supabase, "master_overview")
            df = pd.DataFrame(rows)

            if df.empty:
                st.warning("No data found in master_overview.")
                return

            filters = {
                "legal_form_terms": parse_csv_values(legal_forms_text),
                "wz_terms": parse_csv_values(wz_text),
                "wz_search_mode": wz_search_mode_label,
            }

            filtered = _filter_dataframe_for_export(df, filters)

            if filtered.empty:
                st.warning("No companies matched the selected filters.")
                return

            sort_cols = [c for c in ["company_name", "register_id"] if c in filtered.columns]

            if sort_cols:
                filtered = filtered.sort_values(by=sort_cols)

            register_ids = list(dict.fromkeys(filtered["register_id"].dropna().astype(str).tolist()))

            export_result = build_filtered_workbook_bytes(
                supabase,
                register_ids=register_ids,
                overview_rows=filtered.to_dict("records"),
            )

            st.success(f"Filtered workbook created for {len(register_ids)} companies.")
            st.write("Rows per sheet:")
            st.json(export_result["table_counts"])

            st.download_button(
                "Download filtered workbook",
                data=export_result["workbook_bytes"],
                file_name="filtered_openregister_workbook.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        except Exception as exc:
            st.error("Filtered export failed.")
            st.exception(exc)

    st.divider()
    _render_copy_appscript_button()
    st.markdown(
        "<div style='font-size:1.05rem; line-height:1.5; color: rgba(49,51,63,0.6);'>"
        "For it to function in a similar way you will need to load the downloaded workbook in Google Spreadsheet and paste "
        "the Apps Script code which you can copy on the filtered workbook page into the extension in <b>menu bar &gt; extention &gt; apps script &gt; paste &gt; save</b>, then go to "
        "<b>overview tools in menu bar &gt; run setupalldropdowns</b>."
        "</div>",
        unsafe_allow_html=True,
    )


def main():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:ital,wght@0,600;0,700;1,600;1,700&display=swap');

        .stApp {
            background: linear-gradient(120deg, #ffffff, #ffffff, #bfe0da, #ffffff);
            background-size: 300% 300%;
            animation: cokuBgDrift 14s ease-in-out infinite;
        }
        @keyframes cokuBgDrift {
            0%   { background-position: 0% 30%; }
            50%  { background-position: 100% 60%; }
            100% { background-position: 0% 30%; }
        }

        .stApp h1, .stApp h2 {
            font-family: 'Bricolage Grotesque', sans-serif;
            font-style: italic;
            font-weight: 700;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    try:
        supabase = get_supabase_client()
    except Exception as exc:
        st.error(f"Supabase connection failed: {exc}")
        st.stop()

    if "page" not in st.session_state:
        st.session_state["page"] = 0

    page = st.session_state["page"]

    if page == 0:
        description_tab()

        st.divider()
        _, col_next = st.columns([5, 1])
        with col_next:
            if st.button("Next", type="primary", use_container_width=True):
                st.session_state["page"] = 1
                st.rerun()

    elif page == 1:
        configuration_tab()

        st.divider()
        col_back, _, col_next = st.columns([1, 4, 1])
        with col_back:
            if st.button("Back", use_container_width=True):
                st.session_state["page"] = 0
                st.rerun()
        with col_next:
            if st.button("Next", type="primary", use_container_width=True):
                st.session_state["page"] = 2
                st.rerun()

    elif page == 2:
        import_and_enrichment_tab(supabase)

        st.divider()
        col_back, _, col_next = st.columns([1, 3, 2])
        with col_back:
            if st.button("Back", use_container_width=True):
                st.session_state["page"] = 1
                st.rerun()
        with col_next:
            if st.button("Sync and move to next", type="primary", use_container_width=True):
                with st.spinner("Syncing to Google Sheets..."):
                    try:
                        sync_supabase_to_google_sheets(supabase)
                        st.session_state["page"] = 3
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Google Sheets sync failed: {exc}")
            st.caption("You can use Sync and move to next if the enrichment is bugged.")

    else:
        filtered_export_tab(supabase)

        st.divider()
        col_back, _ = st.columns([1, 5])
        with col_back:
            if st.button("Back", use_container_width=True):
                st.session_state["page"] = 2
                st.rerun()


if __name__ == "__main__":
    main()
