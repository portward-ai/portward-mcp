"""Read back the Python repr Postgres MCP Pro returns from `execute_sql` (`str(list_of_row_dicts)`), without running
it: only literals and the few constructors psycopg's values print as (Decimal, date, time, datetime, timedelta,
timezone, UUID, ZoneInfo) are accepted. Anything else is refused, so a result Portward can't read is never passed on.
"""

from __future__ import annotations

import ast
import datetime
import uuid
import zoneinfo
from decimal import Decimal
from typing import Any

CONSTRUCTORS: dict[str, Any] = {
    "Decimal": Decimal, "decimal.Decimal": Decimal,
    "datetime.date": datetime.date, "datetime.time": datetime.time, "datetime.datetime": datetime.datetime,
    "datetime.timedelta": datetime.timedelta, "datetime.timezone": datetime.timezone,
    "UUID": uuid.UUID, "uuid.UUID": uuid.UUID,
    "zoneinfo.ZoneInfo": zoneinfo.ZoneInfo, "ZoneInfo": zoneinfo.ZoneInfo,
}
CONSTANTS: dict[str, Any] = {"datetime.timezone.utc": datetime.timezone.utc}
MAX_TEXT = 50_000_000  # a result bigger than this isn't read


class NotReadable(ValueError):
    """Not a repr this module reads."""


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    raise NotReadable("unexpected expression")


def _value(node: ast.expr) -> Any:  # noqa: PLR0911 - one case per accepted node
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List | ast.Tuple):
        return [_value(e) for e in node.elts]
    if isinstance(node, ast.Dict):
        if any(k is None for k in node.keys):
            raise NotReadable("unexpected ** in a dict")
        return {_value(k): _value(v) for k, v in zip(node.keys, node.values, strict=True)}  # type: ignore[arg-type]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant) \
            and isinstance(node.operand.value, int | float):
        return -node.operand.value
    if isinstance(node, ast.Attribute) and _dotted(node) in CONSTANTS:
        return CONSTANTS[_dotted(node)]
    if isinstance(node, ast.Call):
        made = CONSTRUCTORS.get(_dotted(node.func))
        if made is None:
            raise NotReadable("unexpected call")
        try:
            return made(*[_value(a) for a in node.args], **{k.arg: _value(k.value) for k in node.keywords if k.arg})
        except (TypeError, ValueError, ArithmeticError, zoneinfo.ZoneInfoNotFoundError) as err:
            raise NotReadable(str(err)) from None
    raise NotReadable(f"unexpected {type(node).__name__}")


def rows_from_repr(text: str) -> list[dict[str, Any]]:
    """The rows in `text`: a list of dicts with string keys."""
    if len(text) > MAX_TEXT:
        raise NotReadable("too big")
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise NotReadable("not a Python literal") from None
    rows = _value(tree.body)
    if not isinstance(rows, list) or not all(isinstance(r, dict) and all(isinstance(k, str) for k in r)
                                             for r in rows):
        raise NotReadable("not a list of rows")
    return rows
