"""A profile's context (#95, Personal): what its granted tables and columns mean, and named definitions ("active
customer", "MRR") the AI must use, and links (#96): a column of one table that matches a column of another, in the
same connection or a different one, so the AI joins on exactly those columns instead of guessing among lookalikes.
An imported data dictionary (dictionary.py) lands as meanings and the AI's assumptions (assumptions.py) become named
definitions, each only once the user confirms; until then they stay on this computer.

Context is written in two steps. The AI app connected over MCP drafts it (`Draft`: what it read of the schema and
sample rows); a draft stays on this computer, used for nothing, until the user approves it or their edit of it. Only
then does it join the profile, as part of a whole new version.

Context never mentions anything the profile doesn't grant:
- a meaning can only be drafted for a table or column the profile grants, and one whose grant is later removed is
  no longer shown;
- `mentions` finds the table and column names a text uses, so a text naming something hidden is flagged to the user
  before it's saved (`ungranted`) and is held back from the AI on every connection where it would name something
  hidden (`hides_something`, used by context_served.py). The AI is never told why, so the hold-back itself says nothing.

When two profiles applied to one connection describe the same column or term differently, the AI gets neither until
the user picks one (`Clash`); the pick is remembered for exactly those two texts, so it holds until either changes.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from portward_free.metric_def import Metric
from portward_free.sources import Table
from portward_free.verified_def import VerifiedAnswer, checked_question

if TYPE_CHECKING:
    from portward_free.profiles import Grant

MAX_TEXT, MAX_TERM = 1000, 80
MEANING, DEFINITION, LINK, METRIC = "meaning", "definition", "link", "metric"
VERIFIED = "verified_answer"  # #130


class ContextError(ValueError):
    """Context that can't be drafted or saved as written; the message is safe to show."""


IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,2}")


