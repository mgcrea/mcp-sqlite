"""Tests for SQL read-only validation — SQLite variant."""

import pytest

from mcp_sqlite.sql_validation import ReadOnlyViolationError, validate_readonly_query


class TestAllowedQueries:
    """Queries that SHOULD be allowed through validation."""

    def test_simple_select(self):
        validate_readonly_query("SELECT * FROM users")

    def test_select_with_where(self):
        validate_readonly_query("SELECT id, name FROM users WHERE active = 1")

    def test_select_with_join(self):
        validate_readonly_query("SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id")

    def test_select_with_subquery(self):
        validate_readonly_query("SELECT * FROM users WHERE id IN (SELECT user_id FROM orders)")

    def test_cte_with_select(self):
        validate_readonly_query(
            "WITH active_users AS (SELECT * FROM users WHERE active = 1) SELECT * FROM active_users"
        )

    def test_multiple_ctes(self):
        validate_readonly_query("WITH cte1 AS (SELECT 1 AS a), cte2 AS (SELECT 2 AS b) SELECT * FROM cte1, cte2")

    def test_select_with_trailing_semicolon(self):
        validate_readonly_query("SELECT 1;")

    def test_select_with_string_containing_keywords(self):
        validate_readonly_query("SELECT * FROM logs WHERE message = 'DELETE FROM users'")

    def test_select_with_comment_containing_keywords(self):
        validate_readonly_query("/* This deletes nothing */ SELECT * FROM users")

    def test_select_with_line_comment(self):
        validate_readonly_query("-- just a comment\nSELECT * FROM users")

    def test_select_with_double_quoted_identifier(self):
        validate_readonly_query('SELECT "DELETE", "DROP" FROM "INSERT"')

    def test_explain_select(self):
        validate_readonly_query("EXPLAIN SELECT * FROM users")

    def test_explain_query_plan(self):
        validate_readonly_query("EXPLAIN QUERY PLAN SELECT * FROM users")

    def test_select_count(self):
        validate_readonly_query("SELECT COUNT(*) FROM users")

    def test_select_with_aggregate_functions(self):
        validate_readonly_query(
            "SELECT department, AVG(salary) FROM employees GROUP BY department HAVING AVG(salary) > 50000"
        )

    def test_select_case_variations(self):
        validate_readonly_query("select * from users")
        validate_readonly_query("Select * From Users")

    def test_select_with_leading_whitespace(self):
        validate_readonly_query("   SELECT * FROM users")

    def test_select_with_escaped_quotes_in_string(self):
        validate_readonly_query("SELECT * FROM users WHERE name = 'O''Brien'")


class TestBlockedQueries:
    """Queries that MUST be blocked by validation."""

    def test_insert(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("INSERT INTO users (name) VALUES ('test')")

    def test_update(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("UPDATE users SET name = 'test' WHERE id = 1")

    def test_delete(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("DELETE FROM users WHERE id = 1")

    def test_drop_table(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("DROP TABLE users")

    def test_alter_table(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("ALTER TABLE users ADD COLUMN age INTEGER")

    def test_create_table(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("CREATE TABLE evil (id INTEGER)")

    def test_truncate(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("TRUNCATE TABLE users")

    def test_grant(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("GRANT ALL ON users TO evil_user")

    def test_revoke(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("REVOKE ALL ON users FROM some_user")

    # --- SQLite-specific attacks ---

    def test_replace(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("REPLACE INTO users (id, name) VALUES (1, 'test')")

    def test_attach_database(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("ATTACH DATABASE '/tmp/evil.db' AS evil")

    def test_detach_database(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("DETACH DATABASE evil")

    def test_pragma(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("PRAGMA journal_mode=WAL")

    def test_select_into(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("SELECT * INTO new_table FROM users")

    def test_vacuum(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("VACUUM")

    def test_reindex(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("REINDEX users")

    # --- Bypass attempts ---

    def test_comment_before_insert(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("/* harmless */ INSERT INTO users VALUES (1)")

    def test_multi_statement_select_then_drop(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("SELECT 1; DROP TABLE users")

    def test_multi_statement_select_then_delete(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("SELECT * FROM users; DELETE FROM users")

    def test_line_comment_before_insert(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("-- comment\nINSERT INTO users VALUES (1)")

    def test_nested_block_comments(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("/* /* nested */ */ DELETE FROM users")

    def test_empty_query(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("")

    def test_whitespace_only_query(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("   \n\t  ")

    def test_cte_with_insert(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query("WITH cte AS (SELECT * FROM users) INSERT INTO archive SELECT * FROM cte")

    def test_cte_with_delete(self):
        with pytest.raises(ReadOnlyViolationError):
            validate_readonly_query(
                "WITH cte AS (SELECT * FROM users) DELETE FROM users WHERE id IN (SELECT id FROM cte)"
            )
