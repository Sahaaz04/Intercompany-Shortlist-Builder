from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def model_to_dict(obj: Any) -> Any:
    """Convert SDK/Pydantic models and nested objects into plain JSON-safe data."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, list):
        return [model_to_dict(x) for x in obj]
    if isinstance(obj, tuple):
        return [model_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): model_to_dict(v) for k, v in obj.items()}
    if hasattr(obj, "to_dict"):
        return model_to_dict(obj.to_dict())
    if hasattr(obj, "model_dump"):
        return model_to_dict(obj.model_dump(by_alias=True))
    return str(obj)


def safe_get(data: Any, *keys: str, default: Any = None) -> Any:
    cur = data
    for key in keys:
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            cur = getattr(cur, key, None)
    return default if cur is None else cur


def eur_to_cents(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        return str(int(round(float(value) * 100)))
    except Exception:
        return None




def to_filter_number(value: Any) -> str | None:
    """Return an API-safe numeric string for non-money OpenRegister filters.

    Streamlit may produce 20.0 even when the user means 20. OpenRegister
    rejects some integer-like filters when they contain a decimal point, so
    20.0 becomes "20" while true decimal values remain compact strings.
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)
        if number.is_integer():
            return str(int(number))
        return format(number, "f").rstrip("0").rstrip(".")
    except Exception:
        return None


def cents_to_eur(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value) / 100.0, 2)
    except Exception:
        return None


def parse_csv_values(text: str | None) -> list[str]:
    if not text:
        return []
    return [x.strip() for x in text.replace("\n", ",").split(",") if x.strip()]


def owner_key(company_id: str, owner: dict, index: int) -> str:
    raw = "|".join([
        company_id or "",
        str(owner.get("id") or ""),
        str(owner.get("name") or ""),
        str(owner.get("type") or ""),
        str(owner.get("relation_type") or ""),
        str(owner.get("percentage_share") or ""),
        str(owner.get("nominal_share") or ""),
        str(owner.get("start") or ""),
        str(index),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def ubo_key(company_id: str, ubo: dict, index: int) -> str:
    raw = "|".join([
        company_id or "",
        str(ubo.get("id") or ""),
        str(ubo.get("name") or ""),
        str(ubo.get("percentage_share") or ""),
        str(ubo.get("max_percentage_share") or ""),
        str(index),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def management_key(company_id: str, person: dict, index: int) -> str:
    raw = "|".join([
        company_id or "",
        str(person.get("id") or ""),
        str(person.get("name") or ""),
        str(person.get("role") or ""),
        str(person.get("start_date") or ""),
        str(index),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def calculate_age(date_of_birth: str | None) -> int | None:
    if not date_of_birth:
        return None
    try:
        dob = datetime.fromisoformat(date_of_birth[:10]).date()
        today = date.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except Exception:
        return None



def format_industry_codes(value: Any) -> str:
    """Return a readable WZ industry-code list for Sheets/UI.

    OpenRegister company details returns industry codes as nested JSON, e.g.
    {"WZ2025": [{"code": "28.99"}]}. Search input is plain code strings,
    but stored company details keep the nested object. This flattens it to
    "28.99" or "87.10, 88.10".
    """
    if value in (None, ""):
        return ""

    # Supabase/gspread may hand this back as a JSON string.
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            value = json.loads(text)
        except Exception:
            return text

    codes: list[str] = []

    def add_code(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            code = item.strip()
        elif isinstance(item, dict):
            code = str(item.get("code") or item.get("value") or "").strip()
        else:
            code = str(item).strip()
        if code and code not in codes:
            codes.append(code)

    if isinstance(value, dict):
        for maybe_list in value.values():
            if isinstance(maybe_list, list):
                for item in maybe_list:
                    add_code(item)
            else:
                add_code(maybe_list)
    elif isinstance(value, list):
        for item in value:
            add_code(item)
    else:
        add_code(value)

    return ", ".join(codes)


def flatten_for_sheet(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
