"""
Shared builder that turns free-text search boxes into safe ListMonk queries.

ListMonk's ``query`` parameter is a raw SQL expression evaluated by Postgres,
so plain search terms must never reach it verbatim ("info@acme" used to 400
with `column "info" does not exist`, and a crafted term could do worse).
Every free-text search box routes through :func:`build_ilike_search`, which
wraps the term in literal ILIKE matches on the given columns.
"""


def escape_search_term(search: str) -> str:
    """Reduce free text to a literal substring safe inside an ILIKE pattern.

    LIKE wildcards and backslashes are stripped (the term must only ever match
    literally) and single quotes are doubled so the value cannot break out of
    its SQL string literal. Returns "" when nothing usable remains.
    """
    term = (search or "").strip()
    if not term:
        return ""
    for ch in ("%", "_", "\\"):
        term = term.replace(ch, "")
    if not term:
        return ""
    return term.replace("'", "''")


def build_ilike_search(search: str, columns: tuple[str, ...] | list[str]) -> str:
    """Return an OR-joined ILIKE clause matching ``search`` in ``columns``.

    ``columns`` must be caller-supplied literal column names (never user
    input), e.g. ``("subscribers.email", "subscribers.name")``.
    """
    term = escape_search_term(search)
    if not term:
        return ""
    parts = [f"{col} ILIKE '%{term}%'" for col in columns]
    clause = " OR ".join(parts)
    return f"({clause})" if len(parts) > 1 else clause
