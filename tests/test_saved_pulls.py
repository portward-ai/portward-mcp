"""A pull can be saved under a name, listed, and re-run by that name like a saved analysis."""

from pathlib import Path

from harness import connected

SQL = "SELECT c.region, sum(o.total) AS revenue FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY 1"
NAME = "Monthly revenue by region"


async def test_a_saved_pull_is_listed_under_its_name(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        pulled = await h.call("query", source="shop", question="Revenue per region", sql=SQL)
        saved = await h.call("save_pull", pull_id=pulled.ai.structured["pull_id"], name=NAME)
        listed = await h.call("saved_pulls")
    assert not saved.is_error, saved.ai.text
    entries = listed.ai.structured["saved"]
    assert [(e["name"], e["id"], e["sql"], e["source"]) for e in entries] == [
        (NAME, pulled.ai.structured["pull_id"], SQL, "shop")]
    assert NAME in listed.ai.text


async def test_a_saved_pull_is_rerun_by_name_and_the_new_run_is_in_history(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, mode="results") as h:
        pulled = await h.call("query", source="shop", question="Revenue per region", sql=SQL)
        await h.call("save_pull", pull_id=pulled.ai.structured["pull_id"], name=NAME)
        await h.call("query", source="shop", question="Something else", sql="SELECT name FROM customers")
        rerun = await h.call("run_saved", name=NAME)
        latest = (await h.call("history", limit=1)).ai.structured["pulls"][0]
    assert not rerun.is_error, rerun.ai.text
    assert rerun.ai.structured["rows"] == [["north", 20833.0], ["south", 10542.0]]
    assert rerun.ai.structured["rerun_of"] == pulled.ai.structured["pull_id"]
    assert rerun.ai.structured["same_result"] is True
    assert latest["id"] == rerun.ai.structured["pull_id"] and latest["sql"] == SQL


async def test_saving_a_name_again_points_it_at_the_new_pull(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db, mode="results") as h:
        old = await h.call("query", source="shop", question="Revenue", sql=SQL)
        await h.call("save_pull", pull_id=old.ai.structured["pull_id"], name=NAME)
        new = await h.call("query", source="shop", question="Customers", sql="SELECT name FROM customers")
        resaved = await h.call("save_pull", pull_id=new.ai.structured["pull_id"], name=NAME)
        listed = (await h.call("saved_pulls")).ai.structured["saved"]
        rerun = await h.call("run_saved", name=NAME)
    assert resaved.ai.structured["replaced"] == old.ai.structured["pull_id"]
    assert [e["id"] for e in listed] == [new.ai.structured["pull_id"]]
    assert rerun.ai.structured["rows"] == [["Ada"], ["Grace"], ["Linus"]]


async def test_saved_names_survive_a_restart(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        pulled = await h.call("query", source="shop", question="Revenue", sql=SQL)
        await h.call("save_pull", pull_id=pulled.ai.structured["pull_id"], name=NAME)
    async with connected(home, shop_db) as h:
        rerun = await h.call("run_saved", name=NAME)
    assert not rerun.is_error, rerun.ai.text


async def test_unknown_names_and_ids_are_errors_that_say_where_to_look(home: Path, shop_db: Path) -> None:
    async with connected(home, shop_db) as h:
        missing_pull = await h.call("save_pull", pull_id="nope", name=NAME)
        missing_name = await h.call("run_saved", name="Never saved")
        pulled = await h.call("query", source="shop", question="Revenue", sql=SQL)
        blank = await h.call("save_pull", pull_id=pulled.ai.structured["pull_id"], name="   ")
        listed = await h.call("saved_pulls")
    assert missing_pull.is_error and "history" in missing_pull.ai.text
    assert missing_name.is_error and "saved_pulls" in missing_name.ai.text
    assert blank.is_error
    assert listed.ai.structured["saved"] == []
    assert "no saved pulls" in listed.ai.text.lower()
