#!/usr/bin/env python3
"""Boot a single labgrid node and hold the session until signaled to stop.

This script is designed to be launched as a subprocess by conftest_mesh.py.
Each node runs in its own process, avoiding labgrid's internal thread-safety
issues (StepLogger, coordinator multi-session crashes) that prevent
in-process parallel booting.

Usage:
    python mesh_boot_node.py \
        --place labgrid-fcefyn-belkin_rt3200_2 \
        --image /srv/tftp/firmwares/belkin_rt3200/libremesh/lime-24.10.5-mediatek-mt7622-linksys_e8450-initramfs-kernel.bin \
        --target-yaml /path/to/targets/linksys_e8450.yaml \
        --status-file /tmp/mesh_node_XYZ.json \
        --stop-file /tmp/mesh_stop_XYZ

Set LG_MESH_KEEP_POWERED=1 (or true/yes) to leave the node powered on when stopping.
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lime_helpers import (
    _read_int_env,
    configure_fixed_ip,
    ensure_batman_mesh,
    generate_mesh_ssh_ip,
    query_node_ip,
    suppress_kernel_console,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("mesh_boot_node")

STOP_POLL_INTERVAL = 2
DEFAULT_BOOT_ATTEMPTS = 3
DEFAULT_RETRY_COOLDOWN = 8
DEFAULT_COORDINATOR = "localhost:20408"
EXCEPTION_SUMMARY_MAX_LEN = 240
CONSOLE_DUMP_READ_CHUNK = 8192
CONSOLE_DUMP_READ_TIMEOUT = 1.0
CONSOLE_DUMP_TOTAL_MAX_SEC = 3.0
STOP_REQUESTED = False


class BootFailure(RuntimeError):
    """Structured boot failure that can be reported back to the parent fixture."""

    def __init__(
        self,
        stage: str,
        error_type: str,
        summary: str,
        retriable: bool = True,
        attempts_used: int = 1,
    ):
        super().__init__(summary)
        self.stage = stage
        self.error_type = error_type
        self.summary = summary
        self.retriable = retriable
        self.attempts_used = attempts_used

    def to_status(self, place: str) -> dict:
        return {
            "place": place,
            "ok": False,
            "error": self.summary,
            "failure_stage": self.stage,
            "attempts_used": self.attempts_used,
            "error_type": self.error_type,
            "error_summary": self.summary,
        }


def _boot_attempt_limit() -> int:
    return max(1, _read_int_env("LG_MESH_BOOT_ATTEMPTS", DEFAULT_BOOT_ATTEMPTS))


def _boot_deadline():
    """Return the monotonic deadline for this node's whole boot, or None.

    The parent fixture watches a wall clock and SIGKILLs this subprocess when
    it expires. Without the same bound here, the boot attempt loop would keep
    starting attempts it cannot finish (3 boot attempts, each allowing 3
    U-Boot captures at up to ~145s on a PoE port, is ~20min against a ~10min
    window), so the node always died mid-attempt with no diagnosis instead of
    reporting why it failed. The parent passes its own budget in
    LG_MESH_BOOT_BUDGET.
    """
    budget = _read_int_env("LG_MESH_BOOT_BUDGET", 0)
    if budget <= 0:
        return None
    return time.monotonic() + budget


def _boot_retry_cooldown() -> int:
    for env_name in ("LG_MESH_BOOT_RETRY_COOLDOWN", "LG_MESH_UBOOT_RETRY_COOLDOWN"):
        if os.environ.get(env_name, "").strip():
            return _read_int_env(env_name, DEFAULT_RETRY_COOLDOWN)
    return DEFAULT_RETRY_COOLDOWN


def _check_stop_requested():
    """Abort promptly when the parent or operator requested shutdown."""
    if STOP_REQUESTED:
        raise BootFailure(
            stage="shutdown",
            error_type="shutdown_requested",
            summary="Shutdown requested while booting",
            retriable=False,
        )


def _summarize_exception(exc: Exception, limit: int = EXCEPTION_SUMMARY_MAX_LEN) -> str:
    text = str(exc).strip()
    if not text:
        text = type(exc).__name__
    summary = f"{type(exc).__name__}: {text}"
    if len(summary) > limit:
        return summary[: limit - 3] + "..."
    return summary


def _error_type_for_stage(stage: str, exc: Exception) -> str:
    text = str(exc).lower()
    exc_name = type(exc).__name__.lower()
    if stage == "acquire_place":
        return "place_acquire_failure"
    if stage == "transition_to_uboot":
        if "waiting for login" in text or "timeout" in exc_name:
            return "uboot_prompt_timeout"
        return "uboot_activation_failure"
    if stage == "tftp_download":
        return "tftp_download_failure"
    if stage == "boot_kernel":
        return "kernel_boot_failure"
    if stage == "activate_shell":
        if "timeout" in exc_name:
            return "shell_activation_timeout"
        return "shell_activation_failure"
    if stage == "post_shell":
        return "post_shell_setup_failure"
    return "boot_failure"


def _raise_stage_failure(stage: str, exc: Exception, retriable: bool) -> None:
    raise BootFailure(
        stage=stage,
        error_type=_error_type_for_stage(stage, exc),
        summary=_summarize_exception(exc),
        retriable=retriable,
    ) from exc


def _decode_console_bytes(raw: bytes) -> str:
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return repr(raw)


def _collect_pexpect_unmatched(strategy) -> bytes:
    """Return the bytes pexpect received but did not match at the failure point.

    Labgrid console drivers hold a pexpect instance at ``console._expect``
    that exposes ``before`` (pre-match/timeout content) and ``buffer`` (lookahead
    buffer). Both together give the last thing the DUT actually printed before
    ShellDriver._await_login ran out of time. This is the data that matters,
    not new bytes, because after the timeout the DUT may be idle (askfirst
    consoles on Belkin-class devices wait silently for Enter).
    """
    console = getattr(strategy, "console", None)
    if console is None:
        return b""
    expect_obj = getattr(console, "_expect", None)
    if expect_obj is None:
        return b""
    parts = []
    for attr in ("before", "buffer"):
        value = getattr(expect_obj, attr, None)
        if not value:
            continue
        if isinstance(value, str):
            value = value.encode("utf-8", errors="replace")
        parts.append(value)
    return b"".join(parts)


def _drain_console_new_bytes(strategy) -> bytes:
    """Drain any bytes still flowing from the DUT after the failure."""
    console = getattr(strategy, "console", None)
    if console is None:
        return b""
    deadline = time.monotonic() + CONSOLE_DUMP_TOTAL_MAX_SEC
    chunks = []
    try:
        while time.monotonic() < deadline:
            data = console.read(
                size=CONSOLE_DUMP_READ_CHUNK, timeout=CONSOLE_DUMP_READ_TIMEOUT
            )
            if not data:
                break
            chunks.append(data)
    except Exception:
        logger.debug("Console new-bytes drain failed", exc_info=True)
    return b"".join(chunks)


def _hex_dump_tail(raw: bytes, max_bytes: int = 512) -> str:
    """Return a human-readable hex+ASCII dump of the last bytes of raw."""
    tail = raw[-max_bytes:]
    lines = []
    for off in range(0, len(tail), 16):
        chunk = tail[off : off + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:04x}  {hex_part:<48}  {ascii_part}")
    return "\n".join(lines)


def _probe_shell_prompt(strategy, raw: bytes) -> str:
    """Apply the ShellDriver prompt regex to the captured buffer ourselves.

    If labgrid's own _await_login could not match the prompt inside the buffer
    but our manual compile does, the mismatch points to an environment-level
    issue (buffer reset, driver races, encoding) rather than a bad pattern.
    """
    try:
        target = getattr(strategy, "target", None)
        if target is None:
            return "no target"
        shell = target.get_driver("ShellDriver", activate=False)
        pattern = getattr(shell, "prompt", None)
        if not pattern:
            return "no prompt attribute"
        import re

        compiled = re.compile(pattern.encode() if isinstance(pattern, str) else pattern)
        matches = list(compiled.finditer(raw))
        if not matches:
            return f"pattern={pattern!r} -> NO MATCH in {len(raw)} bytes"
        last = matches[-1]
        return (
            f"pattern={pattern!r} -> {len(matches)} matches; "
            f"last at offset {last.start()}..{last.end()}: {last.group()!r}"
        )
    except Exception as exc:
        return f"probe failed: {type(exc).__name__}: {exc}"


def _dump_console_tail(strategy, place: str, stage: str) -> None:
    """Dump both the pexpect-unmatched buffer and any still-arriving bytes.

    When activate_shell times out on a remote tunnel, the immediately useful
    diagnostic is what the DUT actually printed before we gave up. Pexpect
    keeps that as ``before`` after a TIMEOUT; the post-mortem drain captures
    any late output that arrived during our sampling window.

    Also emits a hex dump of the last bytes and a manual re-apply of the
    ShellDriver prompt regex, so invisible-byte mismatches (ANSI codes,
    encoding, non-breaking spaces) become visible. Best-effort: failures
    here must not mask the original boot failure.
    """
    unmatched = _collect_pexpect_unmatched(strategy)
    new_bytes = _drain_console_new_bytes(strategy)
    if not unmatched and not new_bytes:
        logger.info("=== Console tail for %s at %s: (empty) ===", place, stage)
        return
    if unmatched:
        logger.info(
            "=== Pexpect unmatched for %s at %s (%d bytes) ===\n%s\n=== End unmatched ===",
            place,
            stage,
            len(unmatched),
            _decode_console_bytes(unmatched),
        )
        logger.info(
            "=== Unmatched tail hex (%d bytes) for %s ===\n%s\n=== End hex ===",
            min(len(unmatched), 512),
            place,
            _hex_dump_tail(unmatched),
        )
        logger.info(
            "=== ShellDriver prompt probe for %s: %s ===",
            place,
            _probe_shell_prompt(strategy, unmatched),
        )
    if new_bytes:
        logger.info(
            "=== Post-timeout console for %s at %s (%d bytes) ===\n%s\n=== End post-timeout ===",
            place,
            stage,
            len(new_bytes),
            _decode_console_bytes(new_bytes),
        )


def _patch_reg_driver_idempotent():
    """Make target_factory.reg_driver idempotent for multiple Environment objects."""
    from labgrid.factory import target_factory

    if hasattr(target_factory.reg_driver, "__wrapped__"):
        return

    original = target_factory.reg_driver

    def idempotent(cls):
        if cls.__name__ in target_factory.all_classes:
            return target_factory.all_classes[cls.__name__]
        return original(cls)

    idempotent.__wrapped__ = original
    target_factory.reg_driver = idempotent


def _write_status(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _boot_node_once(place: str, target, strategy) -> dict:
    """Execute one staged boot attempt after the place has been acquired."""
    _check_stop_requested()
    logger.info("Booting %s to shell...", place)

    try:
        strategy.transition_to_uboot_with_retry()
    except Exception as exc:
        _raise_stage_failure("transition_to_uboot", exc, retriable=True)

    try:
        strategy.run_download_commands()
    except Exception as exc:
        _raise_stage_failure("tftp_download", exc, retriable=True)

    try:
        strategy.boot_kernel()
    except Exception as exc:
        _raise_stage_failure("boot_kernel", exc, retriable=True)

    try:
        strategy.activate_shell()
    except Exception as exc:
        _dump_console_tail(strategy, place, "activate_shell")
        _raise_stage_failure("activate_shell", exc, retriable=True)

    logger.info("Shell ready on %s", place)
    shell = target.get_driver("ShellDriver")

    try:
        suppress_kernel_console(shell)
        ensure_batman_mesh(target, place_name=place)
        mesh_ssh_ip = generate_mesh_ssh_ip(place)
        fixed_ip = configure_fixed_ip(
            shell,
            target,
            place_name=place,
            fixed_ip=mesh_ssh_ip,
            prefer_networkservice=False,
        )
        if fixed_ip:
            ssh_ip = fixed_ip
            logger.info("Node %s using mesh SSH IP %s", place, ssh_ip)
        else:
            ssh_ip = query_node_ip(target, prefer_mesh=True)
            logger.info("Node %s using fallback SSH IP %s", place, ssh_ip)

        mesh_ip = query_node_ip(target, prefer_mesh=True)
        if mesh_ip:
            logger.info("Node %s mesh IP %s", place, mesh_ip)
        else:
            mesh_ip = ssh_ip
            logger.info(
                "Node %s mesh IP unavailable, falling back to %s", place, mesh_ip
            )
    except Exception as exc:
        _raise_stage_failure("post_shell", exc, retriable=True)

    return {
        "ssh_ip": ssh_ip,
        "mesh_ip": mesh_ip,
    }


def _reset_after_failed_attempt(strategy, place: str):
    """Best-effort reset between boot attempts."""
    try:
        logger.info("Resetting %s to off before retry", place)
        strategy.transition("off")
    except Exception:
        logger.warning("Failed to reset %s to off before retry", place, exc_info=True)


def _run_boot_attempts(
    place: str, target, strategy, boot_attempts: int, retry_cooldown: int
) -> dict:
    """Run staged boot attempts until one succeeds or the retry budget is exhausted."""
    last_failure = None
    deadline = _boot_deadline()

    for attempt in range(1, boot_attempts + 1):
        _check_stop_requested()
        logger.info("Boot attempt %d/%d for %s", attempt, boot_attempts, place)
        started = time.monotonic()
        try:
            result = _boot_node_once(place, target, strategy)
            result["attempts_used"] = attempt
            return result
        except BootFailure as exc:
            exc.attempts_used = attempt
            last_failure = exc
            attempt_cost = time.monotonic() - started
            logger.warning(
                "Boot attempt %d/%d for %s failed after %.0fs at %s (%s): %s",
                attempt,
                boot_attempts,
                place,
                attempt_cost,
                exc.stage,
                exc.error_type,
                exc.summary,
            )
            if not exc.retriable or attempt >= boot_attempts:
                raise

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining < attempt_cost + retry_cooldown:
                    logger.error(
                        "Not retrying %s: remaining budget %.0fs cannot fit another "
                        "attempt (last one took %.0fs + %ds cooldown)",
                        place,
                        max(0.0, remaining),
                        attempt_cost,
                        retry_cooldown,
                    )
                    raise

            _reset_after_failed_attempt(strategy, place)
            logger.info("Retrying boot of %s in %ds", place, retry_cooldown)
            for _ in range(retry_cooldown):
                _check_stop_requested()
                time.sleep(1)

    if last_failure:
        raise last_failure
    raise BootFailure(
        stage="boot_attempt",
        error_type="boot_failure",
        summary=f"Unknown boot failure for {place}",
        retriable=False,
        attempts_used=boot_attempts,
    )


def boot_node(place: str, image: str, target_yaml: str, coordinator: str) -> dict:
    """Boot a node via labgrid and return session state needed by the parent."""
    _patch_reg_driver_idempotent()

    from labgrid import Environment
    from labgrid.remote.client import start_session

    os.environ["LG_PLACE"] = place
    os.environ["LG_IMAGE"] = image

    try:
        env = Environment(config_file=target_yaml)

        args = argparse.Namespace(
            place=place,
            state=None,
            initial_state=None,
            allow_unmatched=False,
        )
        session = start_session(
            coordinator, extra={"env": env, "role": "main", "args": args}
        )
        loop = session.loop

        try:
            loop.run_until_complete(session.acquire())
        except Exception:
            logger.warning("Acquire failed for %s, releasing stale lock first", place)
            try:
                loop.run_until_complete(session.release())
            except Exception:
                pass
            loop.run_until_complete(session.acquire())
        logger.info("Place %s acquired", place)

        lg_place = session.get_place(place)
        target = session._get_target(lg_place)
        strategy = target.get_driver("Strategy")
    except Exception as exc:
        _raise_stage_failure("acquire_place", exc, retriable=False)

    boot_attempts = _boot_attempt_limit()
    retry_cooldown = _boot_retry_cooldown()
    result = _run_boot_attempts(place, target, strategy, boot_attempts, retry_cooldown)
    result.update(
        {
            "session": session,
            "target": target,
            "strategy": strategy,
        }
    )
    return result


def _should_keep_powered() -> bool:
    """Check LG_MESH_KEEP_POWERED to leave devices on for debugging."""
    val = os.environ.get("LG_MESH_KEEP_POWERED", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def shutdown_node(session, strategy, place: str):
    """Power off and release a node. Skips power-off if LG_MESH_KEEP_POWERED is set."""
    if not _should_keep_powered():
        try:
            logger.info("Powering off %s", place)
            strategy.transition("off")
        except Exception:
            logger.warning("Power off failed for %s", place, exc_info=True)
    else:
        logger.info(
            "LG_MESH_KEEP_POWERED set: leaving %s powered on (place will be released)",
            place,
        )
    try:
        session.loop.run_until_complete(session.release())
        logger.info("Released %s", place)
    except Exception:
        logger.warning("Release failed for %s", place, exc_info=True)


def main():
    parser = argparse.ArgumentParser(description="Boot a single labgrid mesh node")
    parser.add_argument("--place", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--target-yaml", required=True)
    parser.add_argument("--coordinator", default=DEFAULT_COORDINATOR)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--stop-file", required=True)
    args = parser.parse_args()

    stopping = False
    received_signal = None

    def handle_signal(signum, _frame):
        global STOP_REQUESTED
        nonlocal stopping, received_signal
        STOP_REQUESTED = True
        stopping = True
        received_signal = signum

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    session = None
    strategy = None

    try:
        state = boot_node(args.place, args.image, args.target_yaml, args.coordinator)
        session = state["session"]
        strategy = state["strategy"]

        _write_status(
            args.status_file,
            {
                "place": args.place,
                "ok": True,
                "ssh_ip": state.get("ssh_ip", ""),
                "mesh_ip": state.get("mesh_ip", ""),
                "attempts_used": state.get("attempts_used", 1),
            },
        )
        logger.info("Status written to %s, waiting for stop signal", args.status_file)

        while not stopping:
            if Path(args.stop_file).exists():
                logger.info("Stop file detected: %s", args.stop_file)
                break
            time.sleep(STOP_POLL_INTERVAL)

        if received_signal is not None:
            logger.info("Shutting down after signal %d", received_signal)

    except BootFailure as exc:
        logger.exception("Failed to boot %s", args.place)
        _write_status(args.status_file, exc.to_status(args.place))
        sys.exit(1)
    except Exception as exc:
        logger.exception("Failed to boot %s", args.place)
        failure = BootFailure(
            stage="boot_attempt",
            error_type=_error_type_for_stage("boot_attempt", exc),
            summary=_summarize_exception(exc),
            retriable=False,
        )
        _write_status(args.status_file, failure.to_status(args.place))
        sys.exit(1)
    finally:
        if session and strategy:
            shutdown_node(session, strategy, args.place)


if __name__ == "__main__":
    main()
