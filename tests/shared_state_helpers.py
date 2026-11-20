"""Helpers for shared-state-async tests (single-node and mesh).

The tests use the ``bat-hosts`` data type because every CI image ships it:
``shared-state-bat_hosts`` is in the default package list and depends on
``shared-state-async``. Its publisher inserts one entry per local interface
MAC, and ``shared-state-async dump`` tags every entry with ``mAuthor``, the
hostname of the node that wrote it. That gives a direct answer to "whose
data does this node hold?":

* isolated single node -> only its own hostname;
* mesh -> its own hostname plus the hostnames of the other nodes.

All functions take a ``run`` callable with the ``SSHDriver.run`` contract:
``run(cmd, timeout=...)`` -> ``(stdout_lines, stderr_lines, exit_code)``.
Both labgrid's SSHDriver (single node) and ``SSHProxy`` (mesh) satisfy it.
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

SS_BIN = "shared-state-async"
SS_DATATYPE = "bat-hosts"
SS_PUBLISHER = "/usr/share/shared-state/publishers/shared-state-publish_bat_hosts"
SS_COMMAND_TIMEOUT = 90


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        logger.warning("Invalid integer for %s=%r, using %d", name, raw, default)
        return default
    return value if value > 0 else default


# How long to wait for the node's own entries to show up after publishing.
SINGLE_NODE_TIMEOUT = _env_int("SHARED_STATE_SINGLE_TIMEOUT", 60)
# How long to wait for automatic synchronization until every mesh node holds
# every other node's data.
MESH_TIMEOUT = _env_int("SHARED_STATE_MESH_TIMEOUT", 300)
POLL_INTERVAL = _env_int("SHARED_STATE_POLL_INTERVAL", 15)
# Allow two automatic synchronization intervals before checking for remote data.
MIN_MESH_SYNC_WAIT = 30

_AUTHOR_RE = re.compile(r'"mAuthor"\s*:\s*"([^"]+)"')


def _text(lines) -> str:
    if isinstance(lines, str):
        return lines
    return "\n".join(lines or [])


def get_hostname(run) -> str:
    stdout, _, rc = run("cat /proc/sys/kernel/hostname", timeout=30)
    hostname = _text(stdout).strip()
    if rc != 0 or not hostname:
        raise RuntimeError(f"could not read hostname (rc={rc})")
    return hostname


def daemon_status(run) -> tuple[bool, bool]:
    """Return ``(installed, running)`` for shared-state-async.

    ``running`` looks for the ``shared-state-async peer`` daemon. The
    ``[s]`` trick keeps grep from matching its own command line (``pgrep
    -f`` would match the ``sh -c`` wrapper of the SSH session instead).
    """
    _, _, rc_installed = run(f"command -v {SS_BIN} >/dev/null", timeout=30)
    _, _, rc_running = run(
        "ps w 2>/dev/null | grep -q '[s]hared-state-async peer'", timeout=30
    )
    return rc_installed == 0, rc_running == 0


def publish(run) -> tuple[int, str]:
    """Force the bat-hosts publisher so the test does not wait for cron."""
    stdout, stderr, rc = run(
        f"test -x {SS_PUBLISHER} || {{ echo 'publisher not installed'; exit 127; }};"
        f" {SS_PUBLISHER}",
        timeout=SS_COMMAND_TIMEOUT,
    )
    return rc, (_text(stdout) + "\n" + _text(stderr)).strip()


def parse_authors(dump_text: str) -> set[str]:
    """Collect every ``mAuthor`` from ``shared-state-async dump`` output.

    Walks the parsed JSON so it does not depend on the exact nesting of the
    dump format; falls back to a regex if the output is not valid JSON.
    """
    try:
        data = json.loads(dump_text)
    except ValueError:
        return set(_AUTHOR_RE.findall(dump_text))

    authors: set[str] = set()
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            author = item.get("mAuthor")
            if isinstance(author, str) and author:
                authors.add(author)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return authors


def dump_authors(run) -> tuple[set[str], str, int]:
    """Return ``(authors, raw_dump, exit_code)`` for the bat-hosts data type."""
    stdout, stderr, rc = run(f"{SS_BIN} dump {SS_DATATYPE}", timeout=SS_COMMAND_TIMEOUT)
    raw = _text(stdout)
    if rc != 0 and not raw:
        raw = _text(stderr)
    return parse_authors(raw), raw, rc


def discover_candidates(run) -> list[str]:
    """Neighbour addresses shared-state would sync with (empty when isolated)."""
    stdout, _, _ = run(
        "shared-state-async-discover 2>/dev/null"
        " || shared-state-get_candidates_neigh 2>/dev/null",
        timeout=60,
    )
    return [line.strip() for line in stdout if line.strip()]


def truncate(text: str, limit: int = 1500) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({len(text) - limit} more bytes)"
