from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from anthropic import Anthropic
from bs4 import BeautifulSoup

MAX_WEBSITE_CHARS = 24000
MAX_EXTRA_PAGES = 2
REQUEST_TIMEOUT_SECONDS = 25
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-5"

DIVISION_LABELS = {
    1: "Crop and animal production, hunting and related service activities",
    2: "Forestry and logging",
    3: "Fishing and aquaculture",
    5: "Mining of coal and lignite",
    6: "Extraction of crude petroleum and natural gas",
    7: "Mining of metal ores",
    8: "Other mining and quarrying",
    9: "Mining support service activities",
    10: "Manufacture of food products",
    11: "Manufacture of beverages",
    12: "Manufacture of tobacco products",
    13: "Manufacture of textiles",
    14: "Manufacture of wearing apparel",
    15: "Manufacture of leather and related products of other materials",
    16: "Manufacture of wood and products of wood and cork, except furniture; straw/plaiting materials",
    17: "Manufacture of paper and paper products",
    18: "Printing and reproduction of recorded media",
    19: "Manufacture of coke and refined petroleum products",
    20: "Manufacture of chemicals and chemical products",
    21: "Manufacture of basic pharmaceutical products and pharmaceutical preparations",
    22: "Manufacture of rubber and plastic products",
    23: "Manufacture of other non-metallic mineral products",
    24: "Manufacture of basic metals",
    25: "Manufacture of fabricated metal products, except machinery and equipment",
    26: "Manufacture of computer, electronic and optical products",
    27: "Manufacture of electrical equipment",
    28: "Manufacture of machinery and equipment n.e.c.",
    29: "Manufacture of motor vehicles, trailers and semi-trailers",
    30: "Manufacture of other transport equipment",
    31: "Manufacture of furniture",
    32: "Other manufacturing",
    33: "Repair, maintenance and installation of machinery and equipment",
    35: "Electricity, gas, steam and air conditioning supply",
    36: "Water collection, treatment and supply",
    37: "Sewerage",
    38: "Waste collection, recovery and disposal activities",
    39: "Remediation activities and other waste management service activities",
    41: "Construction of residential and non-residential buildings",
    42: "Civil engineering",
    43: "Specialised construction activities",
    46: "Wholesale trade",
    47: "Retail trade",
    49: "Land transport and transport via pipelines",
    50: "Water transport",
    51: "Air transport",
    52: "Warehousing, storage and support activities for transportation",
    53: "Postal and courier activities",
    55: "Accommodation",
    56: "Food and beverage service activities",
    58: "Publishing activities",
    59: "Motion picture, video and television programme production, sound recording and music publishing",
    60: "Programming, broadcasting, news agency and other content distribution activities",
    61: "Telecommunication",
    62: "Computer programming, consultancy and related activities",
    63: "Computing infrastructure, data processing, hosting and other information service activities",
    64: "Financial service activities, except insurance and pension funding",
    65: "Insurance, reinsurance and pension funding, except compulsory social security",
    66: "Activities auxiliary to financial services and insurance activities",
    68: "Real estate activities",
    69: "Legal and accounting activities",
    70: "Activities of head offices and management consultancy",
    71: "Architectural and engineering activities; technical testing and analysis",
    72: "Scientific research and development",
    73: "Activities of advertising, market research and public relations",
    74: "Other professional, scientific and technical activities",
    75: "Veterinary activities",
    77: "Rental and leasing activities",
    78: "Employment activities",
    79: "Travel agency, tour operator and other reservation service and related activities",
    80: "Investigation and security activities",
    81: "Services to buildings and landscape activities",
    82: "Office administrative, office support and other business support activities",
    84: "Public administration and defence; compulsory social security",
    85: "Education",
    86: "Human health activities",
    87: "Residential care activities",
    88: "Social work activities without accommodation",
    90: "Arts creation and performing arts activities",
    91: "Libraries, archives, museums and other cultural activities",
    92: "Gambling and betting activities",
    93: "Sports activities and amusement and recreation activities",
    94: "Activities of membership organisations",
    95: "Repair and maintenance of computers, personal and household goods, and motor vehicles/motorcycles",
    96: "Personal service activities",
    97: "Activities of households as employers of domestic personnel",
    98: "Undifferentiated goods- and service-producing activities of private households for own use",
    99: "Activities of extraterritorial organisations and bodies",
}