def _text(text: str, what: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        raise ContextError(f"{what} can't be empty")
    if len(cleaned) > MAX_TEXT:
        raise ContextError(f"keep {what} to {MAX_TEXT} characters")
    return cleaned


@dataclass(frozen=True)
class Meaning:
    """What one granted table (column None) or column means. A table's meaning can name its person column (#122): the
    column that says whose row it is (a customer id), so totals count different people, not rows."""

    table: str
    column: str | None
    text: str
    schema: str | None = None
    person: str | None = None
    # #131: a column's value meanings ("V" -> "void") and sample values, and other words for the table or column.
    # Values and samples are real values: they're given to the AI only where it gets the column in full.
    values: tuple[tuple[str, str], ...] = ()
    samples: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()

    @property
    def about(self) -> str:
        return ".".join(p for p in (self.schema, self.table, self.column) if p)

    @property
    def words(self) -> str:
        """Everything it says in words, to check for names the AI mustn't be given: the text, what each value means
        and the synonyms. (The values and samples themselves are data, given only for columns granted in full.)"""
        return " ".join((self.text, *(said for _, said in self.values), *self.synonyms))

    @property
    def described(self) -> str:
        """The text with its value meanings and synonyms, as clashes compare it (two profiles giving one column
        different samples don't clash: samples are examples, not meaning)."""
        parts = [self.text]
        if self.values:
            parts.append("Values: " + "; ".join(f"{value!r} means {said}" for value, said in self.values) + ".")
        if self.synonyms:
            parts.append("Also called: " + ", ".join(self.synonyms) + ".")
        return " ".join(parts)

    @property
    def shown(self) -> str:
        """Everything it says, samples too, as the user approves it."""
        samples = (" Sample values: " + ", ".join(repr(s) for s in self.samples) + ".") if self.samples else ""
        return self.described + samples

    def extras(self) -> dict[str, Any]:
        return ({"values": dict(self.values)} if self.values else {}) | (
            {"samples": list(self.samples)} if self.samples else {}) | (
            {"synonyms": list(self.synonyms)} if self.synonyms else {})

    @property
    def person_column(self) -> Meaning | None:
        """The person column, as a column of this table (to check it is granted)."""
        return replace(self, column=self.person, person=None) if self.person else None

    @property
    def key(self) -> tuple[str, str]:
        """What it describes, for spotting two descriptions of the same thing: table and column, lower case."""
        return (self.table.lower(), (self.column or "").lower())

    def payload(self) -> dict[str, Any]:
        """What a profile listing shows. Values and samples are left out: they're real values, given with the schema
        only where the AI gets the column in full (context_served)."""
        return {"about": self.about, "meaning": self.text} | ({"person": self.person} if self.person else {}) | (
            {"synonyms": list(self.synonyms)} if self.synonyms else {})

    def stored(self) -> dict[str, Any]:
        return {"schema": self.schema, "table": self.table, "column": self.column, "meaning": self.text} | (
            {"person": self.person} if self.person else {}) | self.extras()

    @classmethod
    def from_stored(cls, data: dict) -> Meaning:
        return cls(data["table"], data.get("column"), data["meaning"], data.get("schema"), data.get("person") or None,
                   tuple((str(k), str(v)) for k, v in (data.get("values") or {}).items()),
                   tuple(str(s) for s in data.get("samples") or ()), tuple(str(s) for s in data.get("synonyms") or ()))


@dataclass(frozen=True)
class Definition:
    """A named definition: `term` means `text`."""

    term: str
    text: str

    @property
    def key(self) -> str:
        return self.term.lower()

    def payload(self) -> dict[str, str]:
        return {"term": self.term, "text": self.text}

    @classmethod
    def from_stored(cls, data: dict) -> Definition:
        return cls(data["term"], data["text"])


def checked_term(term: str) -> str:
    cleaned = " ".join(str(term or "").split())
    if not cleaned or len(cleaned) > MAX_TERM:
        raise ContextError(f"name the definition in 1 to {MAX_TERM} characters")
    return cleaned


@dataclass(frozen=True)
class End:
    """One end of a link: a column, as schema.table.column with the schema optional."""

    table: str
    column: str
    schema: str | None = None

    @property
    def name(self) -> str:
        return ".".join(p for p in (self.schema, self.table, self.column) if p)

    @property
    def key(self) -> tuple[str | None, str, str]:
        return (self.schema.lower() if self.schema else None, self.table.lower(), self.column.lower())

    def stored(self) -> dict[str, str | None]:
        return {"schema": self.schema, "table": self.table, "column": self.column}

    @classmethod
    def from_stored(cls, data: dict) -> End:
        return cls(data["table"], data["column"], data.get("schema"))

    @classmethod
    def parse(cls, text: str) -> End:
        parts = [p.strip() for p in str(text or "").split(".")]
        if len(parts) not in (2, 3) or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in parts):
            raise ContextError(f"{text!r}: name each end of a link as table.column or schema.table.column")
        return cls(parts[-2], parts[-1], parts[0] if len(parts) == 3 else None)


ONE_TO_ONE, ONE_TO_MANY, MANY_TO_ONE, MANY_TO_MANY = "one-to-one", "one-to-many", "many-to-one", "many-to-many"
CARDINALITIES = (ONE_TO_ONE, ONE_TO_MANY, MANY_TO_ONE, MANY_TO_MANY)


def flipped(cardinality: str | None) -> str | None:
    """The cardinality read from the other end ("one-to-many" becomes "many-to-one")."""
    if cardinality is None:
        return None
    left, right = cardinality.split("-to-")
    return f"{right}-to-{left}"


@dataclass(frozen=True)
class Side:
    """One end's measure: rows with a value in the column, and different values among them."""

    rows: int
    values: int

    @property
    def unique(self) -> bool:
        return self.rows == self.values

    def payload(self) -> dict[str, int]:
        return {"rows": self.rows, "values": self.values}


@dataclass(frozen=True)
class Counts:
    """A link's match counts (#131), measured when it was saved on what the AI gets (stand-ins for an anonymised
    column): each end's rows and values, and how many values of `one` appear in `other` (None when the ends are on
    different connections, so they were measured apart)."""

    one: Side
    other: Side
    matching: int | None = None

    @property
    def cardinality(self) -> str | None:
        if not self.one.rows or not self.other.rows:
            return None
        return f"{'one' if self.one.unique else 'many'}-to-{'one' if self.other.unique else 'many'}"

    def stored(self) -> dict[str, Any]:
        return {"one": self.one.payload(), "other": self.other.payload()} | (
            {"matching": self.matching} if self.matching is not None else {})

    @classmethod
    def from_stored(cls, data: dict) -> Counts:
        side = (lambda d: Side(int(d["rows"]), int(d["values"])))
        matching = data.get("matching")
        return cls(side(data["one"]), side(data["other"]), None if matching is None else int(matching))


