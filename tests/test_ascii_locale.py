"""Claude Desktop starts the extension with no UTF-8 locale, so the server must name the encoding of every text it
reads or writes: under an ASCII locale the results panel's page failed to decode and the server never started
(Claude Desktop, 2026-11-04)."""

import json
import subprocess
import sys
from pathlib import Path

from memory_keyring import server_env

ASCII = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONIOENCODING": ""}
INITIALIZE = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def test_the_server_starts_and_answers_under_an_ascii_locale(tmp_path: Path) -> None:
    env = server_env(PORTWARD_HOME=str(tmp_path), **ASCII)
    done = subprocess.run([sys.executable, "-X", "utf8=0", "-m", "portward_free"], env=env, cwd=tmp_path,
                          input=json.dumps(INITIALIZE) + "\n", capture_output=True, text=True, encoding="utf-8",
                          timeout=60)
    answers = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
    assert answers and answers[0].get("id") == 0 and "result" in answers[0], done.stderr[-2000:]