ALLOWED_DIVISION_LABELS = set(DIVISION_LABELS.values())
CASE_INSENSITIVE_DIVISION_MAP = {
    label.lower(): label
    for label in ALLOWED_DIVISION_LABELS
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", safe(value)).strip()


def strip_legacy_prefix(value: Any) -> str:
    text = clean_text(value)

    if not text:
        return ""

    return re.sub(
        r"^(?:appoximation|approximation)\s+from\s+claude\s*-\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def division_options_text() -> str:
    return "\n".join(
        f'- "{label}"'
        for _, label in sorted(DIVISION_LABELS.items())
    )


def validate_division_label(value: Any) -> str:
    """
    Store only official division labels.

    This deliberately does not map free-text labels like:
    - Food products
    - Beverages
    - Health and wellness
    - AI and Robotics
    - Natural cosmetics and dietary supplements

    The only non-exact cleanup allowed is removing an old legacy prefix
    and fixing case for an otherwise exact official label.
    """
    text = strip_legacy_prefix(value)

    if not text:
        return ""

    if text in ALLOWED_DIVISION_LABELS:
        return text

    return CASE_INSENSITIVE_DIVISION_MAP.get(text.lower(), "")


def log_event(supabase, **payload: Any) -> None:
    try:
        supabase.table("processing_logs").insert(payload).execute()
    except Exception:
        pass


def normalize_url(url: str | None) -> str:
    text = safe(url)
    if not text:
        return ""

    if not re.match(r"^https?://", text, flags=re.IGNORECASE):
        text = "https://" + text

    return text


def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SuccessionAnalysisBot/1.0; +https://openai.com)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    response = requests.get(
        url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers=headers,
    )
    response.raise_for_status()

    return response.text or ""


def extract_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "canvas", "iframe"]):
        tag.decompose()

    chunks = []

    for elem in soup.find_all(["title", "h1", "h2", "h3", "p", "li", "span"]):
        text = clean_text(elem.get_text(" "))

        if len(text) >= 25:
            chunks.append(text)

    return "\n".join(chunks)


def find_internal_links(
    base_url: str,
    html: str,
    max_links: int = MAX_EXTRA_PAGES,
) -> list[str]:
    if not html:
        return []

    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc.lower().replace("www.", "")
    soup = BeautifulSoup(html, "html.parser")

    preferred = (
        "about",
        "unternehmen",
        "leistungen",
        "produkte",
        "services",
        "kompetenzen",
        "produktion",
        "fertigung",
    )

    seen: set[str] = set()
    scored_links: list[tuple[int, str]] = []

    for a in soup.find_all("a", href=True):
        href = safe(a.get("href"))

        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue

        absolute = urljoin(base_url, href).split("#")[0].rstrip("/")
        parsed = urlparse(absolute)

        if parsed.scheme not in {"http", "https"}:
            continue

        if parsed.netloc.lower().replace("www.", "") != base_domain:
            continue

        if absolute == base_url.rstrip("/") or absolute in seen:
            continue

        seen.add(absolute)

        lower = absolute.lower()
        score = 1

        if any(token in lower for token in preferred):
            score = 0

        scored_links.append((score, absolute))

    return [url for _, url in sorted(scored_links)[:max_links]]


def scrape_website(url: str | None) -> tuple[str, str, str]:
    url = normalize_url(url)

    if not url:
        return "", "NO_WEBSITE", "No website provided."

    try:
        homepage_html = fetch_html(url)
        all_text_parts = [extract_text_from_html(homepage_html)]

        for link in find_internal_links(url, homepage_html, max_links=MAX_EXTRA_PAGES):
            try:
                html = fetch_html(link)
                page_text = extract_text_from_html(html)

                if page_text:
                    all_text_parts.append(f"\nPage: {link}\n{page_text}")

                time.sleep(0.25)

            except Exception:
                continue

        combined = "\n\n".join(part for part in all_text_parts if part)

        if not combined.strip():
            return "", "NO_TEXT", "No useful website text extracted."

        return combined[:MAX_WEBSITE_CHARS], "OK", ""

    except Exception as exc:
        return "", "SCRAPE_ERROR", str(exc)[:1000]


def _company_context(company: dict[str, Any]) -> dict[str, Any]:
    return {
        "company_name": company.get("name"),
        "website": company.get("website"),
        "legal_form": company.get("legal_form"),
        "purpose": company.get("purpose"),

        # Use the NorthData WZ column only as supporting context.
        "northdata_wz_code": company.get("northdata_wz_code"),
    }


