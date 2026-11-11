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

    LIKE wildcards (``%``, ``_``) and backslashes are escaped with a leading
    backslash so they match literally rather than being interpreted as
    pattern operators. Single quotes are doubled so the value cannot break
    out of its SQL string literal. Returns "" when nothing usable remains.
    """
    term = (search or "").strip()
    if not term:
        return ""
    # Escape backslash first so we don't double-escape our own escapes.
    term = term.replace("\\", "\\\\")
    term = term.replace("%", "\\%")
    term = term.replace("_", "\\_")
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


# Characters Postgres' to_tsquery treats as operators. ListMonk feeds the list
# and campaign search text to a full-text query, and a stray one ("a & b",
# "(draft)") makes it answer 500 instead of matching nothing.
_TSQUERY_OPERATORS = str.maketrans({c: " " for c in "&|!():*<>\\"})


def plain_search_term(search: str) -> str:
    """Free text for ListMonk's *plain-text* searches (lists, campaigns).

    Unlike ``/api/subscribers``, the ``query`` parameter of ``/api/lists`` and
    ``/api/campaigns`` is not SQL: ListMonk binds it as a parameter of a name
    (and subject) search. So nothing is escaped here; only full-text operator
    characters are turned into spaces and whitespace is collapsed.
    """
    return " ".join((search or "").translate(_TSQUERY_OPERATORS).split())
