"""Standing filters (#133, Personal): "always leave out test accounts". A profile can say, for a table it grants, which
rows every read keeps. Unlike a row rule (which says what one profile reaches, so profiles widen each other), a
standing filter applies to every read of its table whichever applied profile reaches it: it only ever narrows. It is
enforced through the row rule rewrite (row_cells.py), so it holds through every query shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_local_page import Browser
from test_profile_editor import PREVIEW, SAVE, EditorForm, open_editor, submit
from test_profiles import seen
from test_row_rules_preview import applied, held, lookup, personal
from test_row_rules_leaks import ALL_FULL, DEALS, SHAPES, deals_db, pull, values  # noqa: F401  (fixture)

from portward_free.grants import Grants
from portward_free.profiles import Grant, Profile, ProfileError
from portward_free.row_rules import RowRule
from portward_free.storage import LocalStore
from portward_free.sync.profile_records import _valid_profile, profile_content, profile_from
from portward_free.why_hidden import Kind



def standing(table: str, text: str) -> RowRule:
    return RowRule.parse(table, text, standing=True)


def deals_profile(name: str, *, rows: str | None = None, keep: str | None = None,
                  levels: dict[str, str] = ALL_FULL) -> Profile:
    grants = [Grant.parse(f"deals.{c}", level=lv) for c, lv in levels.items()] + [Grant("owners", "email"),
                                                                               Grant("owners", "team")]
    rules = ((RowRule.parse("deals", rows),) if rows else ()) + ((standing("deals", keep),) if keep else ())
    return Profile.new(name, "", "me@example.com", grants, rows=rules)


NOT_APAC = "region IN ('EU', 'US')"  # leaves out APAC and the row with no region (ids 11, 12, 13)
LEFT_OUT = {11, 12, 13}


def ids(found) -> set[int]:
    return {r[0] for r in found.rows}


@pytest.mark.parametrize("sql", SHAPES)
def test_a_standing_filter_leaves_its_rows_out_of_every_query_shape(deals_db: Path, sql: str) -> None:  # noqa: F811
    found = pull(deals_db, sql, deals_profile("Clean", keep=NOT_APAC))
    seen = values(found)
    assert not any(isinstance(v, str) and ("ivy" in v or "-jo@" in v) for v in seen), found.rows
    assert not any(v in (7700.0, 7800.0, 7900.0) for v in seen), found.rows


def test_a_standing_filter_holds_even_when_another_applied_profile_reaches_every_row(deals_db: Path) -> None:  # noqa: F811
    clean = deals_profile("Clean", keep=NOT_APAC)
    everything = deals_profile("Everything")
    found = pull(deals_db, "SELECT id FROM deals", clean, everything)
    assert ids(found) == {d[0] for d in DEALS} - LEFT_OUT
    assert pull(deals_db, "SELECT COUNT(*) FROM deals", everything, clean).rows == ((10,),)


def test_a_standing_filter_narrows_a_row_rule_of_another_profile(deals_db: Path) -> None:  # noqa: F811
    eu = deals_profile("EU", rows="region = 'EU'")
    no_big = deals_profile("No big deals", keep="amount < 1500", levels={"id": "full"})
    assert ids(pull(deals_db, "SELECT id FROM deals", eu, no_big)) == {1, 2, 3}


def test_a_profile_can_hold_a_row_rule_and_a_standing_filter_on_one_table_but_not_two_filters() -> None:
    both = deals_profile("Both", rows="region = 'EU'", keep="amount < 1500")
    assert both.row_rule("deals") == RowRule.parse("deals", "region = 'EU'")
    assert both.standing_filter("deals") == standing("deals", "amount < 1500")
    with pytest.raises(ProfileError):
        Profile.new("Two", "", "me", [Grant("deals", "id")],
                    rows=(standing("deals", "amount < 1"), standing("deals", "amount < 2")))


def test_a_standing_filter_is_stored_synced_and_checked_like_a_row_rule() -> None:
    store = LocalStore.memory()
    made = store.profiles.save(deals_profile("Clean", rows="region = 'EU'", keep=NOT_APAC))
    assert store.profiles.by_id(made.id).rows == made.rows
    content = profile_content(made)
    assert _valid_profile(content) and profile_from(content).rows == made.rows
    assert profile_from(content).standing_filter("deals") is not None
    tampered = json.loads(json.dumps(content))
    tampered["rows"][1]["standing"] = "yes"
    assert not _valid_profile(tampered)


def test_held_rows_count_as_rules_changed_when_a_standing_filter_changes() -> None:
    from portward_free.rules import policy_from

    clean = Grants.applied([deals_profile("Clean", keep=NOT_APAC)])
    assert policy_from(json.loads(json.dumps(clean.payload()))) == clean
    assert Grants.applied([deals_profile("Clean", keep="region = 'EU'")]) != clean
    assert Grants.applied([deals_profile("Clean")]) != clean


# --- through the product: the editor, the AI log, why-hidden ------------------------------------------------------

FILTER_FIELD = "f:deals"


async def test_a_standing_filter_set_in_the_editor_is_enforced_and_named_in_the_ai_log(home: Path,
                                                                                      deals_db: Path) -> None:  # noqa: F811
    browser = Browser()
    async with personal(home, deals_db, browser) as h:
        applied(home, deals_profile("All deals"), deals_profile("Also all deals"))
        url, page = await open_editor(h, browser, "All deals")
        assert EditorForm(page.body).fields[FILTER_FIELD] == ""
        previewed = submit(url, page, PREVIEW, **{FILTER_FIELD: NOT_APAC})
        assert "1 change since v1" in previewed.body
        saved = submit(url, previewed, SAVE)
        assert saved.status == 303, saved.body[:400]  # fewer rows: nothing to ask
        reply = await h.call("query", source="deals", question="?", sql="SELECT owner_email, amount FROM deals")
        logged = await h.call("ai_log")
        _, again = await open_editor(h, browser, "All deals")
    assert held(home, "All deals").standing_filter("deals").text == NOT_APAC
    assert held(home, "All deals").row_rule("deals") is None
    assert "ivy" not in seen(reply) and "7900" not in seen(reply)
    assert f"standing filter deals: {NOT_APAC} (All deals v2)" in logged.ai.text
    assert EditorForm(again.body).fields[FILTER_FIELD] == NOT_APAC
    assert "standing filter" not in reply.ai.text.lower()


async def test_removing_a_standing_filter_from_an_applied_profile_asks_first(home: Path,
                                                                             deals_db: Path) -> None:  # noqa: F811
    browser = Browser()
    async with personal(home, deals_db, browser) as h:
        applied(home, deals_profile("Clean", keep=NOT_APAC))
        url, page = await open_editor(h, browser, "Clean")
        asked = submit(url, page, SAVE, **{FILTER_FIELD: ""})
        assert asked.status == 200 and "standing filter" in asked.body.lower()
        assert held(home, "Clean").version == 1
        submit(url, page, SAVE, **{FILTER_FIELD: "", "confirm": "yes"})
    assert held(home, "Clean").standing_filter("deals") is None


async def test_why_hidden_says_which_rows_a_standing_filter_leaves_out(home: Path, deals_db: Path) -> None:  # noqa: F811
    async with personal(home, deals_db):
        applied(home, deals_profile("Clean", keep=NOT_APAC), deals_profile("Everything"))
    found = lookup(home, deals_db)
    (rows,) = found.about("deals")
    assert rows.kind is Kind.ROWS and "standing filter" in rows.why and NOT_APAC in rows.why and "Clean" in rows.why
    assert "every row" not in rows.why  # "Everything" reaches every row, but the filter still applies