def _has_fallback_context(company: dict[str, Any]) -> bool:
    return bool(
        safe(company.get("purpose"))
        or safe(company.get("northdata_wz_code"))
        or safe(company.get("name"))
    )


def build_claude_prompt(company: dict[str, Any], website_text: str) -> str:
    payload = _company_context(company)
    allowed_divisions = division_options_text()

    return f"""
You are a business analyst and classification assistant.

Analyze the provided company website text and company context.

Return ONLY valid JSON with exactly these keys:
{{
  "business_segment": "one exact label copied from the allowed division list below",
  "business_model": "specific activity/model only, short phrase, e.g. 'machinery manufacturing and contract manufacturing', 'meat processing and distribution', 'organic cold-pressed juices and juice cleanses', 'specialty coffee roasting and retail'",
  "detailed_business_summary": "business activity summary under 150 words explaining what the company does, products/services, customers/markets if clear"
}}

Allowed business_segment values:
{allowed_divisions}

Hard rules:
- business_segment must be copied EXACTLY from the allowed division list.
- Do not invent a shorter category.
- Do not return values like "Food products", "Beverages", "Cosmetics", "Health and wellness", "Software", "Retail", "AI and Robotics", or "Emergency preparedness retail".
- Do not include the numeric division code.
- Do not add any prefix such as "approximation from claude" or "appoximation from claude".
- Correct business_segment example: "Manufacture of food products"
- Wrong business_segment example: "Food products"
- business_segment and business_model must be separate fields.
- business_model should describe the specific product/service/activity only.
- Use only information supported by the website text and company context.
- Do not invent facts.
- Use northdata_wz_code as a supporting hint if it includes a label.
- If the exact division is uncertain, choose the closest conservative label from the allowed division list.
- detailed_business_summary must stay under 150 words.
- Return valid JSON only. No markdown. No explanation outside JSON.

Company context:
{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}

Website text:
{website_text}
""".strip()


def build_fallback_segment_2_prompt(
    company: dict[str, Any],
    fallback_reason: str,
) -> str:
    payload = {
        **_company_context(company),
        "fallback_reason": fallback_reason,
    }
    allowed_divisions = division_options_text()

    return f"""
You are a cautious business analyst.

No usable website text is available for this company. Create a conservative fallback business classification using ONLY the provided company context.

Return ONLY valid JSON with exactly these keys:
{{
  "business_segment": "one exact label copied from the allowed division list below",
  "business_model": "specific assumed activity/model only, short phrase, based only on purpose and NorthData WZ label if available",
  "detailed_business_summary": "short conservative fallback summary under 120 words, explicitly based only on registered purpose and available WZ context"
}}

Allowed business_segment values:
{allowed_divisions}

Hard rules:
- business_segment must be copied EXACTLY from the allowed division list.
- Do not invent a shorter category.
- Do not return values like "Food products", "Beverages", "Cosmetics", "Health and wellness", "Software", "Retail", "AI and Robotics", or "Emergency preparedness retail".
- Do not include the numeric division code.
- Do not add any prefix such as "approximation from claude" or "appoximation from claude".
- Correct business_segment example: "Manufacture of food products"
- Wrong business_segment example: "Food products"
- This is fallback assumption, not verified website analysis.
- Use the registered purpose first.
- Use northdata_wz_code only as the current NorthData-provided WZ hint.
- Do not mention products, customers, certifications, locations, or markets unless supported by the provided context.
- If evidence is weak, choose the closest conservative label from the allowed division list.
- Return valid JSON only. No markdown. No explanation outside JSON.

Company context:
{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}
""".strip()


def parse_claude_json_response(text: str) -> dict[str, Any]:
    text = safe(text)

    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        text = re.sub(r"\s*```$", "", text).strip()

    start = text.find("{")
    end = text.rfind("}")

    if start >= 0 and end >= start:
        text = text[start : end + 1]

    return json.loads(text)


def _call_claude(
    api_key: str,
    model_name: str,
    prompt: str,
    *,
    max_tokens: int = 650,
) -> str:
    client = Anthropic(api_key=str(api_key).strip())

    request_payload = {
        "model": model_name,
        "max_tokens": max_tokens,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
    }

    if "opus-4-7" not in str(model_name).lower():
        request_payload["temperature"] = 0.2

    response = client.messages.create(**request_payload)

    return "\n".join(
        block.text
        for block in response.content
        if getattr(block, "type", "") == "text"
    ).strip()


