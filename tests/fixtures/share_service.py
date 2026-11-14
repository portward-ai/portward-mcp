"""The control plane's share service and Personal accounts on 127.0.0.1, for the free tool's sharing and account tests and the view page's browser check.

Runs with the gateway project's Python (it imports `portward`, which the free package never does):
    uv run --project <repo root> python free/tests/fixtures/share_service.py <port> <folder> [--generous]
        [--clock <file>] [--debug-log]
Mail is never sent: every email is appended to <folder>/mail.jsonl as {"to", "subject", "body"}. The database is
<folder>/control.db. Bot checks are cheap (max_number 50) so tests solve them fast.

WorkOS AuthKit is a fake (FakeAuthKit): its authorize address is https://authkit.invalid/authorize?..., and a test
plays the user signing in there by calling the callback with the code `FakeAuthKit.code(email, code_challenge)`.

An address starting "ended-" gets an account made ENDED_AGO seconds back, so its 14-day trial is over: Free (#56).

--clock <file>: the app's clock is the real time plus the seconds written in <file> (a test moves it forward, e.g.
past a trial's end). --debug-log: every log line at DEBUG, access lines included, goes to stderr (what #55's test
scans for plaintext).

Hosted engines (#104) run on the local fake: each one's start settings (its token included) are written to
<folder>/engines/<engine id>.json, and a test starts the engine (portward_free.hosted.engine) from them, as a host
would start the container."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import time
import logging
from pathlib import Path
from urllib.parse import urlencode

import uvicorn

from portward.control.altcha import Altcha
from portward.control.authkit import Identity, SignInRefused
from portward.control.hosted_engines import LocalEngines
from portward.control.app import HOURLY, PER_MINUTE, Control, Settings, build_app
from portward.control.personal_store import PersonalStore
from portward.control.share_store import ShareStore
from portward.control.store import Store
from portward.ratelimit import RateLimiter


ENDED_AGO = 30 * 86_400
PERSONAL_RELAY_DOMAIN = "me.portward.test"  # #103: Personal computers' names on the direct route
PERSONAL_HOSTED_DOMAIN = "hosted.portward.test"  # #104: hosted engines' names


class FixturePersonalStore(PersonalStore):
    def sign_in(self, subject: str, email: str):  # type: ignore[no-untyped-def]
        if not email.startswith("ended-") or self.account_by_email(email) is not None:
            return super().sign_in(subject, email)
        self._now = lambda: time.time() - ENDED_AGO
        try:
            return super().sign_in(subject, email)
        finally:
            self._now = time.time


class FileMail:
    def __init__(self, path: Path) -> None:
        self.path = path

    def send(self, to: str, subject: str, body: str) -> None:
        with self.path.open("a") as out:
            out.write(json.dumps({"to": to, "subject": subject, "body": body}) + "\n")


class FakeAuthKit:
    """WorkOS AuthKit on no network: a code names a verified email and the PKCE challenge it was issued for."""

    @staticmethod
    def code(email: str, challenge: str) -> str:
        return f"{email}|{challenge}"

    def authorize_url(self, *, redirect_uri: str, state: str, code_challenge: str) -> str:
        return "https://authkit.invalid/authorize?" + urlencode(
            {"redirect_uri": redirect_uri, "state": state, "code_challenge": code_challenge})

    def identify(self, *, code: str, code_verifier: str) -> Identity:
        email, _, challenge = code.partition("|")
        made = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
        if not email or made != challenge:
            raise SignInRefused("WorkOS refused the sign-in (invalid_grant)")
        return Identity(self.subject(email), email, "an email code")

    @staticmethod
    def subject(email: str) -> str:
        return "user_" + hashlib.sha256(email.encode()).hexdigest()[:16]

    # #102: AuthKit as the sign-in claude.ai and ChatGPT use for the free tool's MCP over HTTP. A test plays the
    # connector signing the user in by making the access token `FakeAuthKit.mcp_token(email, resource)`.
    @staticmethod
    def mcp_token(email: str, resource: str) -> str:
        return f"mcp|{email}|{resource}"

    def mcp_issuer(self) -> str:
        return "https://authkit.invalid"

    def check_mcp_token(self, *, token: str, resource: str) -> str:
        kind, email, issued_for, *_ = [*token.split("|"), "", ""]
        if kind != "mcp" or not email or issued_for != resource:
            raise SignInRefused("that token didn't check out")
        return self.subject(email)

    @staticmethod
    def mcp_client(*, token: str) -> str | None:
        """#134: the OAuth client a checked token was issued to: `mcp|email|resource|client` (none without it)."""
        parts = token.split("|")
        return parts[3] if len(parts) > 3 and parts[3] else None

    def delete_user(self, subject: str) -> None:  # #58: deleting the account deletes its WorkOS user
        return None

    def logout_url(self, *, session_id: str, return_to: str) -> str:
        return "https://authkit.invalid/logout?" + urlencode({"session_id": session_id, "return_to": return_to})


def main() -> None:
    port, folder = int(sys.argv[1]), Path(sys.argv[2])
    folder.mkdir(parents=True, exist_ok=True)
    now = time.time
    if "--clock" in sys.argv:
        offset = Path(sys.argv[sys.argv.index("--clock") + 1])
        now = lambda: time.time() + float(offset.read_text() or 0) if offset.exists() else time.time()  # noqa: E731
    debug = "--debug-log" in sys.argv
    if debug:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    settings = Settings(public_url=f"http://127.0.0.1:{port}", registry_host="registry.invalid",
                        relay_host="relay.portward.test", personal_relay_domain=PERSONAL_RELAY_DOMAIN,  # #103
                        personal_hosted_domain=PERSONAL_HOSTED_DOMAIN)  # #104
    control = Control(settings, Store(folder / "control.db"), FileMail(folder / "mail.jsonl"), None,
                      Altcha(secrets.token_bytes(32), max_number=50), now=now,
                      shares=ShareStore(folder / "control.db"),
                      personal=FixturePersonalStore(folder / "control.db"), authkit=FakeAuthKit())  # #53
    assert control.personal is not None
    control.personal.hosted_route.host = LocalEngines(folder / "engines")  # #104: a test starts the engine from its file
    if "--generous" in sys.argv:  # the browser check asks for a code at every width and scheme
        control.hourly = RateLimiter({k: 10_000 for k in HOURLY}, window=3600)
        control.minutely = RateLimiter({k: 10_000 for k in PER_MINUTE}, window=60)
    uvicorn.run(build_app(control), host="127.0.0.1", port=port, log_level="debug" if debug else "warning",
                access_log=debug)


if __name__ == "__main__":
    main()
