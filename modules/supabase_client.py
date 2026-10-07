from __future__ import annotations

import os
import streamlit as st
from supabase import create_client, Client


@st.cache_resource(show_spinner=False)
def get_supabase_client() -> Client:
    url = st.secrets.get("SUPABASE_URL") or os.getenv("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in Streamlit secrets.")
    return create_client(url, key)
