"""What the panel adds to the instruction a scan's agents get, on top of what the user wrote."""

from app.core.config import settings

# Models often hand Strix's `list_requests` a filter Caido cannot parse: `NOT`, quoted numbers, text operators on numeric fields.
# Caido then answers «Invalid HTTPQL query» and the tool fails (the scan goes on, the model tries again, tokens are wasted).
HTTPQL_NOTE = """[Oreshnik] Caido HTTPQL cheat sheet (for the httpql_filter of list_requests; an invalid filter fails with "Invalid HTTPQL query"):
- A clause is namespace.field.operator:value, e.g. req.method.eq:"POST". No spaces inside a clause.
- Text fields (req.method, req.host, req.path, req.query, req.ext, req.raw, resp.raw): eq, ne, cont, ncont, regex, nregex. Values ALWAYS in double quotes: req.path.cont:"/api/".
- Numeric fields (resp.code, req.port): eq, ne, gt, gte, lt, lte. Values NEVER in quotes: resp.code.gte:400 (resp.code.eq:"200" is an error).
- Dates (req.created_at): gt, lt with a quoted ISO time: req.created_at.gt:"2024-01-01T00:00:00Z".
- Combine clauses with AND / OR in upper case: req.method.eq:"POST" AND resp.code.gte:400.
- There is no NOT and no "!": use the negated operator (ne, ncont, nregex): req.path.ncont:"/static".
- A bare quoted string searches request and response bodies: "password" OR "api_key".
- To list everything, pass no filter at all instead of inventing a match-all expression.
- If a filter is refused, cut it down to one clause and add the others back one at a time."""


def scan_instruction(user_instruction: str | None, resumed: bool) -> str | None:
    """The instruction handed to Strix: the user's own text, then the panel's notes. A resumed run already got the notes at its start."""
    own = (user_instruction or "").strip() or None
    if resumed or not settings.strix_httpql_hint:
        return own
    return f"{own}\n\n{HTTPQL_NOTE}" if own else HTTPQL_NOTE
