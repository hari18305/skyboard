"""
Streamlit chat front-end for the monday.com Business Intelligence agent.

Run locally:   streamlit run app.py
Deploy:        push to GitHub, deploy on https://share.streamlit.io
               (set GEMINI_API_KEY / MONDAY_API_TOKEN / MONDAY_WORK_ORDERS_BOARD_ID
               / MONDAY_DEALS_BOARD_ID in the app's Secrets)
"""
from datetime import date

import streamlit as st
from google import genai
from google.genai import types

from agent_tools import TOOLS
from config import GEMINI_API_KEY, GEMINI_MODEL, MONDAY_API_TOKEN, BOARD_IDS

st.set_page_config(page_title="Skylark BI Agent", page_icon="📊", layout="centered")

SYSTEM_PROMPT = f"""You are a business intelligence analyst agent for a company's
leadership team. You answer questions by querying two live monday.com boards:
"work_orders" (project execution data) and "deals" (sales pipeline data).
Today's date is {date.today().isoformat()} — resolve relative time references
("this quarter", "this month", "YTD") against it yourself; do not ask the user
what today's date is.

Rules you must follow:
1. Never invent or assume data. Always call a tool (get_board_schema,
   get_board_data, or compute_aggregate) to get real values before answering
   a question that depends on the boards.
2. For any total, count, sum, average, or breakdown-by-category, use
   compute_aggregate rather than eyeballing raw rows — its numbers are exact,
   yours from memory are not.
3. The underlying data is real-world and messy: missing values, inconsistent
   date formats, and near-duplicate category labels have already been
   partially cleaned for you, but gaps remain. When a tool result includes
   data_quality_notes relevant to your answer, briefly surface the caveat to
   the user (e.g. "note: 4 of 52 deals had no close date and were excluded").
   Don't hide data quality problems, but don't dump every note either — only
   the ones relevant to the numbers you're citing.
4. If a question is ambiguous (unclear time period, unclear which board,
   unclear metric, a sector/category name that doesn't clearly match what's
   in the data), ask a short clarifying question instead of guessing.
5. When you have both a number and business context, don't just state the
   number — add one or two sentences of insight (e.g. what's driving it,
   what's notable, what's missing), since the user is a founder/executive
   who wants insight, not a raw query result.
6. Be concise. This is a chat, not a report — unless the user explicitly asks
   for a leadership-update-style summary, in which case use clear headers
   and short bullets.
"""

LEADERSHIP_UPDATE_PROMPT = (
    "Prepare a leadership update. Pull pipeline value and deal count by "
    "sector from the deals board, and flag any work orders that look overdue "
    "or stalled from the work_orders board. Structure it as a short "
    "exec-ready summary with headers and bullets, and call out any data "
    "quality caveats that affect confidence in these numbers."
)


def boards_configured() -> bool:
    return bool(MONDAY_API_TOKEN and BOARD_IDS.get("work_orders") and BOARD_IDS.get("deals"))


def get_chat():
    if "chat" not in st.session_state:
        client = genai.Client(api_key=GEMINI_API_KEY)
        st.session_state.chat = client.chats.create(
            model=GEMINI_MODEL,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=TOOLS,
            ),
        )
    return st.session_state.chat


def send(text: str):
    st.session_state.messages.append({"role": "user", "content": text})
    with st.spinner("Thinking..."):
        try:
            response = get_chat().send_message(text)
            answer = response.text
        except Exception as e:
            answer = f"⚠️ The agent hit an error talking to Gemini or monday.com: {e}"
    st.session_state.messages.append({"role": "assistant", "content": answer})


st.title("📊 Skylark BI Agent")
st.caption("Conversational business intelligence over your monday.com Work Orders and Deals boards.")

if not GEMINI_API_KEY:
    st.error("GEMINI_API_KEY is not configured. Set it in your .env (local) or Secrets (Streamlit Cloud).")
    st.stop()

if not boards_configured():
    st.warning(
        "monday.com is not fully configured yet (MONDAY_API_TOKEN / "
        "MONDAY_WORK_ORDERS_BOARD_ID / MONDAY_DEALS_BOARD_ID). The agent will "
        "start, but any board query will return an error until this is set."
    )

if "messages" not in st.session_state:
    st.session_state.messages = []

with st.sidebar:
    st.subheader("Status")
    st.write("✅ Gemini configured" if GEMINI_API_KEY else "❌ Gemini not configured")
    st.write("✅ monday.com configured" if boards_configured() else "❌ monday.com not configured")
    st.divider()
    if st.button("📋 Generate leadership update"):
        send(LEADERSHIP_UPDATE_PROMPT)
    if st.button("🔄 Reset conversation"):
        st.session_state.messages = []
        st.session_state.pop("chat", None)
        st.rerun()

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if prompt := st.chat_input("Ask about pipeline, revenue, sectors, work orders..."):
    send(prompt)
    st.rerun()
