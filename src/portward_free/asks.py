"""Open asks for a secret the user types out of band: a database password, the sharing confirmation code.

An `Ask` says what is asked for (shown to the user in the panel form or the local page) and how a typed secret is
saved (`save`, which raises `Refused` with a safe message when it isn't). `Requests` holds the open asks by a
single-use unguessable token: each expires, closes once answered, and closes after MAX_ATTEMPTS wrong answers. The
token reaches only the panel (in `_meta`, or from the app-only `secret_request` tool) or the local page's server;
never the AI. Each ask also has a public `id` (no use without the panel's tool, which the model can't call): it goes
in the visible result so the panel can read the ask in hosts that don't hand it `_meta`.

Open asks live in the local store, not in memory: a client can run two server processes on one app-data folder
(Claude Desktop: one gets the AI's calls, the other the panel's), so the ask opened by one must be answerable in the
other. The store keeps only the token's SHA-256 hash, and the ask's `kind` and non-secret `params`; the other process
rebuilds the ask (its `save`) from them with the builder registered for that kind.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from portward_free.storage import LocalStore, OpenAsk

REQUEST_MINUTES = 10  # how long a panel form or local page can still take the secret
MAX_ATTEMPTS = 5  # wrong answers one form or page takes before it closes
PANEL_META = "portward/secret_request"  # the panel form's ask, in a result's _meta (the AI never sees _meta)
REQUEST_ID = "request_id"  # the ask's public handle, in the visible result, for hosts that drop _meta
ALLOW = "allow"  # what an allow ask's one button sends (the panel's and the local page's); nothing is typed
CLOSED = "this form has expired or was already used; ask the AI to try again"


class Refused(Exception):
    """The secret wasn't saved. The message is safe to show: it never contains the secret."""


class Outcome(Protocol):
    def summary(self) -> str: ...

    def payload(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Ask:
    key: str  # what it is for; a new ask for the same key closes the old one
    title: str  # "Database password", "Sharing code"
    label: str  # the field's label: "Password", "Code"
    details: tuple[tuple[str, str], ...]  # what the user checks before typing (where a password goes)
    done: str  # the sentence the panel can put in the chat once it is saved
    save: Callable[[str], Outcome]  # raises Refused
    kind: str = ""  # how another server process rebuilds this ask (Requests.kinds); "" only answers in this one
    params: Mapping[str, Any] = field(default_factory=dict)  # what the kind's builder needs: never secret
    hidden: bool = True  # the field hides what's typed (a password, a code)
    allow: bool = False  # no field: one button labelled `label` sends ALLOW (letting the AI read a file, #115)


@dataclass(frozen=True)
class Request:
    token: str
    ask: Ask
    expires_at: float
    attempts: int = 0
    id: str = ""  # public handle: says which ask, but can't answer it

    def form(self) -> dict[str, Any]:
        """What the panel form needs, its token included: never shown to the AI."""
        return {"token": self.token, "title": self.ask.title, "label": self.ask.label,
                "details": [list(d) for d in self.ask.details], "done": self.ask.done, "hidden": self.ask.hidden,
                "allow": self.ask.allow}

    def panel_meta(self) -> dict[str, Any]:
        return {PANEL_META: self.form()}


Builder = Callable[[Mapping[str, Any]], Ask]


def _hashed(token: Any) -> str | None:
    return hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) and token else None


class Requests:
    """The open asks, kept in `store` (shared by every server process on it). Thread-safe: a local page answers on
    its own thread. `kinds` rebuilds an ask another process opened."""

    def __init__(self, store: LocalStore | None = None, minutes: float | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._store = store if store is not None else LocalStore.memory()
        self._seconds = (REQUEST_MINUTES if minutes is None else minutes) * 60
        self._clock = clock  # wall-clock seconds: expiry has to read the same in every process
        self._lock = threading.Lock()
        self._made_here: dict[str, Ask] = {}  # asks this process opened, by token hash, as they were made
        self.kinds: dict[str, Builder] = {}

    def open(self, ask: Ask) -> Request:
        token, ask_id = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
        request = Request(token, ask, self._clock() + self._seconds, id=ask_id)
        hashed = _hashed(token)
        assert hashed is not None
        with self._lock:
            self._store.close_asks(expired_by=self._clock())
            self._store.put_ask(OpenAsk(hashed, ask_id, ask.key, ask.kind, dict(ask.params), request.expires_at))
            self._made_here[hashed] = ask
        return request

    def cancel(self, key: str) -> None:
        """Close every ask for `key` (what it was for changed, or is gone), whichever process opened it."""
        with self._lock:
            self._store.close_asks(key)
            self._made_here = {h: a for h, a in self._made_here.items() if a.key != key}

    def _found(self, token_hash: str | None = None, ask_id: str | None = None) -> tuple[OpenAsk, Ask]:
        """The open ask and how to save it; Refused when it is unknown, expired, closed or can't be rebuilt here."""
        with self._lock:
            row = (self._store.open_ask(token_hash) if token_hash is not None
                   else self._store.open_ask(ask_id=ask_id) if isinstance(ask_id, str) and ask_id else None)
            if row is not None and row.expires_at <= self._clock():
                self._store.close_ask(row.token_hash)
                row = None
            if row is None:
                raise Refused(CLOSED)
            ask = self._made_here.get(row.token_hash)
        if ask is None:
            builder = self.kinds.get(row.kind)
            if builder is None:
                raise Refused(CLOSED)
            ask = builder(row.params)
        return row, ask

    def answer(self, token: str, secret: str) -> Outcome:
        """Save the secret for the ask `token` names. Saved closes the ask; a refused one counts as an attempt."""
        hashed = _hashed(token)
        if hashed is None:
            raise Refused(CLOSED)
        _, ask = self._found(hashed)
        try:
            outcome = ask.save(secret)
        except Refused:
            with self._lock:
                attempts = self._store.ask_attempted(hashed)
                if attempts is not None and attempts >= MAX_ATTEMPTS:
                    self._store.close_ask(hashed)
                    self._made_here.pop(hashed, None)
            raise
        with self._lock:
            self._store.close_ask(hashed)
            self._made_here.pop(hashed, None)
        return outcome

    def by_id(self, request_id: str) -> Request:
        """The open ask with public handle `request_id`, for the panel to show (Refused if none). It gets a new
        token, returned here (the store keeps only its hash): the one handed out before stops working."""
        row, ask = self._found(ask_id=request_id)
        token = secrets.token_urlsafe(32)
        hashed = _hashed(token)
        assert hashed is not None
        with self._lock:
            if not self._store.rotate_ask(row.token_hash, hashed):
                raise Refused(CLOSED)
            made = self._made_here.pop(row.token_hash, None)
            if made is not None:
                self._made_here[hashed] = made
        return Request(token, ask, row.expires_at, row.attempts, id=row.id)

    def seconds_left(self, request: Request) -> float:
        return max(request.expires_at - self._clock(), 0.0)

    def is_open(self, token: str) -> bool:
        hashed = _hashed(token)
        with self._lock:
            row = self._store.open_ask(hashed) if hashed is not None else None
            return row is not None and row.expires_at > self._clock()
