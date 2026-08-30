# Skylark BI Agent

A conversational agent that answers founder-level business intelligence
questions by querying two live monday.com boards (**Work Orders** and
**Deals**), cleaning the underlying messy data on the fly, and computing
exact aggregates rather than letting the LLM guess numbers.

## Architecture

```
monday.com boards (Work Orders, Deals)
        │  GraphQL v2 API, read-only, personal token
        ▼
monday_client.py     — raw fetch (schema + items) for a board
        ▼
data_utils.py        — cleaning layer, generic over monday.com column types:
                         • dates parsed / invalid ones flagged, not dropped
                         • numbers stripped of currency symbols/commas
                         • near-duplicate category labels merged
                           ("Energy" / "energy " / "ENERGY" → "Energy")
                         • every gap recorded as a human-readable
                           data_quality_note instead of silently vanishing
        ▼
agent_tools.py        — 3 tools exposed to the LLM:
                         • get_board_schema   – discover columns before querying
                         • get_board_data     – inspect cleaned row-level data
                         • compute_aggregate  – exact sum/count/avg/min/max/median,
                                                 optional group_by + filters,
                                                 computed in pandas (not by the LLM)
        ▼
app.py (Streamlit)    — chat UI. Gemini (google-genai) drives the tool-calling
                         loop automatically: the model decides which tool(s)
                         to call, we execute them, it synthesizes the answer.
```

**Why this shape:** the assignment's own framing ("business data is messy",
"provide context... not just raw numbers") pushed the design toward two
non-negotiables — (1) numeric answers must be computed deterministically, not
hallucinated by the model reading a table, and (2) every cleaning step must
leave an audit trail (`data_quality_notes`) that reaches the final answer,
rather than quietly dropping rows.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| LLM / agent loop | Gemini (`google-genai`, automatic function calling) | Available API key; native tool-use means no hand-rolled function-call parser |
| monday.com integration | Direct GraphQL v2 API (`requests`) | Full control, no extra infra, satisfies "MCP or API — your choice" |
| Data cleaning | `pandas` | Fast, expressive normalization/aggregation |
| UI | Streamlit `st.chat_message` | Fastest path to a real conversational UI + free public hosting |
| Hosting | Streamlit Community Cloud | Deploys straight from GitHub, no server management, satisfies "testable without local setup" |

## Setup

1. **monday.com**: import the two CSVs as separate boards (Work Orders, Deals),
   note each board's ID from its URL, and generate a personal API token
   (Avatar → Administration → API).
2. Copy `.env.example` to `.env` and fill in `GEMINI_API_KEY`,
   `MONDAY_API_TOKEN`, `MONDAY_WORK_ORDERS_BOARD_ID`, `MONDAY_DEALS_BOARD_ID`.
3. `pip install -r requirements.txt`
4. `streamlit run app.py`

### Deploying (Streamlit Community Cloud)
Push this repo to GitHub → [share.streamlit.io](https://share.streamlit.io) →
"New app" → point at `app.py` → add the same four values from `.env` under
**Secrets** (same `KEY = "value"` format, no quotes needed for numbers) →
Deploy.

## What's implemented
- Live, read-only monday.com querying (no hardcoded CSV data)
- Generic cleaning layer driven by monday.com's own column types
- Deterministic aggregation tool (sum/count/avg/min/max/median, group-by, filters)
- Conversational chat interface with tool-use
- Data quality caveats surfaced in answers
- "Generate leadership update" one-click summary (sidebar button)
- Graceful error handling for monday.com/API failures (returned as JSON
  `{"error": ...}` to the model, which explains the failure to the user
  instead of crashing)

## Known limitations / what I'd improve with more time
- No conversation-level memory of previously fetched data — every question
  triggers a fresh board fetch (fine at this data scale, wouldn't scale to
  large boards).
- Category-label fuzzy-matching (`difflib`) is a heuristic, not perfect —
  a dedicated small classification pass would be more robust for messier text.
- No pagination past 500 items per board.
- No caching layer, so latency scales with monday.com API round-trips.
- Clarifying-question behavior depends on the model choosing to ask rather
  than guess — good system-prompt discipline helps but isn't a hard guarantee.

See `DECISION_LOG.md` for assumptions, trade-offs, and the interpretation of
"leadership updates."
