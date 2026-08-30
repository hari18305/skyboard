"""
Thin client for the monday.com GraphQL v2 API.

Deliberately dependency-light (just `requests`) and read-only: it only ever
issues queries, never mutations, matching the assignment's "read only"
integration requirement.
"""
import requests

from config import MONDAY_API_TOKEN, BOARD_IDS

MONDAY_API_URL = "https://api.monday.com/v2"

_BOARD_QUERY = """
query ($boardId: [ID!]) {
  boards(ids: $boardId) {
    name
    columns { id title type }
    items_page(limit: 500) {
      cursor
      items {
        id
        name
        column_values {
          id
          text
          type
          column { title }
        }
      }
    }
  }
}
"""


class MondayError(Exception):
    """Raised for any monday.com API / auth / network failure."""


def _resolve_board_id(board: str) -> str:
    board_id = BOARD_IDS.get(board)
    if not board_id:
        raise MondayError(
            f"No board configured for '{board}'. Known boards: {list(BOARD_IDS)}. "
            "Check MONDAY_WORK_ORDERS_BOARD_ID / MONDAY_DEALS_BOARD_ID."
        )
    return str(board_id)


def fetch_board_raw(board: str) -> dict:
    """
    Fetch a board's schema + all items directly from monday.com.

    Returns: {"name": str, "columns": [{"id","title","type"}], "items": [...]}
    Raises MondayError on any failure (bad token, bad board id, network, etc.)
    so callers can surface a clean message instead of a stack trace.
    """
    if not MONDAY_API_TOKEN:
        raise MondayError("MONDAY_API_TOKEN is not configured.")

    board_id = _resolve_board_id(board)

    try:
        resp = requests.post(
            MONDAY_API_URL,
            json={"query": _BOARD_QUERY, "variables": {"boardId": [board_id]}},
            headers={
                "Authorization": MONDAY_API_TOKEN,
                "Content-Type": "application/json",
                "API-Version": "2024-10",
            },
            timeout=20,
        )
    except requests.RequestException as e:
        raise MondayError(f"Network error contacting monday.com: {e}") from e

    if resp.status_code != 200:
        raise MondayError(f"monday.com API returned HTTP {resp.status_code}: {resp.text[:300]}")

    payload = resp.json()

    if "errors" in payload:
        raise MondayError(f"monday.com API error: {payload['errors']}")

    boards = payload.get("data", {}).get("boards") or []
    if not boards:
        raise MondayError(f"Board id {board_id} ('{board}') returned no data — check the id and token permissions.")

    b = boards[0]
    items = b.get("items_page", {}).get("items", [])

    return {"name": b.get("name"), "columns": b.get("columns", []), "items": items}