@dataclass(frozen=True)
class Link:
    """Column `one` holds the same values as column `other` of another table, so their rows join on them. `text`
    says so in the user's words. `cardinality` (#131) says how many rows of each side share a value, read from
    `one` to `other`: measured on save (`counts`), or as drafted when it couldn't be measured."""

    one: End
    other: End
    text: str
    cardinality: str | None = None
    counts: Counts | None = None

    @property
    def about(self) -> str:
        return f"{self.one.name} = {self.other.name}"

    @property
    def key(self) -> tuple[tuple[str | None, str, str], ...]:
        """The same link whichever end is named first."""
        return tuple(sorted((self.one.key, self.other.key), key=repr))

    def payload(self) -> dict[str, Any]:
        return {"columns": [self.one.name, self.other.name], "text": self.text} | (
            {"cardinality": self.cardinality} if self.cardinality else {})

    def stored(self) -> dict[str, Any]:
        return {"one": self.one.stored(), "other": self.other.stored(), "text": self.text} | (
            {"cardinality": self.cardinality} if self.cardinality else {}) | (
            {"counts": self.counts.stored()} if self.counts else {})

    def measured(self, counts: Counts | None) -> Link:
        """The link with counts measured just now; its cardinality follows them when they tell it."""
        if counts is None:
            return self
        return replace(self, counts=counts, cardinality=counts.cardinality or self.cardinality)

    @classmethod
    def from_stored(cls, data: dict) -> Link:
        cardinality = data.get("cardinality")
        return cls(End.from_stored(data["one"]), End.from_stored(data["other"]), data["text"],
                   cardinality if cardinality in CARDINALITIES else None,
                   Counts.from_stored(data["counts"]) if data.get("counts") else None)


def checked_cardinality(cardinality: str | None) -> str | None:
    cleaned = " ".join(str(cardinality or "").split()).lower().replace(" ", "-") or None
    if cleaned is not None and cleaned not in CARDINALITIES:
        raise ContextError(f"say how the columns match as one of {', '.join(CARDINALITIES)}")
    return cleaned


def new_link(column: str, other: str, text: str | None = None, cardinality: str | None = None) -> Link:
    one, two = End.parse(column), End.parse(other)
    if one.key[:2] == two.key[:2]:
        raise ContextError("a link connects columns of two different tables")
    return Link(one, two, _text(text or f"{one.name} holds the same values as {two.name}", "a link"),
                checked_cardinality(cardinality))


