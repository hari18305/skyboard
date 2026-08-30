# Decision Log

## Key assumptions
- The two CSVs map cleanly to one monday.com board each, with monday.com's
  auto-detected column types (date/numbers/status/dropdown/text) being close
  enough to correct that type-driven cleaning is reliable without manual
  column remapping.
- "Founder-level query" means natural business language ("how's pipeline for
  energy sector this quarter?"), not a structured query language — so the
  agent needs to map loose language onto exact column names/filters itself.
  Column discovery is done once per session in app code (not a model-callable
  tool) and baked into the system prompt, both to save a tool round-trip and
  because the small model we ended up on (see below) has a tight token budget
  to spend carefully.
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
- **Category normalization is exact-match-after-trim/case only, not fuzzy
  string similarity.** Fuzzy matching (`difflib`) was tried first and
  rejected: this data has many structured ID-like codes (`OWNER_001` vs
  `OWNER_002`, `SDPLDEAL-075` vs `SDPLDEAL-101`) that are textually similar
  but semantically distinct, and fuzzy matching was silently merging them —
  a real correctness bug caught during live testing against the actual
  boards, not a hypothetical. Exact-match-after-normalization is safer even
  though it won't catch genuine free-text typos.
- **Switched LLM provider mid-build: Gemini → Groq.** Built the first working
  version on Gemini (`google-genai`, automatic function calling — the
  cleanest API for this if it works). Live-tested it successfully against
  the real boards, but its free tier's 5 requests/minute cap made even a
  single question unreliable once tool-calling was involved (a multi-step
  BI question can need several tool round-trips). Rather than ask for a
  billed key or silently ship something flaky, switched to Groq, whose free
  tier has far more headroom. Cost: Groq's SDK doesn't support automatic
  function calling from Python objects, so the tool loop had to be
  hand-written; also had to re-tune for Groq's own constraint (an 8K
  tokens/minute cap per model) by trimming conversation history sent per
  request and keeping tool-result payloads small. This is the single
  biggest "designed around a real constraint under time pressure" decision
  in this project.

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
- Tighten the system prompt to discourage the model from making more
  `compute_aggregate` calls than a question needs — observed it probing
  count/sum/avg/breakdown somewhat exhaustively for open-ended questions
  ("how's pipeline for X"), which produces a genuinely good, insightful
  answer but costs more tool round-trips (and token budget) than strictly
  necessary. A few-shot example or a stricter "plan your queries before
  calling tools" instruction would likely cut this down.
