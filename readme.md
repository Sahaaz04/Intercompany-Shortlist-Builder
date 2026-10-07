Intercompany Shortlist Builder

A Streamlit application for building and enriching a shortlist of companies relevant for succession / acquisition opportunities.

The application combines company data from NorthData and OpenRegister, enriches that data with ownership, UBO, management and financial information, uses Anthropic Claude to analyze the company's business model and fit, stores the results in Supabase, and can sync the resulting dataset to Google Sheets or generate a filtered Excel workbook.

What the application does

1. Import company data

The app accepts Excel workbooks from two sources:

NorthData — each row is matched against OpenRegister and, when matched, is stored using the real OpenRegister company ID.

OpenRegister — the company ID contained in the uploaded file is used directly, so no matching step is required.

Both sources can be uploaded together, independently, or not at all when you only want to re-run enrichment or fit scoring for companies that are already in the database.

2. Enrich company data

For each company, the enrichment pipeline can retrieve and store:

additional company / management information

OpenRegister financial information

shareholders

UBOs (ultimate beneficial owners)

an AI-generated business model / business summary using Claude

Existing enrichment can be overwritten deliberately, but doing so re-runs external API calls and may consume additional API credits.

3. Claude fit scoring

Claude evaluates the available company information against configurable succession-target criteria such as:

revenue

employee count

net income

shareholder age

UBO age

preferred business type

preferred industries

additional scoring instructions

The resulting score includes a fit score, label, comment, succession / financial / shareholder signals, risk flags and a recommended action.

The scoring criteria guide the model; they are not used as a hard database filter.

4. Google Sheets sync

The application can synchronize the backend data into Google Sheets. The repository also contains a Google Apps Script under superbase/appscript which adds spreadsheet-side functionality such as dropdown setup, overview formulas, cockpit setup and formatting helpers.

5. Filtered workbook export

The final page can create a downloadable Excel workbook from the backend using filters for:

legal form

NorthData WZ code

OpenRegister WZ code

The generated workbook contains the relevant company data and related sheets for the selected companies.

Application flow

The Streamlit UI is organized as a four-step flow:

1. Description
      ↓
2. Configuration
      ↓
3. Import + Enrichment + Claude Fit Scoring
      ↓
4. Google Sheets Sync / Filtered Workbook Export

The backend data is persisted in Supabase, so later runs can work with companies that have already been imported.

Architecture

                    ┌──────────────────────┐
                    │      Streamlit UI    │
                    │        app.py        │
                    └──────────┬───────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
              ▼                ▼                ▼
        NorthData         OpenRegister       Claude
         imports          imports/enrich.   AI analysis
              │                │                │
              └────────────────┼────────────────┘
                               ▼
                         ┌─────────────┐
                         │  Supabase   │
                         │  PostgreSQL │
                         └──────┬──────┘
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
              Google Sheets          Filtered XLSX
                  sync                  export

Main modules

Module

Purpose

app.py

Streamlit application and user workflow

modules/northdata_import.py

Imports and matches NorthData Excel data against OpenRegister

modules/openregister_import.py

Imports OpenRegister Excel data using the supplied company ID

modules/openregister_enrichment.py

Retrieves additional OpenRegister company, financial, ownership, UBO and management data

modules/claude_business_model.py

Website/context collection and Claude business-model analysis

modules/fit_scoring.py

Claude-based succession / acquisition fit scoring

modules/google_sheets_sync.py

Synchronizes backend data into Google Sheets

modules/filtered_workbook_export.py

Builds filtered Excel workbooks

modules/openregister_search.py

OpenRegister company-search/filter helper functions

modules/openregister_client.py

OpenRegister SDK client creation

modules/supabase_client.py

Supabase client creation

modules/utils.py

Shared parsing, formatting and helper functions

Repository structure

.
├── app.py
├── requirements.txt
├── .streamlit/
│   ├── config.toml
│   └── secrets.toml.example
├── .devcontainer/
│   └── devcontainer.json
├── modules/
│   ├── claude_business_model.py
│   ├── filtered_workbook_export.py
│   ├── fit_scoring.py
│   ├── google_sheets_sync.py
│   ├── northdata_import.py
│   ├── openregister_client.py
│   ├── openregister_enrichment.py
│   ├── openregister_import.py
│   ├── openregister_search.py
│   ├── supabase_client.py
│   └── utils.py
└── superbase/
    ├── schema.sql
    ├── appscript
    └── query* files

superbase/ consists all superbase queries and appscript code. there are in this repository for record purpose and not directly linked to either superbase or appscipt extension on spreadsheets.
