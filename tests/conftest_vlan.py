"""
Dynamic VLAN switching for multi-node LibreMesh mesh tests only.

Single-node tests keep each DUT on its isolated VLAN (as advertised by the
labgrid exporter, e.g. ``192.168.1.1%vlan104``). Only when ``LG_MESH_PLACES``
is set does ``mesh_vlan_multi`` move those DUT ports to mesh VLAN (200) and
set ``TFTP_SERVER_IP``; isolated VLANs are restored on session teardown.

VLAN commands are executed via ``switch-vlan`` (labgrid-switch-abstraction):

- **Remote first**: if ``LG_PROXY`` is set, the command is sent via SSH to the
  proxy host (developer machine scenario). The lab host owns the switch
  credentials and dut-config, so VLAN changes always run there.
- **Local fallback**: if ``LG_PROXY`` is NOT set, ``switch-vlan`` runs locally
  (lab host, CI runner). Requires ``switch-vlan`` in PATH and a configured
  ``SWITCH_DUT_CONFIG`` / ``SWITCH_PASSWORD``.

No local ``dut-config.yaml`` or ``labgrid-switch-abstraction`` install is
required on the developer machine.

For mesh tests: uses ``LG_MESH_PLACES`` (comma-separated) to switch all listed
nodes to VLAN 200.

Set VLAN_SWITCH_DISABLED=1 to skip VLAN switching (e.g. when switch is
already configured manually or running in mesh-only mode).

Place-to-DUT mapping
--------------------

The DUT name passed to ``switch-vlan`` is derived from the labgrid place
name. The default prefix is ``labgrid-fcefyn-`` (FCEFyN lab convention).
Labs using a different naming scheme can override with ``PLACE_PREFIX``
(empty string disables and falls back to stripping up to the second
hyphen, so ``labgrid-<lab>-<dut>`` -> ``<dut>``).
"""

import logging
import os
import shutil
import subprocess

import pytest

logger = logging.getLogger(__name__)

VLAN_MESH = 200
MESH_TFTP_IP_DEFAULT = "192.168.200.1"
DEFAULT_PLACE_PREFIX = "labgrid-fcefyn-"

SWITCH_TIMEOUT_BASE = 30
SWITCH_TIMEOUT_PER_DUT = 10


def _place_to_dut_name(place: str) -> str:
    """Map a labgrid place name to the DUT name expected by ``switch-vlan``.

    Resolution order:
    1. ``PLACE_PREFIX`` env var (explicit override; empty string disables).
    2. Default prefix ``labgrid-fcefyn-`` (FCEFyN lab convention).
    3. Fallback: strip everything up to and including the second hyphen
       (``labgrid-<lab>-<dut>`` -> ``<dut>``).
    """
    prefix_env = os.environ.get("PLACE_PREFIX")
    prefix = prefix_env if prefix_env is not None else DEFAULT_PLACE_PREFIX

    if prefix and place.startswith(prefix):
        return place[len(prefix) :]

    parts = place.split("-", 2)
    if len(parts) == 3:
        return parts[2]
    return place


def _is_disabled() -> bool:
    return os.environ.get("VLAN_SWITCH_DISABLED", "").lower() in ("1", "true", "yes")


def _resolve_proxy_host() -> str | None:
    """Extract SSH host from LG_PROXY (e.g. 'ssh://labgrid-fcefyn' -> 'labgrid-fcefyn')."""
    raw = os.environ.get("LG_PROXY", "").strip()
    if not raw:
        return None
    return raw.removeprefix("ssh://")


def _switch_timeout(num_duts: int) -> int:
    """Compute timeout proportional to DUT count.

    TP-Link JetStream switches process CLI commands slowly (~0.5-1s each).
    With ~5 commands per DUT plus connection setup and config save,
    a fixed 30s timeout is insufficient for batch operations.
    """
    return SWITCH_TIMEOUT_BASE + SWITCH_TIMEOUT_PER_DUT * max(num_duts, 1)


