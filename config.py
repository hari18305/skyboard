"""
Small helper to read config from either Streamlit secrets (when deployed on
Streamlit Community Cloud) or environment variables / .env (when run locally).
"""
import os
from dotenv import load_dotenv

load_dotenv()


def get_secret(key: str, default=None):
    try:
        import streamlit as st
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key, default)


GROQ_API_KEY = get_secret("GROQ_API_KEY")
GROQ_MODEL = get_secret("GROQ_MODEL", "openai/gpt-oss-120b")

MONDAY_API_TOKEN = get_secret("MONDAY_API_TOKEN")
MONDAY_WORK_ORDERS_BOARD_ID = get_secret("MONDAY_WORK_ORDERS_BOARD_ID")
MONDAY_DEALS_BOARD_ID = get_secret("MONDAY_DEALS_BOARD_ID")

BOARD_IDS = {
    "work_orders": MONDAY_WORK_ORDERS_BOARD_ID,
    "deals": MONDAY_DEALS_BOARD_ID,
}
