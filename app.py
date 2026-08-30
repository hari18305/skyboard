"""
Streamlit chat front-end for the monday.com Business Intelligence agent.

Run locally:   streamlit run app.py
Deploy:        push to GitHub, deploy on https://share.streamlit.io
               (set GROQ_API_KEY / MONDAY_API_TOKEN / MONDAY_WORK_ORDERS_BOARD_ID
               / MONDAY_DEALS_BOARD_ID in the app's Secrets)

LLM: Groq (openai/gpt-oss-120b by default). Groq's Python SDK mirrors the
OpenAI SDK and does NOT do automatic function calling from plain Python
objects the way google-genai does — so this file drives a manual tool-call
loop: ask the model, check for tool_calls, execute them locally, feed the
results back, repeat until the model returns a plain text answer.
"""
import json
import re
import time
from datetime import date

import streamlit as st
from groq import Groq, APIStatusError

from agent_tools import TOOL_SCHEMAS, TOOL_FUNCS, get_board_schema
from config import GROQ_API_KEY, GROQ_MODEL, MONDAY_API_TOKEN, BOARD_IDS

st.set_page_config(page_title="Skylark BI Agent", page_icon="📊", layout="centered")

MAX_TOOL_ITERS = 10


def _describe_schema() -> str:
    """
    Fetches both boards' live column schema via a plain Python call (costs
    nothing against the LLM's rate/token limits) and formats it for the
    system prompt, so the model doesn't spend a tool round-trip (and
    precious tokens, on a model with only an 8K-token/minute budget)
    discovering column names it can just be told upfront. Still honest to
    "query monday.com dynamically" — this is fetched fresh each session,
    never hardcoded.
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
   yours from memory are not, and it's far cheaper on your limited token
   budget than dumping rows via get_board_data.
3. Keep get_board_data calls small (max_rows around 10, hard-capped at 20) —
   you are running on a model with a tight per-minute token budget.
4. The underlying data is real-world and messy: missing values, inconsistent
   date formats, and near-duplicate category labels have already been
   partially cleaned for you, but gaps remain. When a tool result includes
   data_quality_notes relevant to your answer, briefly surface the caveat to
   the user (e.g. "note: 4 of 52 deals had no close date and were excluded").
   Don't hide data quality problems, but don't dump every note either — only
   the ones relevant to the numbers you're citing.
5. If a question is ambiguous (unclear time period, unclear which board,
   unclear metric, a sector/category name that doesn't clearly match what's
   in the data), ask a short clarifying question instead of guessing.
6. When you have both a number and business context, don't just state the
   number — add one or two sentences of insight (e.g. what's driving it,
   what's notable, what's missing), since the user is a founder/executive
   who wants insight, not a raw query result.
7. Be concise. This is a chat, not a report — unless the user explicitly asks
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


def _extract_retry_delay(exc: Exception) -> float:
    """Best-effort parse of a suggested retry delay out of a Groq 429 error."""
    match = re.search(r"try again in ([\d.]+)s", str(exc), re.IGNORECASE)
    if match:
        return min(float(match.group(1)), 30) + 0.5
    return 5.0


def _shrink_largest_message(messages: list) -> bool:
    """
    Safety net for a 413 (request too large): tool-result payloads are
    sized to stay well under budget on their own (see agent_tools.py), but
    accumulated multi-turn history could still occasionally push a request
    over the limit. Truncates the largest tool-result message in place.
    Returns False if there's nothing left worth shrinking (caller should
    give up rather than loop forever).
    """
    candidates = [i for i, m in enumerate(messages) if m.get("role") == "tool" and len(m.get("content", "")) > 500]
    if not candidates:
        return False
    idx = max(candidates, key=lambda i: len(messages[i]["content"]))
    messages[idx]["content"] = messages[idx]["content"][:500] + '..."[truncated — result was too large]"'
    return True


def run_agent(client: Groq, messages: list) -> str:
    """
    Drives the manual tool-calling loop against Groq. `messages` is mutated
    in place (assistant/tool turns appended) so the caller's history stays
    in sync; returns the final assistant text.
    """
    for _ in range(MAX_TOOL_ITERS):
        answer = None
        for attempt in range(1, 4):
            try:
                resp = client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=messages,
                    tools=TOOL_SCHEMAS,
                    tool_choice="auto",
                    temperature=0.2,
                )
                break
            except APIStatusError as e:
                if e.status_code == 429 and attempt < 3:
                    delay = _extract_retry_delay(e)
                    with st.spinner(f"Rate-limited, retrying in {delay:.0f}s..."):
                        time.sleep(delay)
                    continue
                if e.status_code == 413 and attempt < 3 and _shrink_largest_message(messages):
                    # Too-large errors aren't time-based — retry immediately
                    # after shrinking, no point sleeping.
                    continue
                return f"⚠️ The agent hit a Groq API error: {e}"
        else:
            return "⚠️ Still rate-limited after retries — please wait a few seconds and try again."

        msg = resp.choices[0].message

        if not msg.tool_calls:
            return msg.content or "(no response)"

        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ],
            }
        )

        for tc in msg.tool_calls:
            func = TOOL_FUNCS.get(tc.function.name)
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if func is None:
                result = json.dumps({"error": f"Unknown tool '{tc.function.name}'"})
            else:
                try:
                    result = func(**args)
                except Exception as e:
                    result = json.dumps({"error": f"Tool '{tc.function.name}' raised: {e}"})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return "⚠️ Reached the tool-call step limit without a final answer — try rephrasing your question."


def send(text: str):
    st.session_state.messages.append({"role": "user", "content": text})

    # Trim to the last few conversational turns before sending — full
    # history (including old tool-call/tool-result messages) would burn
    # through the 8K-token/minute budget fast on a multi-turn chat.
    RECENT_TURNS = 6
    recent = [m for m in st.session_state.messages if m["role"] in ("user", "assistant")][-RECENT_TURNS:]

    client = Groq(api_key=GROQ_API_KEY)
    conversation = [{"role": "system", "content": SYSTEM_PROMPT}] + [
        {"role": m["role"], "content": m["content"]} for m in recent
    ]

    with st.spinner("Thinking..."):
        answer = run_agent(client, conversation)

    st.session_state.messages.append({"role": "assistant", "content": answer})


st.title("📊 Skylark BI Agent")
st.caption("Conversational business intelligence over your monday.com Work Orders and Deals boards.")

if not GROQ_API_KEY:
    st.error("GROQ_API_KEY is not configured. Set it in your .env (local) or Secrets (Streamlit Cloud).")
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
    st.write("✅ Groq configured" if GROQ_API_KEY else "❌ Groq not configured")
    st.write("✅ monday.com configured" if boards_configured() else "❌ monday.com not configured")
    st.divider()
    if st.button("📋 Generate leadership update"):
        send(LEADERSHIP_UPDATE_PROMPT)
    if st.button("🔄 Reset conversation"):
        st.session_state.messages = []
        st.rerun()

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if prompt := st.chat_input("Ask about pipeline, revenue, sectors, work orders..."):
    send(prompt)
    st.rerun()