def _run_switch_vlan(args: list[str], *, num_duts: int = 1) -> bool:
    """Run ``switch-vlan`` via SSH to LG_PROXY, or locally if no proxy is set.

    Rationale: ``LG_PROXY`` indicates the lab is at a remote host, so the VLAN
    command must run there (the proxy host owns switch credentials and
    dut-config). Only fall back to local execution when no proxy is set
    (lab host or CI runner).

    Returns True on success, False on failure (logged, never raises).
    """
    proxy = _resolve_proxy_host()
    if proxy:
        cmd = ["ssh", proxy, "switch-vlan " + " ".join(args)]
    elif shutil.which("switch-vlan"):
        cmd = ["switch-vlan", *args]
    else:
        logger.warning(
            "LG_PROXY not set and switch-vlan not in PATH; skipping VLAN command"
        )
        return False

    timeout = _switch_timeout(num_duts)
    logger.debug("VLAN command (timeout=%ds): %s", timeout, cmd)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error("switch-vlan timed out after %ds: %s", timeout, cmd)
        return False

    if result.returncode != 0:
        stderr = result.stderr.strip()
        hint = ""
        if "password required" in stderr.lower() and proxy:
            hint = (
                f"\nHint: switch-vlan ran on '{proxy}' as the SSH user. "
                f"That user needs a readable switch.conf "
                f"(per-user '~/.config/switch.conf' or system-wide "
                f"'/etc/switch.conf' with group access). "
                f"See libremesh-tests CONTRIBUTING_LAB.md."
            )
        logger.error(
            "switch-vlan failed (rc=%d): %s\nstderr: %s%s",
            result.returncode,
            cmd,
            stderr,
            hint,
        )
        return False

    logger.debug("switch-vlan stdout: %s", result.stdout.strip())
    return True


def _switch_to_mesh(dut_names: list[str]) -> bool:
    return _run_switch_vlan([*dut_names, str(VLAN_MESH)], num_duts=len(dut_names))


def _restore_vlans(dut_names: list[str]) -> bool:
    return _run_switch_vlan([*dut_names, "--restore"], num_duts=len(dut_names))


@pytest.fixture(scope="session")
def mesh_vlan_multi():
    """Switch all mesh DUT ports to VLAN 200 for multi-node tests.

    Activates based on LG_MESH_PLACES. Session-scoped: switches VLANs once
    for all mesh tests, restores on session teardown.
    """
    if _is_disabled():
        yield []
        return

    places_str = os.environ.get("LG_MESH_PLACES", "")
    if not places_str:
        yield []
        return

    places = [p.strip() for p in places_str.split(",") if p.strip()]
    dut_names = [_place_to_dut_name(p) for p in places]

    logger.info(
        "Switching %d mesh DUTs to VLAN %d: %s",
        len(dut_names),
        VLAN_MESH,
        dut_names,
    )
    ok = _switch_to_mesh(dut_names)
    if not ok:
        pytest.fail(f"VLAN switch to {VLAN_MESH} failed for DUTs: {dut_names}")

    mesh_tftp_ip = os.environ.get("LG_MESH_TFTP_IP", MESH_TFTP_IP_DEFAULT)
    os.environ["TFTP_SERVER_IP"] = mesh_tftp_ip
    logger.info("Set TFTP_SERVER_IP=%s for VLAN %d", mesh_tftp_ip, VLAN_MESH)

    yield dut_names

    os.environ.pop("TFTP_SERVER_IP", None)
    logger.info("Restoring %d mesh DUTs to isolated VLANs", len(dut_names))
    if _restore_vlans(dut_names):
        logger.info("Restored %d mesh DUTs to isolated VLANs", len(dut_names))
    else:
        # Discarding this used to hide the leak until an unrelated job failed
        # much later: a DUT left on the mesh VLAN is unreachable over its own
        # VLAN, so the next run dies in TFTP or SSH with no mention of VLANs.
        logger.error(
            "VLAN restore FAILED for %s; these DUTs are stranded on VLAN %d. "
            "Run 'switch-vlan --restore-all' on the lab host before the next run.",
            dut_names,
            VLAN_MESH,
        )
