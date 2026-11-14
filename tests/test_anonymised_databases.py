"""Anonymised grants (#93) on real Postgres and MySQL: stand-ins are made inside the database (no extension needed),
match the reference every source reproduces (so they match SQLite, files and the other database), and the key
never appears in the SQL the AI could see an error about."""

from __future__ import annotations

import os

import pytest
from conftest import Database

from portward_free import stand_ins
from portward_free.context import new_link
from portward_free.grants import Grants
from portward_free.guard import QueryRejected
from portward_free.profiles import ANONYMISED, Grant
from portward_free.pull import run_pull
from portward_free.sources import SourceError, open_source

KEY = os.urandom(32)
GRANTS = Grants.of([Grant("customers", "name", level=ANONYMISED), Grant("customers", "id", level=ANONYMISED),
                    Grant("customers", "region"), Grant("orders", "customer_id", level=ANONYMISED),
                    Grant("orders", "total")],
                   [new_link("orders.customer_id", "customers.id")])  # stand-ins match only across links (#121)


def name_stand_in(value: str) -> str | None:
    return stand_ins.stand_in_for(KEY, value, GRANTS.scopes.group("customers", "name"))


def id_stand_in(value: str) -> str | None:
    return stand_ins.stand_in_for(KEY, value, GRANTS.scopes.group("orders", "customer_id"))


def pulled(database: Database, sql: str):
    source = open_source(database.kind, database.fields(), database.password())
    try:
        return run_pull("shop", source, "?", sql, GRANTS, lambda: KEY)
    finally:
        source.close()


@pytest.mark.integration
def test_stand_ins_are_made_in_the_database_and_match_every_other_source(database: Database) -> None:
    pull = pulled(database, "SELECT id, name, region FROM customers ORDER BY region, name")
    expected = {name_stand_in(n) for n in ("Ada", "Grace", "Linus")}
    assert {r[1] for r in pull.rows} == expected  # text, as the reference makes it
    assert {r[0] for r in pull.rows} == {id_stand_in(i) for i in ("1", "2", "3")}  # whole numbers
    assert dict(pull.real_values)[name_stand_in("Ada")] == "Ada"  # for the user's panel only


@pytest.mark.integration
def test_filtering_grouping_and_joining_on_stand_ins_work(database: Database) -> None:
    ada = name_stand_in("Ada")
    filtered = pulled(database, f"SELECT region FROM customers WHERE name = '{ada}'")
    real_value = pulled(database, "SELECT region FROM customers WHERE name = 'Ada'")
    joined = pulled(database, "SELECT c.name, sum(o.total) AS spent FROM orders o "
                              "JOIN customers c ON c.id = o.customer_id GROUP BY c.name")
    assert filtered.rows == (("north",),)
    assert real_value.rows == ()  # a real value never matches a stand-in
    assert {r[0] for r in joined.rows} == {ada, name_stand_in("Grace")}


@pytest.mark.integration
def test_the_key_never_appears_in_what_the_ai_could_be_told(database: Database) -> None:
    pads = [p.hex() for p in stand_ins.hmac_pads(KEY)]
    try:
        pulled(database, "SELECT name / 0 AS x FROM customers")
    except (SourceError, QueryRejected) as err:
        message = str(err)
    else:
        message = ""
    assert not any(p[:32] in message for p in pads)
