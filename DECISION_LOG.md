# Decision Log

## Key assumptions
- The two CSVs map cleanly to one monday.com board each, with monday.com's
  auto-detected column types (date/numbers/status/dropdown/text) being close
  enough to correct that type-driven cleaning is reliable without manual
  column remapping.
- "Founder-level query" means natural business language ("how's pipeline for
  energy sector this quarter?"), not a structured query language — so the
  agent needs to map loose language onto exact column names/filters itself,
  which is why `get_board_schema` exists as a discovery step before querying.
- Given the 6-hour/2-hour time budget, correctness of computed numbers
  (deterministic aggregation) mattered more than conversational polish or
  breadth of supported query types.

## Trade-offs chosen and why
- **LLM does not compute numbers itself.** Aggregation (`compute_aggregate`)
  is plain pandas, not the model reading rows and adding them up. Slower to
  build than "just dump the table into the prompt," but a BI tool that's
  occasionally wrong about a total is worse than one that's occasionally
  unable to answer.
- **Direct GraphQL API over monday.com's MCP server.** Fewer moving parts to
  wire up and debug under time pressure; the assignment explicitly allows
  either.
- **Streamlit over a custom React/FastAPI split.** Cuts the "hosted, testable
  without local setup" requirement down to a single `git push` + Streamlit
  Cloud deploy, at the cost of UI polish/customizability.
- **Type-driven generic cleaning over hardcoded column names.** More
  resilient to whatever the actual CSV schema turned out to be, and directly
  serves the "do not hardcode CSV data" requirement — the cleaning logic
  adapts to the live board schema rather than assuming fixed columns.
- **Near-duplicate category merging via string-similarity (`difflib`)
  rather than an LLM call per value.** Cheap and fast, good enough for
  small-cardinality fields like sector/status; would not scale to
  high-cardinality free text.

## How "leadership updates" was interpreted
Interpreted as: a founder wants a short, structured, executive-ready digest
they could paste into a Slack update or slide — not a dashboard, not a raw
export. Implemented as a one-click sidebar action that sends a fixed prompt
asking the agent to pull pipeline value/count by sector and flag overdue/
stalled work orders, using the same tools as normal chat, formatted with
headers and bullets, with data-quality caveats called out. Kept intentionally
lightweight (a prompt template over existing tools, not new backend logic)
given the time budget — the "optional" framing in the assignment signaled
this shouldn't consume core-feature time.

## What I'd do differently with more time
- Add a lightweight intent/column-mapping step (or a few-shot example bank)
  so the agent maps founder phrasing to actual column names more reliably,
  instead of relying entirely on the model's judgment plus `get_board_schema`.
- Cache board fetches per session instead of re-querying monday.com on every
  message.
- Add automated tests around the cleaning functions (`data_utils.py`) using
  synthetic messy CSVs, since correctness there underlies every downstream
  answer.
- Expand `compute_aggregate` to support multi-column group-by and simple
  time-bucketing (by month/quarter) natively, instead of relying on the model
  to pre-filter by date range via `filters_json`.
- Add a proper cross-board join tool (e.g. matching deals to work orders by
  client/project name) rather than relying on the model to reason across two
  separately-fetched tables.
