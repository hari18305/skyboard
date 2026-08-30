"""
The tool surface the Gemini agent is given. Kept deliberately small (3 tools)
and generic so the model can answer arbitrary founder-level questions without
us hardcoding query logic per question type.

Design choice: numeric aggregation is computed here in pandas, NOT left to
the LLM to eyeball from a data dump. For a BI agent, "the pipeline total is
$X" has to be exactly right — so the model's job is choosing *which*
aggregate to run and narrating the result, not doing the arithmetic itself.
"""
import json

import pandas as pd

from data_utils import get_clean_board
from monday_client import MondayError

VALID_BOARDS = ("work_orders", "deals")
VALID_AGGS = ("sum", "count", "avg", "min", "max", "median")


def _error(msg: str) -> str:
    return json.dumps({"error": msg})


def _load_df(board: str):
    clean = get_clean_board(board)
    df = pd.DataFrame(clean["records"])
    return df, clean


def get_board_schema(board: str) -> str:
    """Get the column names and monday.com data types for a board, so you know
    what fields exist before querying it. board must be exactly 'work_orders' or 'deals'.

    Returns a JSON string: {"board_name": ..., "columns": {column_name: monday_type}}.
    """
    if board not in VALID_BOARDS:
        return _error(f"Unknown board '{board}'. Must be one of {VALID_BOARDS}.")
    try:
        clean = get_clean_board(board)
    except MondayError as e:
        return _error(str(e))
    return json.dumps({"board_name": clean["board_name"], "columns": clean["schema"]})


def get_board_data(board: str, max_rows: int = 200) -> str:
    """Fetch cleaned, normalized records from a monday.com board ('work_orders' or
    'deals'). Missing values have been left as null (not dropped), inconsistent
    date formats parsed to ISO dates, and near-duplicate category labels (e.g.
    'Energy' vs 'energy ') merged. Use this to inspect actual rows, e.g. to
    answer questions that need row-level detail rather than a single number.

    Returns a JSON string: {"board_name", "row_count", "data_quality_notes",
    "records": [ {column: value, ...}, ... ]}. If row_count exceeds max_rows,
    only the first max_rows records are returned (row_count still reflects the
    true total) — prefer compute_aggregate for questions about totals/counts
    over the full board.
    """
    if board not in VALID_BOARDS:
        return _error(f"Unknown board '{board}'. Must be one of {VALID_BOARDS}.")
    try:
        clean = get_clean_board(board)
    except MondayError as e:
        return _error(str(e))

    records = clean["records"][:max_rows]
    return json.dumps(
        {
            "board_name": clean["board_name"],
            "row_count": clean["row_count"],
            "returned_rows": len(records),
            "data_quality_notes": clean["data_quality_notes"],
            "records": records,
        },
        default=str,
    )


def compute_aggregate(
    board: str,
    metric: str = "",
    agg: str = "count",
    group_by: str = "",
    filters_json: str = "",
) -> str:
    """Compute an exact, deterministic aggregate over a monday.com board. Always
    prefer this over eyeballing get_board_data for any total, count, sum,
    average, or breakdown-by-category question.

    Args:
      board: 'work_orders' or 'deals'.
      metric: numeric column name to aggregate, e.g. 'Deal Value'. Leave empty
        when agg='count' (row counting doesn't need a metric column).
      agg: one of 'sum','count','avg','min','max','median'.
      group_by: optional column name to break the result down by, e.g. 'Sector'
        or 'Status'. Leave empty for a single overall number.
      filters_json: optional JSON object string of {"column_name": "value"}
        equality/contains filters applied (case-insensitive substring match)
        before aggregating, e.g. '{"Sector": "Energy"}'.

    Returns a JSON string: {"result": number or {group: number, ...},
    "rows_used": int, "rows_excluded_missing_metric": int, "data_quality_notes": [...]}.
    """
    if board not in VALID_BOARDS:
        return _error(f"Unknown board '{board}'. Must be one of {VALID_BOARDS}.")
    if agg not in VALID_AGGS:
        return _error(f"Unknown agg '{agg}'. Must be one of {VALID_AGGS}.")

    try:
        df, clean = _load_df(board)
    except MondayError as e:
        return _error(str(e))

    if df.empty:
        return json.dumps({"result": None, "rows_used": 0, "note": "Board has no rows."})

    filters = {}
    if filters_json:
        try:
            filters = json.loads(filters_json)
        except json.JSONDecodeError:
            return _error(f"filters_json was not valid JSON: {filters_json!r}")

    for col, val in filters.items():
        if col not in df.columns:
            return _error(f"Filter column '{col}' not found. Available columns: {list(df.columns)}")
        df = df[df[col].astype(str).str.contains(str(val), case=False, na=False)]

    rows_available = len(df)

    if agg != "count":
        if not metric:
            return _error("metric is required unless agg='count'.")
        if metric not in df.columns:
            return _error(f"Metric column '{metric}' not found. Available columns: {list(df.columns)}")
        before = len(df)
        df = df[df[metric].notna()]
        excluded = before - len(df)
    else:
        excluded = 0

    def _apply(sub: pd.DataFrame):
        if agg == "count":
            return len(sub)
        series = pd.to_numeric(sub[metric], errors="coerce").dropna()
        if series.empty:
            return None
        if agg == "sum":
            return float(series.sum())
        if agg == "avg":
            return float(series.mean())
        if agg == "min":
            return float(series.min())
        if agg == "max":
            return float(series.max())
        if agg == "median":
            return float(series.median())

    if group_by:
        if group_by not in df.columns:
            return _error(f"group_by column '{group_by}' not found. Available columns: {list(df.columns)}")
        result = {}
        for key, sub in df.groupby(group_by, dropna=False):
            label = "Unknown/Missing" if pd.isna(key) else str(key)
            result[label] = _apply(sub)
    else:
        result = _apply(df)

    return json.dumps(
        {
            "result": result,
            "rows_used": rows_available - excluded,
            "rows_excluded_missing_metric": excluded,
            "data_quality_notes": clean["data_quality_notes"],
        },
        default=str,
    )


TOOLS = [get_board_schema, get_board_data, compute_aggregate]