def _parsed_business_fields(parsed: dict[str, Any]) -> tuple[str, str, str, str]:
    summary = strip_legacy_prefix(
        parsed.get("detailed_business_summary")
        or parsed.get("detailed_business_segment")
        or parsed.get("detailed_business_model")
    )
    raw_segment = parsed.get("business_segment")
    segment = validate_division_label(raw_segment)
    business_model = strip_legacy_prefix(parsed.get("business_model"))

    return summary, segment, business_model, safe(raw_segment)


def summarize_with_claude(
    api_key: str,
    model_name: str,
    company: dict[str, Any],
    website_text: str,
) -> tuple[str, str, str, str, str, dict[str, Any]]:
    """
    Returns:
    summary, business_segment, business_model, api_status, notes, raw_data
    """
    if not api_key:
        return "", "", "", "CLAUDE_ERROR", "Claude API key missing.", {}

    if not website_text:
        return "", "", "", "NO_TEXT", "No website text extracted.", {}

    response_text = _call_claude(
        api_key=api_key,
        model_name=model_name,
        prompt=build_claude_prompt(company, website_text),
        max_tokens=650,
    )

    if not response_text:
        return "", "", "", "CLAUDE_ERROR", "Empty Claude response.", {}

    try:
        parsed = parse_claude_json_response(response_text)
    except Exception as exc:
        return (
            response_text[:6000],
            "",
            "",
            "PARSE_WARNING",
            f"Could not parse JSON: {exc}",
            {"raw_response": response_text},
        )

    summary, segment, business_model, raw_segment = _parsed_business_fields(parsed)

    notes_parts = []
    api_status = "success"

    if not segment:
        api_status = "PARSE_WARNING"
        notes_parts.append(
            f"Claude returned invalid business_segment outside official division list: {raw_segment}"
        )

    if not business_model:
        api_status = "PARSE_WARNING"
        notes_parts.append("Claude JSON missing: business_model")

    return (
        summary,
        segment,
        business_model,
        api_status,
        "; ".join(notes_parts),
        {
            "parsed": parsed,
            "raw_response": response_text,
        },
    )


def summarize_fallback_segment_2_with_claude(
    api_key: str,
    model_name: str,
    company: dict[str, Any],
    fallback_reason: str,
) -> tuple[str, str, str, str, str, dict[str, Any]]:
    """
    Returns:
    summary, business_segment, business_model, api_status, notes, raw_data
    """
    if not api_key:
        return "", "", "", "CLAUDE_ERROR", "Claude API key missing.", {}

    if not _has_fallback_context(company):
        return (
            "",
            "",
            "",
            "NO_FALLBACK_CONTEXT",
            "No purpose, WZ code, or company name available for fallback.",
            {},
        )

    response_text = _call_claude(
        api_key=api_key,
        model_name=model_name,
        prompt=build_fallback_segment_2_prompt(company, fallback_reason),
        max_tokens=550,
    )

    if not response_text:
        return "", "", "", "CLAUDE_ERROR", "Empty Claude fallback response.", {}

    try:
        parsed = parse_claude_json_response(response_text)
    except Exception as exc:
        return (
            "",
            "",
            "",
            "FALLBACK_PARSE_WARNING",
            f"Could not parse fallback JSON: {exc}",
            {
                "raw_response": response_text,
                "fallback_reason": fallback_reason,
            },
        )

    summary, segment, business_model, raw_segment = _parsed_business_fields(parsed)

    notes_parts = [f"Fallback assumption used because: {fallback_reason}"]
    api_status = "FALLBACK_ASSUMPTION"

    if not segment:
        api_status = "FALLBACK_PARSE_WARNING"
        notes_parts.append(
            f"Claude returned invalid business_segment outside official division list: {raw_segment}"
        )

    if not business_model:
        notes_parts.append("Claude JSON missing: business_model")

    if not summary:
        notes_parts.append("Claude JSON missing: detailed_business_summary")

    return (
        summary,
        segment,
        business_model,
        api_status,
        "; ".join(notes_parts),
        {
            "parsed": parsed,
            "raw_response": response_text,
            "fallback_reason": fallback_reason,
            "source": "fallback_purpose_northdata_wz",
            "claude_assumption": "Yes",
        },
    )


