"""
Turns raw monday.com board data (strings, everywhere) into a clean pandas
DataFrame plus a running list of human-readable data-quality caveats.

Deliberately schema-agnostic: it drives cleaning off each column's monday.com
*type* (date / numeric / status / dropdown / text) rather than hardcoded
column names, so it works for whatever structure the CSV import produced.
"""
import re

import pandas as pd

from monday_client import fetch_board_raw

NUMERIC_TYPES = {"numbers", "numeric"}
DATE_TYPES = {"date"}
CATEGORY_TYPES = {"status", "dropdown", "color"}

_NUMERIC_STRIP_RE = re.compile(r"[^0-9.\-]")

# monday.com's quick-import column mapping frequently defaults everything to
# a generic "text" type regardless of actual content (dates and currency
# fields included). Rather than trust the declared type, sample each text
# column's values and infer date/numeric semantics from their shape.
_DATE_LIKE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}:\d{2})?$|^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")
_NUMERIC_LIKE_RE = re.compile(r"^[₹$€\s]*-?[\d,]+(\.\d+)?\s*%?$")


def _infer_semantic_type(series: pd.Series) -> str:
    non_empty = series.dropna().astype(str).str.strip()
    non_empty = non_empty[non_empty != ""]
    if len(non_empty) == 0:
        return "text"
    sample = non_empty if len(non_empty) <= 200 else non_empty.sample(200, random_state=0)
    if sample.str.match(_DATE_LIKE_RE).mean() >= 0.8:
        return "date"
    if sample.str.match(_NUMERIC_LIKE_RE).mean() >= 0.8:
        return "numeric"
    return "text"


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
    Trims whitespace and collapses labels that differ only by case/whitespace
    (e.g. 'Energy ', 'energy', 'ENERGY' -> 'Energy') so grouping/filtering
    doesn't silently split the same real-world value into buckets.

    Deliberately exact-match-after-normalization only (no fuzzy/edit-distance
    matching): this data has many structured ID-like codes (e.g. 'OWNER_001'
    vs 'OWNER_002', 'SDPLDEAL-075' vs 'SDPLDEAL-101') that are textually
    similar but semantically distinct records — fuzzy similarity matching
    was tried and rejected because it merged those together.
    """
    notes = []
    stripped = series.fillna("").astype(str).str.strip()
    non_empty = stripped[stripped != ""]

    # First-seen original casing wins as the canonical label for each
    # lowercased key, so e.g. 'Energy' (seen first) beats a later 'energy'.
    canonical_map: dict[str, str] = {}
    for val in non_empty.unique():
        key = val.lower()
        canonical_map.setdefault(key, val)

    collapsed = {
        val: canonical_map[val.lower()]
        for val in non_empty.unique()
        if val != canonical_map[val.lower()]
    }
    if collapsed:
        notes.append(f"Normalized case/whitespace-only label variants: {collapsed}")

    normalized = stripped.map(lambda v: canonical_map.get(v.lower(), v) if v else v)
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

        effective_type = mtype
        if mtype not in NUMERIC_TYPES and mtype not in DATE_TYPES:
            inferred = _infer_semantic_type(col)
            if inferred in ("date", "numeric") and inferred != mtype:
                notes.append(
                    f"Column '{title}': monday.com declared type '{mtype}', but values look like "
                    f"{inferred} data — inferring and parsing as {inferred}."
                )
                effective_type = inferred
            schema[title] = f"{mtype} (inferred: {inferred})" if inferred != mtype else mtype

        if effective_type in NUMERIC_TYPES or effective_type == "numeric":
            df[title] = col.map(_clean_numeric)
            unparseable = df[title].isna().sum() - missing_before
            if unparseable > 0:
                notes.append(f"Column '{title}': {unparseable} value(s) could not be parsed as numbers.")
        elif effective_type in DATE_TYPES or effective_type == "date":
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