@dataclass(frozen=True)
class Context:
    """A profile's approved context."""

    meanings: tuple[Meaning, ...] = ()
    definitions: tuple[Definition, ...] = ()
    links: tuple[Link, ...] = ()
    metrics: tuple[Metric, ...] = ()  # #129
    verified: tuple[VerifiedAnswer, ...] = ()  # #130

    def with_meaning(self, meaning: Meaning) -> Context:
        """This context with `meaning` replacing any earlier meaning of the same table or column."""
        kept = tuple(m for m in self.meanings if m.key != meaning.key)
        return replace(self, meanings=(*kept, meaning))

    def with_definition(self, definition: Definition) -> Context:
        kept = tuple(d for d in self.definitions if d.key != definition.key)
        return replace(self, definitions=(*kept, definition))

    def with_link(self, link: Link) -> Context:
        kept = tuple(k for k in self.links if k.key != link.key)
        return replace(self, links=(*kept, link))

    def with_metric(self, metric: Metric) -> Context:
        """This context with `metric` in place of any earlier metric of the same name. A metric keeps its version
        while its calculation is unchanged and goes up by one when it changes (#129: answers name that version)."""
        earlier = next((m for m in self.metrics if m.key == metric.key), None)
        if earlier is None:
            version = 1
        else:
            version = earlier.version if earlier.same_calculation(metric) else earlier.version + 1
        kept = tuple(m for m in self.metrics if m.key != metric.key)
        return replace(self, metrics=(*kept, replace(metric, version=version)))

    def with_verified(self, answer: VerifiedAnswer) -> Context:
        """This context with `answer` in place of any earlier verified answer to the same question. It keeps its
        version while its SQL and verified result are unchanged and goes up by one when either changes (#130)."""
        earlier = next((v for v in self.verified if v.key == answer.key), None)
        if earlier is None:
            version = 1
        else:
            version = earlier.version if earlier.same_answer(answer) else earlier.version + 1
        kept = tuple(v for v in self.verified if v.key != answer.key)
        return replace(self, verified=(*kept, replace(answer, version=version)))

    def counts(self) -> dict[str, int]:
        return {"meanings": len(self.meanings), "definitions": len(self.definitions), "links": len(self.links),
                "metrics": len(self.metrics), "verified_answers": len(self.verified)}

    def payload(self, grants: Sequence[Grant]) -> dict[str, list[dict[str, Any]]]:
        """What a profile listing shows: everything but a text naming a table.column the profile doesn't grant."""
        metrics = [m.payload() for m in self.metrics if not ungranted(m.text, grants, ())
                   and all(_grants_name(grants, f"{m.table}.{c}") for c in m.columns)]
        # a verified answer's SQL isn't listed: describe_verified_answer gives it where nothing it reads is hidden
        verified = [{**v.brief(), "source": v.source} for v in self.verified if not ungranted(v.question, grants, ())]
        return {"meanings": [m.payload() for m in self.meanings if not ungranted(m.words, grants, ())],
                "definitions": [d.payload() for d in self.definitions if not ungranted(d.text, grants, ())],
                "links": [k.payload() for k in self.links if not ungranted(k.text, grants, ())]} | (
                    {"metrics": metrics} if metrics else {}) | (
                    {"verified_answers": verified} if verified else {})

    def stored(self) -> dict[str, list[dict[str, Any]]]:
        return {"meanings": [m.stored() for m in self.meanings],
                "definitions": [{"term": d.term, "text": d.text} for d in self.definitions],
                "links": [k.stored() for k in self.links]} | (
                    {"metrics": [m.stored() for m in self.metrics]} if self.metrics else {}) | (
                    {"verified_answers": [v.stored() for v in self.verified]} if self.verified else {})

    @classmethod
    def from_stored(cls, data: dict) -> Context:
        return cls(tuple(Meaning.from_stored(m) for m in data.get("meanings", [])),
                   tuple(Definition.from_stored(d) for d in data.get("definitions", [])),
                   tuple(Link.from_stored(k) for k in data.get("links", [])),
                   tuple(Metric.from_stored(m) for m in data.get("metrics", [])),
                   tuple(VerifiedAnswer.from_stored(v) for v in data.get("verified_answers", [])))


def granted_end(end: End, grants: Iterable[Grant]) -> bool:
    """Whether `grants` grant the column at this end of a link."""
    return any(g.table.lower() == end.table.lower() and g.column.lower() == end.column.lower()
               and (end.schema is None or g.schema is None or g.schema.lower() == end.schema.lower())
               for g in grants)


def links_granted(links: Iterable[Link], grants: Iterable[Grant]) -> tuple[Link, ...]:
    """The links both of whose ends `grants` grant: a link never names a column the profile hides."""
    grants = tuple(grants)
    return tuple(k for k in links if granted_end(k.one, grants) and granted_end(k.other, grants))