def _fetch_companies(
    supabase,
    page_size: int = 1000,
    hard_cap: int = 50000,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = 0

    while len(rows) < hard_cap:
        end = min(start + page_size - 1, hard_cap - 1)

        res = (
            supabase.table("companies")
            .select(
                "openregister_company_id,"
                "register_id,"
                "name,"
                "legal_form,"
                "purpose,"
                "website,"
                "northdata_wz_code"
            )
            .order("created_at", desc=True)
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


def _existing_model_exists(supabase, register_id: str) -> bool:
    res = (
        supabase.table("company_models")
        .select("id")
        .eq("company_register_id", register_id)
        .eq("model_provider", "claude")
        .limit(1)
        .execute()
    )

    return bool(getattr(res, "data", None) or [])


def _delete_existing_model(supabase, register_id: str) -> None:
    (
        supabase.table("company_models")
        .delete()
        .eq("company_register_id", register_id)
        .eq("model_provider", "claude")
        .execute()
    )


def _upsert_model_row(supabase, row: dict[str, Any]) -> None:
    supabase.table("company_models").upsert(
        row,
        on_conflict="company_register_id,model_provider",
    ).execute()


def _build_model_row(
    *,
    company: dict[str, Any],
    model_name: str,
    website: str,
    summary: str,
    segment: str,
    segment_2: str,
    business_model: str,
    api_status: str,
    notes: str,
    raw_data: dict[str, Any],
) -> dict[str, Any]:
    company_id = company.get("openregister_company_id")
    register_id = company.get("register_id") or company_id
    company_name = company.get("name") or company_id

    return {
        "company_register_id": register_id,
        "openregister_company_id": company_id,
        "company_name": company_name,
        "website": website,
        "model_provider": "claude",
        "model_name": model_name,

        # Website-derived or fallback-assumed segment.
        # Must be an official division label only.
        "business_segment": segment,

        # Assumption flag:
        # "No" = website-derived analysis.
        # "Yes" = fallback assumption from purpose + NorthData WZ.
        "business_segment_2": segment_2,

        "business_model": business_model,
        "summary": summary,
        "api_status": api_status,
        "notes": notes,
        "raw_data": raw_data,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


def run_claude_business_model_enrichment(
    *,
    supabase,
    claude_api_key: str,
    model_name: str = DEFAULT_CLAUDE_MODEL,
    update_existing: bool = False,
    progress_callback=None,
) -> dict[str, Any]:
    companies = _fetch_companies(supabase)
    total = len(companies)
    done = 0

    results: list[dict[str, Any]] = []

    processed = 0
    saved = 0
    skipped = 0
    errors = 0
    no_website = 0
    fallback_count = 0

    for company in companies:
        register_id = company.get("register_id") or company.get("openregister_company_id")
        company_id = company.get("openregister_company_id")
        company_name = company.get("name") or company_id
        website = normalize_url(company.get("website"))

        if not register_id:
            done += 1
            if progress_callback is not None:
                try:
                    progress_callback(done, total)
                except Exception:
                    pass
            continue

        try:
            if _existing_model_exists(supabase, register_id) and not update_existing:
                skipped += 1
                results.append(
                    {
                        "company": company_name,
                        "status": "skipped",
                        "reason": "existing model",
                    }
                )
                continue

            if update_existing:
                _delete_existing_model(supabase, register_id)

            processed += 1

            website_text = ""
            scrape_status = "NO_WEBSITE"
            scrape_notes = "No website available in company details."

            if website:
                website_text, scrape_status, scrape_notes = scrape_website(website)
            else:
                no_website += 1

            if scrape_status != "OK":
                (
                    summary,
                    segment,
                    business_model,
                    api_status,
                    notes,
                    raw_data,
                ) = summarize_fallback_segment_2_with_claude(
                    api_key=claude_api_key,
                    model_name=model_name,
                    company=company,
                    fallback_reason=f"{scrape_status}: {scrape_notes}",
                )

                if api_status.startswith("FALLBACK"):
                    fallback_count += 1

                row = _build_model_row(
                    company=company,
                    model_name=model_name,
                    website=website,
                    summary=summary,
                    segment=segment,
                    segment_2="Yes",
                    business_model=business_model,
                    api_status=api_status,
                    notes=notes,
                    raw_data={
                        **(raw_data or {}),
                        "scrape_status": scrape_status,
                        "scrape_notes": scrape_notes,
                        "company": company,
                        "claude_assumption": "Yes",
                    },
                )

                _upsert_model_row(supabase, row)
                saved += 1

                results.append(
                    {
                        "company": company_name,
                        "status": api_status,
                        "business_segment": segment,
                        "claude_assumption": "Yes",
                        "business_model": business_model,
                        "notes": notes[:160],
                    }
                )

                log_event(
                    supabase,
                    company_register_id=register_id,
                    openregister_company_id=company_id,
                    company_name=company_name,
                    module="claude_business_model",
                    endpoint="fallback_purpose_northdata_wz",
                    status=api_status,
                    message=f"Saved fallback Claude assumption: {segment} / {business_model}",
                )

                continue

            (
                summary,
                segment,
                business_model,
                api_status,
                notes,
                raw_data,
            ) = summarize_with_claude(
                api_key=claude_api_key,
                model_name=model_name,
                company=company,
                website_text=website_text,
            )

            segment_2 = "No"

            if api_status != "success" or not segment:
                (
                    fallback_summary,
                    fallback_segment,
                    fallback_business_model,
                    fallback_status,
                    fallback_notes,
                    fallback_raw,
                ) = summarize_fallback_segment_2_with_claude(
                    api_key=claude_api_key,
                    model_name=model_name,
                    company=company,
                    fallback_reason=f"Website Claude result was incomplete: {api_status}; {notes}",
                )

                if fallback_status.startswith("FALLBACK"):
                    fallback_count += 1

                row = _build_model_row(
                    company=company,
                    model_name=model_name,
                    website=website,
                    summary=fallback_summary,
                    segment=fallback_segment,
                    segment_2="Yes",
                    business_model=fallback_business_model,
                    api_status=fallback_status,
                    notes=fallback_notes,
                    raw_data={
                        "website_attempt": raw_data or {},
                        "fallback": fallback_raw or {},
                        "scraped_text_chars": len(website_text),
                        "source": "fallback_purpose_northdata_wz_after_incomplete_website_result",
                        "claude_assumption": "Yes",
                    },
                )

                _upsert_model_row(supabase, row)
                saved += 1

                results.append(
                    {
                        "company": company_name,
                        "status": fallback_status,
                        "business_segment": fallback_segment,
                        "claude_assumption": "Yes",
                        "business_model": fallback_business_model,
                        "notes": fallback_notes[:160],
                    }
                )

                log_event(
                    supabase,
                    company_register_id=register_id,
                    openregister_company_id=company_id,
                    company_name=company_name,
                    module="claude_business_model",
                    endpoint="fallback_purpose_northdata_wz",
                    status=fallback_status,
                    message=(
                        "Saved fallback Claude assumption after incomplete website result: "
                        f"{fallback_segment} / {fallback_business_model}"
                    ),
                )

                continue

            row = _build_model_row(
                company=company,
                model_name=model_name,
                website=website,
                summary=summary,
                segment=segment,
                segment_2=segment_2,
                business_model=business_model,
                api_status=api_status,
                notes=notes,
                raw_data={
                    **(raw_data or {}),
                    "scraped_text_chars": len(website_text),
                    "source": "website",
                    "claude_assumption": "No",
                },
            )

            _upsert_model_row(supabase, row)
            saved += 1

            results.append(
                {
                    "company": company_name,
                    "status": api_status,
                    "business_segment": segment,
                    "claude_assumption": "No",
                    "business_model": business_model,
                }
            )

            log_event(
                supabase,
                company_register_id=register_id,
                openregister_company_id=company_id,
                company_name=company_name,
                module="claude_business_model",
                endpoint="anthropic.messages.create",
                status=api_status,
                message=f"Saved website-derived Claude business segment/model: {segment} / {business_model}",
            )

        except Exception as exc:
            errors += 1
            msg = str(exc)[:1000]

            results.append(
                {
                    "company": company_name,
                    "status": "error",
                    "error": msg,
                }
            )

            log_event(
                supabase,
                company_register_id=register_id,
                openregister_company_id=company_id,
                company_name=company_name,
                module="claude_business_model",
                endpoint="business_model_enrichment",
                status="error",
                error_message=msg,
            )
        finally:
            done += 1
            if progress_callback is not None:
                try:
                    progress_callback(done, total)
                except Exception:
                    pass

    return {
        "companies_seen": len(companies),
        "processed": processed,
        "saved": saved,
        "skipped": skipped,
        "no_website": no_website,
        "fallback_count": fallback_count,
        "errors": errors,
        "results": results,
    }
