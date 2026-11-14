"""Asking for a source kind Portward can't add from the chat ends in a next step, not a dead end."""

from pathlib import Path

import pytest
from harness import Harness


async def _asked_for(home: Path, kind: str, **fields: object):
    async with Harness.start(home) as h:
        return await h.call("add_source", name="warehouse", kind=kind, **fields)


async def test_asking_for_snowflake_gives_the_command_that_puts_portward_in_front_of_it(home: Path) -> None:
    reply = await _asked_for(home, "snowflake")
    assert reply.is_error
    assert "wrap-add warehouse --provider snowflake" in reply.ai.text
    assert "terminal" in reply.ai.text
    assert "unknown source kind" not in reply.ai.text


async def test_snowflake_is_recognised_however_it_is_written(home: Path) -> None:
    reply = await _asked_for(home, " Snowflake ")
    assert "--provider snowflake" in reply.ai.text


@pytest.mark.parametrize(("kind", "provider"), [("wrap-snowflake", "snowflake"), ("wrap-postgres", "postgres")])
async def test_a_wrapped_server_kind_gives_its_own_command_with_the_sources_name(home: Path, kind: str,
                                                                                 provider: str) -> None:
    reply = await _asked_for(home, kind)
    assert reply.is_error
    assert f"wrap-add warehouse --provider {provider}" in reply.ai.text


@pytest.mark.parametrize("kind", ["bigquery", "databricks", "redshift", "sqlserver", "mssql", "oracle", "parquet"])
async def test_a_kind_portward_cant_read_here_says_what_it_can_and_the_way_round(home: Path, kind: str) -> None:
    reply = await _asked_for(home, kind)
    assert reply.is_error
    assert "CSV or Excel" in reply.ai.text  # the way round: an export of the table
    for reads in ("sqlite", "csv", "excel", "postgres", "mysql"):
        assert reads in reply.ai.text
    assert "wrap-" not in reply.ai.text  # not a list of internal kind names the AI can't use anyway


async def test_a_source_name_cant_smuggle_shell_into_the_command_the_user_is_shown(home: Path) -> None:
    async with Harness.start(home) as h:
        reply = await h.call("add_source", name="x; rm -rf ~", kind="snowflake")
    assert "wrap-add 'x; rm -rf ~' --provider snowflake" in reply.ai.text


async def test_nothing_is_stored_for_a_kind_that_cant_be_added(home: Path) -> None:
    async with Harness.start(home) as h:
        await h.call("add_source", name="warehouse", kind="snowflake")
        await h.call("add_source", name="lake", kind="bigquery")
        assert h.stored("connections") == []
