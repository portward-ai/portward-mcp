"""LocalWeb's one-time options (#62's password page): it can expire, and a response can close it after it is sent."""

import time
import urllib.parse
from http.client import HTTPConnection

import pytest

from portward_free.localweb import LocalWeb, Request, Response


def get(url: str) -> int:
    """A request's status; -1 when nothing listens there (closed)."""
    parts = urllib.parse.urlsplit(url)
    connection = HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        connection.request("GET", parts.path)
        return connection.getresponse().status
    except (ConnectionRefusedError, ConnectionResetError):
        return -1
    finally:
        connection.close()


def wait_closed(url: str, seconds: float = 5) -> int:
    deadline = time.monotonic() + seconds
    status = get(url)
    while status != -1 and time.monotonic() < deadline:
        time.sleep(0.05)
        status = get(url)
    return status


def test_a_response_can_close_the_server_once_it_is_sent() -> None:
    def app(request: Request, web: LocalWeb) -> Response:
        return Response(200, "done", close_after=request.path == "/done")

    web = LocalWeb(app).start()
    assert get(web.url("/")) == 200
    assert get(web.url("/done")) == 200
    assert wait_closed(web.url("/")) == -1


def test_a_server_can_expire() -> None:
    web = LocalWeb(lambda request, web: Response(200, "hi"), expires_in=0.3).start()
    assert get(web.url("/")) == 200
    time.sleep(0.4)
    assert wait_closed(web.url("/")) in (-1, 410)


def test_a_failing_app_is_a_500_and_prints_nothing(capfd: pytest.CaptureFixture[str]) -> None:
    def app(request: Request, web: LocalWeb) -> Response:
        raise RuntimeError("something with a secret in it")

    web = LocalWeb(app).start()
    try:
        status = get(web.url("/"))
    finally:
        web.stop()
    _, err = capfd.readouterr()
    assert status == 500
    assert "secret" not in err and "Traceback" not in err
