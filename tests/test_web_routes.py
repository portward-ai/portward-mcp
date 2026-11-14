"""Which web routes a connection is offered (#103, #90 story 31): files and databases only this computer can reach
get the direct route only, so nothing is uploaded to reach them. A database on the internet gets both, hosted
first (the default)."""

from __future__ import annotations

import pytest

from portward_free.remote.routes import offered_routes
from portward_free.route_names import DIRECT, HOSTED


@pytest.mark.parametrize(("kind", "settings"), [
    ("csv", {"path": "/Users/sam/orders.csv"}),
    ("excel", {"path": "/Users/sam/q3.xlsx"}),
    ("sqlite", {"path": "/Users/sam/shop.db"}),
    ("postgres", {"host": "localhost", "port": 5432, "database": "shop", "user": "sam"}),
    ("postgres", {"host": "127.0.0.1"}), ("postgres", {"host": "::1"}),
    ("mysql", {"host": "192.168.1.20"}), ("mysql", {"host": "10.0.0.5"}), ("mysql", {"host": "172.20.0.2"}),
    ("postgres", {"host": "100.101.102.103"}),  # a tailnet (CGNAT) address
    ("postgres", {"host": "fd00::5"}), ("postgres", {"host": "169.254.0.9"}),
    ("postgres", {"host": "db.local"}), ("postgres", {"host": "nas"}), ("postgres", {"host": "pg.internal"}),
    ("postgres", {"host": "db.localhost"}), ("postgres", {"host": "db.home.arpa"}),
    ("postgres", {}), ("wrapped-postgres-mcp", {}),  # anything unknown: the safe answer
])
def test_files_and_laptop_only_databases_are_offered_only_the_direct_route(kind: str, settings: dict) -> None:
    assert offered_routes(kind, settings) == (DIRECT,)


@pytest.mark.parametrize("host", ["db.example.com", "abc123.us-east-2.rds.amazonaws.com", "8.8.8.8"])
def test_a_database_on_the_internet_is_offered_hosted_first_and_direct(host: str) -> None:
    assert offered_routes("postgres", {"host": host, "port": 5432}) == (HOSTED, DIRECT)
    assert offered_routes("mysql", {"host": host.upper()}) == (HOSTED, DIRECT)
