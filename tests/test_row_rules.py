"""Row rules (#132, Personal): a profile can limit a granted table to rows matching fixed values. Each cell takes the
highest level among the profiles whose row rule matches its row, and a value anonymised on any row is anonymised on
every row. Rules are parsed and re-rendered by sqlglot from a small grammar; raw SQL is never accepted."""

from __future__ import annotations

import pytest

from portward_free.row_rules import RowRule, RowRuleError


@pytest.mark.parametrize("text, shown", [
    ("region = 'EU'", "region = 'EU'"),
    ("region IN ('EU', 'US')", "region IN ('EU', 'US')"),
    ("amount >= 100 AND region <> 'APAC'", "amount >= 100 AND region <> 'APAC'"),
    ("(region = 'EU' OR region = 'US') AND amount < 2.5", "(region = 'EU' OR region = 'US') AND amount < 2.5"),
    ("'EU' = region", "region = 'EU'"),
    ("100 < amount", "amount > 100"),
    ("region != 'EU'", "region <> 'EU'"),
    ("amount > -3", "amount > -3"),
])
def test_a_row_rule_is_parsed_and_rewritten_in_portwards_own_words(text: str, shown: str) -> None:
    assert RowRule.parse("deals", text).text == shown


@pytest.mark.parametrize("text", [
    "region = 'EU'; DROP TABLE deals",
    "region = (SELECT region FROM deals LIMIT 1)",
    "region IN (SELECT region FROM other)",
    "lower(region) = 'eu'",
    "region = owner_email",
    "1 = 1",
    "region LIKE 'E%'",
    "NOT region = 'EU'",
    "region IS NULL",
    "region = 'EU' OR TRUE",
    "deals.region = 'EU'",
    "other.region = 'EU'",
    "amount + 1 > 3",
    "region = current_setting('x')",
    "region = @x",
    "region = ?",
    "",
    "region",
    "SELECT * FROM deals",
    "region IN ()",
    "region = NULL",
])
def test_anything_outside_values_in_lists_comparisons_and_and_or_is_refused(text: str) -> None:
    with pytest.raises(RowRuleError):
        RowRule.parse("deals", text)


def test_a_rule_round_trips_through_its_stored_form_without_any_sql() -> None:
    rule = RowRule.parse("deals", "region IN ('EU', 'US') AND amount > 10", schema="main")
    stored = rule.stored()
    assert "IN" not in str(stored["where"])  # structured, never SQL text
    assert RowRule.from_stored(stored) == rule


@pytest.mark.parametrize("stored", [
    {"table": "deals", "where": "region = 'EU'"},
    {"table": "deals", "where": {"op": "raw", "sql": "1=1"}},
    {"table": "deals", "where": {"op": "=", "column": "region", "value": {"x": 1}}},
    {"table": "deals", "where": {"op": "=", "column": "region", "value": True}},
    {"table": "deals", "where": {"op": "in", "column": "region", "values": []}},
    {"table": "deals", "where": {"op": "and", "parts": []}},
    {"table": 3, "where": {"op": "=", "column": "region", "value": "EU"}},
    {"table": "deals", "where": {"op": "=", "column": "a.b", "value": "EU"}},
])
def test_a_stored_rule_that_isnt_in_the_grammar_is_refused(stored: dict) -> None:
    with pytest.raises(RowRuleError):
        RowRule.from_stored(stored)