# --- drafts ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Draft:
    """A meaning or definition the AI proposed for a profile, waiting for the user."""

    id: str
    profile_id: str
    item: Meaning | Definition | Link | Metric | VerifiedAnswer
    drafted_by: str = ""  # the AI app that drafted it, as its MCP client names itself
    drafted_at: str = ""

    @classmethod
    def of(cls, profile_id: str, item: Meaning | Definition | Link | Metric | VerifiedAnswer, by: str) -> Draft:
        return cls(uuid.uuid4().hex[:12], profile_id, item, by)

    @property
    def kind(self) -> str:
        if isinstance(self.item, Metric):
            return METRIC
        if isinstance(self.item, VerifiedAnswer):
            return VERIFIED
        return MEANING if isinstance(self.item, Meaning) else LINK if isinstance(self.item, Link) else DEFINITION

    @property
    def about(self) -> str:
        return self.item.term if isinstance(self.item, Definition) else self.item.about

    @property
    def text(self) -> str:
        return self.item.text

    def edited(self, text: str) -> Draft:
        """The draft with the user's own text (a metric's one-liner)."""
        if isinstance(self.item, Metric):
            words = " ".join(text.split())
            if len(words) > 200:
                raise ContextError("keep the description to one line (200 characters at most)")
            return replace(self, item=replace(self.item, text=words))
        if isinstance(self.item, VerifiedAnswer):  # the user rewords the question; the SQL is what was verified
            try:
                return replace(self, item=replace(self.item, question=checked_question(text)))
            except ValueError as err:
                raise ContextError(str(err)) from None
        return replace(self, item=replace(self.item, text=_text(text, f"a {self.kind}")))

    def onto(self, context: Context) -> Context:
        if isinstance(self.item, Meaning):
            return context.with_meaning(self.item)
        if isinstance(self.item, Link):
            return context.with_link(self.item)
        if isinstance(self.item, Metric):
            return context.with_metric(self.item)
        if isinstance(self.item, VerifiedAnswer):
            return context.with_verified(self.item)
        return context.with_definition(self.item)

    def stored(self) -> dict[str, dict[str, Any]]:
        if isinstance(self.item, Meaning):
            return {MEANING: self.item.stored()}
        if isinstance(self.item, Link):
            return {LINK: self.item.stored()}
        if isinstance(self.item, Metric):
            return {METRIC: self.item.stored()}
        if isinstance(self.item, VerifiedAnswer):
            return {VERIFIED: self.item.stored()}
        return {DEFINITION: {"term": self.item.term, "text": self.item.text}}

    @staticmethod
    def item_from_stored(body: dict) -> Meaning | Definition | Link | Metric | VerifiedAnswer:
        if VERIFIED in body:
            return VerifiedAnswer.from_stored(body[VERIFIED])
        if METRIC in body:
            return Metric.from_stored(body[METRIC])
        if MEANING in body:
            return Meaning.from_stored(body[MEANING])
        if LINK in body:
            return Link.from_stored(body[LINK])
        return Definition.from_stored(body[DEFINITION])


MAX_VALUES, MAX_VALUE, MAX_SAMPLES, MAX_SYNONYMS = 50, 100, 20, 20


def _short(value: object, what: str, most: int) -> str:
    cleaned = " ".join(str(value if value is not None else "").split())
    if not cleaned or len(cleaned) > most:
        raise ContextError(f"each {what} needs 1 to {most} characters")
    return cleaned


def checked_extras(column: str | None, values: dict[str, str] | None, samples: Sequence[str] | None,
                   synonyms: Sequence[str] | None) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...],
                                                            tuple[str, ...]]:
    """A meaning's value meanings, samples and synonyms (#131), cleaned; ContextError when they can't be saved."""
    values, samples, synonyms = dict(values or {}), list(samples or ()), list(synonyms or ())
    if (values or samples) and not column:
        raise ContextError("value meanings and sample values belong to a column: name the column")
    if len(values) > MAX_VALUES or len(samples) > MAX_SAMPLES or len(synonyms) > MAX_SYNONYMS:
        raise ContextError(f"keep to {MAX_VALUES} value meanings, {MAX_SAMPLES} sample values and {MAX_SYNONYMS} "
                           "synonyms")
    pairs = tuple((_short(v, "value", MAX_VALUE), _text(said, "a value's meaning")) for v, said in values.items())
    return (pairs, tuple(dict.fromkeys(_short(s, "sample value", MAX_VALUE) for s in samples)),
            tuple(dict.fromkeys(_short(s, "synonym", MAX_TERM) for s in synonyms)))


