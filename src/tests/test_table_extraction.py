"""What the table allow-list's input is allowed to be.

A false positive here is an annoyed user; a false negative is a breach — a statement
authorized against a smaller set of tables than it actually touches. So the cases that
matter most are the ones where the extractor must **refuse** rather than answer.
"""

from __future__ import annotations

import pytest

from mcp_sqlite.table_extraction import (
    TableExtractionError,
    extract_referenced_tables,
    normalize_table_name,
)


class TestReads:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("SELECT id, name FROM users ORDER BY id", {"users"}),
            ("SELECT * FROM Users", {"users"}),
            ('SELECT * FROM "Payroll"', {"payroll"}),
            ("SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id", {"users", "orders"}),
            ("SELECT * FROM (SELECT * FROM secrets) s", {"secrets"}),
            ("SELECT * FROM users UNION SELECT * FROM archive_users", {"users", "archive_users"}),
            # EXCEPT and INTERSECT are siblings of UNION in sqlglot, not subclasses, so a
            # source allow-list naming only `exp.Union` would refuse these.
            ("SELECT * FROM users EXCEPT SELECT * FROM archive_users", {"users", "archive_users"}),
            ("SELECT * FROM sqlite_master", {"sqlite_master"}),
        ],
    )
    def test_enumerates_every_table_read(self, query, expected):
        assert extract_referenced_tables(query) == expected

    def test_a_cte_does_not_hide_the_underlying_table(self):
        # The CTE alias is not a table; the table inside it is, and it is the one a rule is
        # written about.
        assert extract_referenced_tables('WITH x AS (SELECT * FROM "Payroll") SELECT * FROM x') == {"payroll"}

    def test_a_cte_alias_does_not_shadow_a_real_qualified_table(self):
        assert extract_referenced_tables("WITH users AS (SELECT 1) SELECT * FROM main.users") == {"users"}

    def test_a_statement_that_reads_no_table_enumerates_to_nothing(self):
        # Legitimately empty, and still authorized: the `path_prefix` resource for the
        # database file is submitted alongside whatever this returns.
        assert extract_referenced_tables("SELECT 1") == set()


class TestWrites:
    """Read/write mode skips `validate_readonly_query` entirely, so these are exactly the
    statements where this extractor is the only thing naming what gets touched."""

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("INSERT INTO users (id, name) VALUES (4, 'Dave')", {"users"}),
            ("UPDATE users SET active = 0 WHERE id = 1", {"users"}),
            ("DELETE FROM users", {"users"}),
            ("DROP TABLE users", {"users"}),
            ("ALTER TABLE users ADD COLUMN note TEXT", {"users"}),
            ("CREATE TABLE new_table (id INTEGER PRIMARY KEY)", {"new_table"}),
            # Both halves: the table created and the table read to fill it.
            ("CREATE TABLE copy AS SELECT * FROM users", {"copy", "users"}),
        ],
    )
    def test_a_write_names_the_table_it_writes(self, query, expected):
        assert extract_referenced_tables(query) == expected


class TestNormalization:
    def test_the_main_qualifier_is_stripped_rather_than_kept(self):
        # If `main.users` and `users` produced different values, a rule denying `users`
        # would be sidestepped by spelling the qualifier out.
        assert extract_referenced_tables("SELECT * FROM main.users") == extract_referenced_tables("SELECT * FROM users")

    def test_the_temp_qualifier_is_stripped_too(self):
        assert extract_referenced_tables("SELECT * FROM temp.scratch") == {"scratch"}

    def test_discovery_and_parsing_agree_on_the_spelling(self):
        # `sqlite_list_tables` gets names from `sqlite_master`, `sqlite_query` gets them from
        # a parsed statement. A disagreement would list a table under one spelling and
        # authorize it under another.
        assert normalize_table_name("Users") == next(iter(extract_referenced_tables("SELECT * FROM Users")))


class TestRefusals:
    """Every case here would otherwise enumerate to a set smaller than the truth."""

    @pytest.mark.parametrize(
        "query",
        [
            # Parses to a node containing no table at all, so the walk would return the
            # empty set — which on an allow-list reads as "touches nothing" and is allowed.
            pytest.param("PRAGMA table_info(users)", id="pragma"),
            pytest.param("ATTACH DATABASE '/tmp/other.db' AS other", id="attach"),
            pytest.param("DETACH DATABASE other", id="detach"),
            pytest.param("REINDEX users", id="reindex"),
            pytest.param("VACUUM", id="vacuum"),
            # Table-valued functions read whatever they like and produce no table node.
            pytest.param("SELECT * FROM pragma_table_info('users')", id="table-valued-function"),
            pytest.param("SELECT * FROM json_each('[1,2]')", id="json-each"),
            # A second statement the walk would never reach.
            pytest.param("SELECT * FROM users; SELECT * FROM orders", id="multi-statement"),
            pytest.param("not sql at all ((", id="unparseable"),
        ],
    )
    def test_refuses_rather_than_under_reporting(self, query):
        with pytest.raises(TableExtractionError):
            extract_referenced_tables(query)

    def test_an_attached_database_is_refused(self):
        # A qualifier that is not `main` or `temp` names a different file on disk, which this
        # deployment's `path_prefix` resource does not describe.
        with pytest.raises(TableExtractionError, match="Attached databases"):
            extract_referenced_tables("SELECT * FROM other.users")


class TestExplain:
    """`validate_readonly_query` advertises EXPLAIN as allowed, so extraction must agree."""

    def test_explain_is_authorized_against_the_tables_the_query_would_read(self):
        assert extract_referenced_tables("EXPLAIN SELECT * FROM users") == {"users"}

    def test_explain_query_plan_too(self):
        query = "EXPLAIN QUERY PLAN SELECT * FROM users u JOIN orders o ON u.id = o.user_id"
        assert extract_referenced_tables(query) == {"users", "orders"}

    def test_explain_over_something_unanalyzable_is_still_refused(self):
        with pytest.raises(TableExtractionError):
            extract_referenced_tables("EXPLAIN PRAGMA table_info(users)")
