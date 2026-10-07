from __future__ import annotations

from openregister import Openregister


def get_openregister_client(api_key: str) -> Openregister:
    if not api_key:
        raise ValueError("OpenRegister API key is required.")
    return Openregister(api_key=api_key, max_retries=2, timeout=90.0)