def new_meaning(table: str, column: str | None, text: str, person: str | None = None,
                values: dict[str, str] | None = None, samples: Sequence[str] | None = None,
                synonyms: Sequence[str] | None = None) -> Meaning:
    parts = [p.strip() for p in str(table).split(".")]
    if len(parts) not in (1, 2) or not all(parts):
        raise ContextError(f"{table!r}: name the table as table or schema.table")
    column = column.strip() if column and column.strip() else None
    person = person.strip() if person and person.strip() else None
    if person and column:
        raise ContextError(f"a person column belongs on the table's own meaning: leave out the column to name "
                           f"{person!r} as {parts[-1]}'s person column")
    if person and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", person):
        raise ContextError(f"{person!r}: name the person column by its plain column name")
    return Meaning(parts[-1], column, _text(text, "a meaning"), parts[0] if len(parts) == 2 else None, person,
                   *checked_extras(column, values, samples, synonyms))


def new_definition(term: str, text: str) -> Definition:
    return Definition(checked_term(term), _text(text, "a definition"))


# --- what a text mentions -------------------------------------------------------------------------------------------

def mentions(text: str) -> tuple[str, ...]:
    """The identifiers in `text` that could be table or column names: words and dotted names, as written."""
    return tuple(dict.fromkeys(IDENTIFIER.findall(text)))


def _grants_name(grants: Sequence[Grant], dotted: str) -> bool:
    parts = [p.lower() for p in dotted.split(".")]
    table, column = parts[-2], parts[-1]
    schema = parts[0] if len(parts) == 3 else None
    return any(g.table.lower() == table and g.column.lower() == column
               and (schema is None or g.schema is None or g.schema.lower() == schema) for g in grants)


def ungranted(text: str, grants: Sequence[Grant], catalogs: Sequence[Sequence[Table]]) -> tuple[str, ...]:
    """The names in `text` the profile's `grants` don't grant: dotted `table.column` names it doesn't grant, and
    words that are a table or column in one of `catalogs` (the connections it's applied to) it doesn't grant."""
    granted_words = {g.table.lower() for g in grants} | {g.column.lower() for g in grants}
    known: set[str] = set()
    for catalog in catalogs:
        for table in catalog:
            known.add(table.name.lower())
            known.update(c.lower() for c in table.columns)
    found = []
    for name in mentions(text):
        if "." in name:
            if not _grants_name(grants, name) and _names_a_column(name, catalogs, grants):
                found.append(name)
        elif name.lower() in known and name.lower() not in granted_words:
            found.append(name)
    return tuple(found)


def _names_a_column(name: str, catalogs: Sequence[Sequence[Table]], grants: Sequence[Grant]) -> bool:
    """A dotted name counts as a column reference when its table part is a table of the data or of the grants (so
    "e.g." or "U.S." don't), whatever the column part: naming a column that doesn't exist is flagged alike."""
    table = name.split(".")[-2].lower()
    return table in {g.table.lower() for g in grants} or any(
        table == t.name.lower() for catalog in catalogs for t in catalog)


def hides_something(text: str, visible: Sequence[Table], catalog: Sequence[Table]) -> bool:
    """Whether `text` names a table or column of `catalog` the AI can't see here (`visible` is what it can)."""
    shown = {t.name.lower() for t in visible} | {c.lower() for t in visible for c in t.columns}
    columns = {t.name.lower(): {c.lower() for c in t.columns} for t in visible}
    every = {t.name.lower() for t in catalog} | {c.lower() for t in catalog for c in t.columns}
    tables = {t.name.lower() for t in catalog}
    for name in mentions(text):
        parts = [p.lower() for p in name.split(".")]
        if len(parts) == 1:
            if parts[0] in every and parts[0] not in shown:
                return True
        elif parts[-2] in tables and parts[-1] not in columns.get(parts[-2], set()):
            return True
    return False


# --- clashes --------------------------------------------------------------------------------------------------------

def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Clash:
    """Two or more applied profiles describe one column or term differently. `subject` names what's described;
    `candidates` are (profile id, profile name, text)."""

    subject: str  # "meaning:customers.name" or "definition:mrr"
    about: str  # as shown: customers.name, or the term
    candidates: tuple[tuple[str, str, str], ...]

    @property
    def fingerprint(self) -> str:
        """The texts picked between: a pick holds for exactly these, so it's asked again once either changes."""
        return ",".join(sorted(f"{pid}:{text_hash(text)}" for pid, _, text in self.candidates))
