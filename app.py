"""
Streamlit chat front-end for the monday.com Business Intelligence agent.

Run locally:   streamlit run app.py
Deploy:        push to GitHub, deploy on https://share.streamlit.io
               (set GEMINI_API_KEY / MONDAY_API_TOKEN / MONDAY_WORK_ORDERS_BOARD_ID
               / MONDAY_DEALS_BOARD_ID in the app's Secrets)
"""
import json
import re
import time
from datetime import date

import streamlit as st
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from agent_tools import get_board_data, compute_aggregate, get_board_schema

# get_board_schema is deliberately NOT offered to Gemini as a callable tool —
# its output is baked into the system prompt below (fetched fresh each
# session via a plain Python call) so the model never needs to spend a
# rate-limited API round-trip discovering column names it already has.
AGENT_TOOLS = [get_board_data, compute_aggregate]
from config import GEMINI_API_KEY, GEMINI_MODEL, MONDAY_API_TOKEN, BOARD_IDS

st.set_page_config(page_title="Skylark BI Agent", page_icon="📊", layout="centered")


def _describe_schema() -> str:
    """
    Fetches both boards' live column schema via a plain Python call (no
    Gemini request involved, so it costs nothing against the API rate
    limit) and formats it for the system prompt. This lets the model skip
    a get_board_schema tool round-trip on every question — cutting typical
    Gemini calls per question roughly in half — while staying honest to
    "query monday.com dynamically": the schema is fetched fresh from the
    live board each session, never hardcoded.
    """
    lines = []
    for board in ("work_orders", "deals"):
        try:
            info = json.loads(get_board_schema(board))
            cols = ", ".join(info.get("columns", {}).keys())
            lines.append(f"- {board}: {cols}")
        except Exception:
            lines.append(f"- {board}: (schema unavailable — monday.com may not be configured yet)")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You are a business intelligence analyst agent for a company's
leadership team. You answer questions by querying two live monday.com boards:
"work_orders" (project execution data) and "deals" (sales pipeline data).
Today's date is {date.today().isoformat()} — resolve relative time references
("this quarter", "this month", "YTD") against it yourself; do not ask the user
what today's date is.

Known columns on each board (fetched live at session start — use these exact
names in tool calls, no need to re-discover them):
{_describe_schema()}

Rules you must follow:
1. Never invent or assume data. Always call a tool (get_board_data or
   compute_aggregate) to get real values before answering a question that
   depends on the boards.
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


def send(text: str):
    """
    Sends one message and gets a reply.

    Note: we deliberately create a brand-new genai.Client + chat on every
    call rather than caching one long-lived chat object across Streamlit
    reruns — the SDK's underlying transport was observed to close itself
    after a single request in that usage pattern ("client has been closed"
    on the second message). Conversation continuity is preserved instead by
    replaying prior turns as `history` into the fresh chat.
    """
    st.session_state.messages.append({"role": "user", "content": text})

    # attempts=1 disables the SDK's own internal retry-with-backoff on
    # transient errors (observed to silently retry for minutes on 429s
    # before ever raising) — our retry loop below handles that instead,
    # with a visible spinner and a bounded wait.
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(
            timeout=30_000,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    history = [
        types.Content(
            role=("model" if m["role"] == "assistant" else "user"),
            parts=[types.Part(text=m["content"])],
        )
        for m in st.session_state.messages[:-1]  # everything before this new user turn
    ]

    answer = None
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            with st.spinner("Thinking..." if attempt == 1 else f"Rate-limited, retrying (attempt {attempt}/{max_attempts})..."):
                chat = client.chats.create(
                    model=GEMINI_MODEL,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        tools=AGENT_TOOLS,
                    ),
                    history=history,
                )
                response = chat.send_message(text)
                answer = response.text
            break
        except genai_errors.ClientError as e:
            is_rate_limit = getattr(e, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(e)
            if is_rate_limit and attempt < max_attempts:
                match = re.search(r"retry in ([\d.]+)s", str(e))
                delay = min(float(match.group(1)), 60) + 1 if match else 15
                with st.spinner(f"Gemini free-tier rate limit hit — waiting {int(delay)}s before retrying..."):
                    time.sleep(delay)
                continue
            if is_rate_limit:
                answer = (
                    "⚠️ Hit the Gemini free-tier rate limit (5 requests/min) and retries were "
                    "exhausted. Please wait ~30-60s and ask again — or use an API key with billing "
                    "enabled for higher limits."
                )
            else:
                answer = f"⚠️ The agent hit an error talking to Gemini: {e}"
            break
        except Exception as e:
            answer = f"⚠️ The agent hit an unexpected error: {e}"
            break

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
