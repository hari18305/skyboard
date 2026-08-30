"""
Turns raw monday.com board data (strings, everywhere) into a clean pandas
DataFrame plus a running list of human-readable data-quality caveats.

Deliberately schema-agnostic: it drives cleaning off each column's monday.com
*type* (date / numeric / status / dropdown / text) rather than hardcoded
column names, so it works for whatever structure the CSV import produced.
"""
import re
from difflib import get_close_matches

import pandas as pd

from monday_client import fetch_board_raw

NUMERIC_TYPES = {"numbers", "numeric"}
DATE_TYPES = {"date"}
CATEGORY_TYPES = {"status", "dropdown", "color"}

_NUMERIC_STRIP_RE = re.compile(r"[^0-9.\-]")


def _clean_numeric(text: str):
    if text is None or text.strip() == "":
        return None
    cleaned = _NUMERIC_STRIP_RE.sub("", text)
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _clean_date(text: str):
    if text is None or text.strip() == "":
        return None
    try:
        return pd.to_datetime(text, errors="raise", dayfirst=False)
    except Exception:
        try:
            return pd.to_datetime(text, errors="raise", dayfirst=True)
        except Exception:
            return None


def _normalize_category_series(series: pd.Series) -> tuple[pd.Series, list[str]]:
    """
    Trims/cases category text and collapses near-duplicate labels
    (e.g. 'Energy ', 'energy', 'ENERGY' -> 'Energy') so grouping/filtering
    doesn't silently split the same real-world value into buckets.
    """
    notes = []
    stripped = series.fillna("").astype(str).str.strip()
    non_empty = stripped[stripped != ""]

    canonical_map: dict[str, str] = {}
    canonical_labels: list[str] = []
    for val in non_empty.unique():
        key = val.lower()
        match = get_close_matches(key, [c.lower() for c in canonical_labels], n=1, cutoff=0.86)
        if match:
            existing = next(c for c in canonical_labels if c.lower() == match[0])
            canonical_map[val] = existing
        else:
            canonical_labels.append(val)
            canonical_map[val] = val

    collapsed = {k: v for k, v in canonical_map.items() if k != v}
    if collapsed:
        notes.append(f"Normalized near-duplicate category labels: {collapsed}")

    normalized = stripped.map(lambda v: canonical_map.get(v, v))
    normalized = normalized.replace("", pd.NA)
    return normalized, notes


def get_clean_board(board: str) -> dict:
    """
    Fetch + clean a board.

    Returns:
        {
          "board_name": str,
          "schema": {col_title: monday_type, ...},
          "records": [ {col_title: value, ...}, ... ]   # values are clean python types
          "data_quality_notes": [str, ...],
          "row_count": int,
        }
    """
    raw = fetch_board_raw(board)
    notes: list[str] = []

    schema: dict[str, str] = {c["title"]: c["type"] for c in raw["columns"]}

    rows = []
    for item in raw["items"]:
        row = {"Name": item.get("name")}
        for cv in item.get("column_values", []):
            title = cv["column"]["title"] if cv.get("column") else cv.get("id")
            row[title] = cv.get("text")
        rows.append(row)

    if not rows:
        return {
            "board_name": raw["name"],
            "schema": schema,
            "records": [],
            "data_quality_notes": ["Board has no items."],
            "row_count": 0,
        }

    df = pd.DataFrame(rows)

    for title, mtype in schema.items():
        if title not in df.columns:
            continue
        col = df[title]
        total = len(col)
        missing_before = col.isna().sum() + (col.astype(str).str.strip() == "").sum()

        if mtype in NUMERIC_TYPES:
            df[title] = col.map(_clean_numeric)
            unparseable = df[title].isna().sum() - missing_before
            if unparseable > 0:
                notes.append(f"Column '{title}': {unparseable} value(s) could not be parsed as numbers.")
        elif mtype in DATE_TYPES:
            df[title] = col.map(_clean_date)
            unparseable = df[title].isna().sum() - missing_before
            if unparseable > 0:
                notes.append(f"Column '{title}': {unparseable} value(s) could not be parsed as dates.")
        elif mtype in CATEGORY_TYPES or df[title].dtype == object:
            df[title], cat_notes = _normalize_category_series(col)
            notes.extend(f"Column '{title}': {n}" for n in cat_notes)

        missing_after = df[title].isna().sum()
        if missing_after > 0:
            pct = round(100 * missing_after / total, 1)
            notes.append(f"Column '{title}': {missing_after}/{total} ({pct}%) values missing.")

    # JSON/records-friendly output: convert Timestamps to ISO strings, NaN -> None
    records = []
    for _, row in df.iterrows():
        rec = {}
        for k, v in row.items():
            if pd.isna(v):
                rec[k] = None
            elif isinstance(v, pd.Timestamp):
                rec[k] = v.date().isoformat()
            else:
                rec[k] = v
        records.append(rec)

    return {
        "board_name": raw["name"],
        "schema": schema,
        "records": records,
        "data_quality_notes": notes,
        "row_count": len(records),
    }
