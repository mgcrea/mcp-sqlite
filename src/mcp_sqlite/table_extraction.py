"""Enumerate every table a SQLite statement touches.

**This is the security-critical half of the guard, and it is the exact dual of
`sql_validation.py`.** That module is a deny-list over tokens — "does `ATTACH` appear?" —
where over-approximating is safe and a false positive is an annoyed user. A table allow-list
needs the opposite: a *complete over-approximation of the set of tables touched*, where a
false negative is a breach. Missing one table means a statement is authorized against a
smaller set than it actually reads or writes.

**Why a regex cannot do this.** `_strip_literals_and_comments` in `sql_validation.py`
replaces double-quoted identifiers with spaces, so `SELECT * FROM "Payroll"` has its table
name *deleted* before any scan could see it. Beyond that, CTEs, correlated subqueries,
derived tables, `UNION` arms and table-valued functions each defeat a `FROM\\s+(\\w+)` pattern
independently. Real parsing is not gold-plating here; it is the minimum that can be correct.

**SQLite names are unqualified, unlike Postgres or SQL Server.** There is no schema
component, so the value a rule is written about is the bare table name, lowercased. The
*database* is not part of it — it is submitted separately as a `path_prefix` resource, for
the reasons set out in `tools/sqlite.py`. The one qualification SQLite does have is the
attached-database prefix (`main.users`, `temp.t`), and it is normalized away rather than
kept: otherwise a rule denying `users` would be sidestepped by writing `main.users`.

Both gates run, in order: `validate_readonly_query` first when the server is read-only
(sqlglot parses `DELETE` perfectly happily — enumeration is not policy), then this.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

DIALECT = "sqlite"

#: Database qualifiers that name *this* connection's own database rather than another file.
#:
#: `main` is the file opened at `SQLITE_DB_PATH`; `temp` is the per-connection scratch
#: database, which lives and dies inside a single call here because every call opens and
#: closes its own connection. Both normalize to the bare table name. Anything else is an
#: `ATTACH`ed file — a different database on disk, which the `path_prefix` resource for this
#: deployment does not cover — and is refused below.
_OWN_DATABASES = frozenset({"main", "temp"})

#: Statement shapes this extractor can enumerate completely.
#:
#: An allow-list, because the failure it exists to stop is silent. `PRAGMA table_info(users)`
#: and `ATTACH DATABASE '/etc/other.db' AS x` each parse to a node that contains *no*
#: `exp.Table` at all, so the walk below would return an empty set — which on an allow-list
#: reads as "this statement touches nothing" and is authorized as such. A deny-list would
#: have to name every such form in advance; this only has to name the forms the tool serves.
#:
#: `exp.Select` covers `WITH … SELECT` (the CTEs hang off the select). `exp.SetOperation`
#: covers `UNION`, `EXCEPT` and `INTERSECT`, which are siblings rather than subclasses of
#: one another. The DML and DDL entries matter only in read/write mode, where
#: `validate_readonly_query` does not run.
_ANALYZABLE_STATEMENTS = (
    exp.Select,
    exp.SetOperation,
    exp.Subquery,
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
)

#: Node types a `FROM` / `JOIN` may name and still be fully enumerable.
#:
#: `Table` is walked below; `Subquery`, `Select` and set operations are recursed into, so
#: their tables surface too; `Values` and `Unnest` are constants that read nothing; `Lateral`
#: is a wrapper whose own source is checked on its own iteration.
#:
#: **Anything else is refused.** A table-valued function — `SELECT * FROM
#: pragma_table_info('users')`, or a `json_each(...)` over a column — produces no plain table
#: node, so it would otherwise sail through the walk while reading whatever it likes. An
#: allow-list of source shapes catches that; a deny-list of known-bad functions would not.
_ANALYZABLE_SOURCES = (
    exp.Table,
    exp.Subquery,
    exp.Select,
    exp.SetOperation,
    exp.Values,
    exp.Unnest,
    exp.Lateral,
)


class TableExtractionError(Exception):
    """The set of tables touched could not be established with certainty.

    Always fatal to the authorization decision. Every raise site below is a case where the
    extractor cannot prove what a statement touches — and an unprovable set must never be
    handed to an allow-list check, because the check would then be authorizing a guess.
    """


def extract_referenced_tables(query: str) -> set[str]:
    """Every table the statement touches, as lowercase bare names.

    Covers writes as well as reads: `INSERT INTO users`, `UPDATE users`, `DROP TABLE users`
    all name `users`, and in read/write mode those are exactly the statements most worth
    authorizing. The resource kind does not distinguish the two — the *function* does, and
    `SQLITE_READONLY` is a separate gate that runs first.

    Raises `TableExtractionError` whenever the answer is not certain.
    """
    try:
        statements = sqlglot.parse(query, dialect=DIALECT)
    except SqlglotError as exc:
        raise TableExtractionError(f"Could not parse the query: {exc}") from exc
    except RecursionError as exc:
        # A deeply nested query can blow the parser's stack. Refusing is the only safe
        # answer: a half-walked tree is a partial answer presented as a complete one.
        raise TableExtractionError("Query is too deeply nested to analyze") from exc

    real = [statement for statement in statements if statement is not None]
    if not real:
        raise TableExtractionError("Query contained no statement to analyze")
    if len(real) > 1:
        # `sqlite3.Cursor.execute` refuses these too, and `validate_readonly_query` rejects
        # them first in read-only mode. This is defence in depth for the read/write path,
        # where neither of those two gates is the one making the authorization decision.
        raise TableExtractionError("Multi-statement queries cannot be analyzed")

    statement = _unwrap_explain(real[0])

    if not isinstance(statement, _ANALYZABLE_STATEMENTS):
        # The allow-list. See `_ANALYZABLE_STATEMENTS`: the statements this refuses are
        # precisely the ones that would otherwise enumerate to the empty set and be
        # authorized as touching nothing.
        raise TableExtractionError(f"Statements of this form cannot be authorized: {statement.sql(DIALECT)}")

    # sqlglot emits `Command` for syntax it recognises but does not model. Its contents are
    # opaque, so any table inside one would be invisible to the walk below. The statement
    # itself has already passed the allow-list; this catches a nested one.
    for command in statement.find_all(exp.Command):
        raise TableExtractionError(f"Query contains a construct that cannot be analyzed: {command.this}")

    _assert_analyzable_sources(statement)

    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}

    tables: set[str] = set()
    for table in statement.find_all(exp.Table):
        resolved = resolve_table_reference(table, cte_names=cte_names)
        if resolved is not None:
            tables.add(resolved)
    return tables


def _unwrap_explain(statement: exp.Expression) -> exp.Expression:
    """`EXPLAIN <query>` reduced to `<query>`, which is what actually reads tables.

    `validate_readonly_query` advertises `EXPLAIN` as an allowed statement, but sqlglot's
    SQLite dialect does not model it: the whole thing comes back as an opaque `Command`
    carrying the rest as a string literal. Without this, every `EXPLAIN` the read-only gate
    permits would fail extraction and be refused — a gate contradicting the one in front of
    it. Re-parsing the tail authorizes the plan against the same tables the query itself
    would touch, which is the honest answer: an execution plan discloses the shape of the
    data it describes.
    """
    if not isinstance(statement, exp.Command) or str(statement.this).upper() != "EXPLAIN":
        return statement

    tail = statement.expression.this if statement.expression is not None else ""
    # `EXPLAIN QUERY PLAN SELECT …` is the more common SQLite form; the modifier is not part
    # of the query.
    if tail.upper().startswith("QUERY PLAN "):
        tail = tail[len("QUERY PLAN ") :]

    try:
        inner = sqlglot.parse_one(tail, dialect=DIALECT)
    except SqlglotError as exc:
        raise TableExtractionError(f"Could not parse the query behind EXPLAIN: {exc}") from exc
    if inner is None:
        raise TableExtractionError("EXPLAIN carried no statement to analyze")
    return inner


def _assert_analyzable_sources(statement: exp.Expression) -> None:
    """Refuse any `FROM` / `JOIN` source that is not a table, subquery or constant.

    Walking `exp.Table` alone is not enough, because some sources never become a table node.
    A table-valued function is the case that matters: the statement would otherwise be
    authorized against only the tables it *did* declare while the function read whatever it
    liked.
    """
    for node in statement.find_all(exp.From, exp.Join, exp.Lateral):
        source = node.this
        if source is None or not isinstance(source, _ANALYZABLE_SOURCES):
            raise TableExtractionError(
                "Query reads from a function or external source, which cannot be authorized: "
                f"{source.sql(dialect=DIALECT) if source is not None else node.sql(dialect=DIALECT)}"
            )


def resolve_table_reference(table: exp.Table, *, cte_names: set[str]) -> str | None:
    """One table node to its bare lowercase name, or None when it is not a real table."""
    parts = list(table.parts)
    name = table.name

    if not name:
        # A table-valued function modelled as a Table with an empty name and the call in a
        # sibling node. Whatever it touches is not enumerable here.
        raise TableExtractionError("Query reads from a function or external source, which cannot be authorized")

    if len(parts) > 2:
        # SQLite has at most `database.table`. Anything longer is a shape this extractor
        # does not model, and sqlglot would silently drop a component — refuse instead.
        raise TableExtractionError(f"Table names of more than two parts cannot be authorized: {table.sql(DIALECT)}")

    database = (table.db or table.catalog or "").lower()

    if not database and name.lower() in cte_names:
        # A CTE reference, not a table. Only ever unqualified — `WITH x AS (…) SELECT * FROM
        # main.x` names the real `x`, so requiring the qualifier to be empty is what keeps a
        # CTE alias from shadowing a real table of the same name.
        return None

    if database and database not in _OWN_DATABASES:
        # An `ATTACH`ed database: a different file on disk, which this deployment's
        # `path_prefix` resource does not describe. `ATTACH` itself is already refused
        # above, and a connection here never outlives one call, so reaching this means
        # something unexpected — refuse rather than authorize it against the wrong file.
        raise TableExtractionError(f"Attached databases cannot be authorized: {table.sql(DIALECT)}")

    return name.lower()


def normalize_table_name(table: str) -> str:
    """The same normalization, for callers that already have a bare name.

    Used by the discovery tools, which get names from `sqlite_master` rather than from a
    parsed statement. Both paths must produce byte-identical strings, or a table would be
    listed under one spelling and authorized under another.
    """
    return table.lower()
