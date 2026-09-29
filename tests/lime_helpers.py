"""Shared helpers for LibreMesh testing.

Functions used by both single-node (conftest.py) and multi-node
(conftest_mesh.py, mesh_boot_node.py) test paths. Centralised here to
avoid duplication and keep a single source of truth.
"""

import hashlib
import ipaddress
import logging
import os
import re
import time
from os import getenv
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def _read_int_env(name: str, default: int) -> int:
    """Return a non-negative integer from the environment, or *default*."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid integer for %s=%r, using default %d", name, raw, default
        )
        return default
    if value < 0:
        logger.warning(
            "Negative integer for %s=%r, using default %d", name, raw, default
        )
        return default
    return value


MESH_SSH_IP_PREFIX = "10.13.200"
# Backward-compatible alias kept for older callers and tests.
FIXED_IP_PREFIX = MESH_SSH_IP_PREFIX
MESH_IP_NETWORK = ipaddress.IPv4Network("10.13.0.0/16")
MESH_SSH_IP_NETWORK = ipaddress.IPv4Network(f"{MESH_SSH_IP_PREFIX}.0/24")
# Backward-compatible alias kept for older callers and tests.
FIXED_IP_NETWORK = MESH_SSH_IP_NETWORK

REPO_ROOT = Path(__file__).resolve().parent.parent


def resolve_labnet_path(repo_root: Path | None = None) -> Path:
    """Return the path to ``labnet.yaml`` (devices, labs).

    Resolution order:
    1. ``LABNET_PATH`` env var (explicit file path).
    2. ``OPENWRT_TESTS_DIR/labnet.yaml`` env var (root of an aparcar/openwrt-tests checkout).
    3. Sibling clone ``<repo_parent>/openwrt-tests/labnet.yaml``.

    *repo_root* is the libremesh-tests root (contains ``targets/``).
    The global ``labnet.yaml`` is maintained in
    https://github.com/aparcar/openwrt-tests and is not duplicated here.
    """
    root = repo_root if repo_root is not None else REPO_ROOT

    explicit = os.environ.get("LABNET_PATH", "").strip()
    if explicit:
        path = Path(os.path.expanduser(explicit)).resolve()
        if not path.is_file():
            raise FileNotFoundError(
                f"LABNET_PATH is set but file not found: {path} "
                "(check LABNET_PATH or unset it)"
            )
        return path

    openwrt_dir = os.environ.get("OPENWRT_TESTS_DIR", "").strip()
    if openwrt_dir:
        path = Path(os.path.expanduser(openwrt_dir)).resolve() / "labnet.yaml"
        if not path.is_file():
            raise FileNotFoundError(
                f"OPENWRT_TESTS_DIR is set but labnet.yaml not found at {path}. "
                "Point OPENWRT_TESTS_DIR at the root of an aparcar/openwrt-tests clone, "
                "or set LABNET_PATH to the file directly."
            )
        return path

    sibling = root.parent / "openwrt-tests" / "labnet.yaml"
    if sibling.is_file():
        return sibling.resolve()

    raise FileNotFoundError(
        "labnet.yaml not found. Set LABNET_PATH to the file, or:\n"
        "  - Clone https://github.com/aparcar/openwrt-tests next to this repo\n"
        f"    (expected: {root.parent / 'openwrt-tests' / 'labnet.yaml'}), or\n"
        "  - Set OPENWRT_TESTS_DIR to the root of that checkout."
    )


# ---------------------------------------------------------------------------
# Serial console helpers
# ---------------------------------------------------------------------------


def suppress_kernel_console(shell) -> None:
    """Set kernel console log level to ALERT-only (``dmesg -n 1``).

    Kernel messages (e.g. batman-adv interface activation) printed to the
    serial console interfere with pexpect prompt detection, causing TIMEOUT
    errors in ShellDriver.run().  Must be called once after the shell is
    ready and before any other serial commands.
    """
    try:
        shell.run("dmesg -n 1")
    except Exception:
        logger.debug("dmesg -n 1 failed (non-critical)")


def assert_libremesh_runtime(shell) -> None:
    """Hard-fail if the booted DUT is not a LibreMesh image.

    The full repo is named ``libremesh-tests`` for a reason: every test path
    assumes the DUT is running LibreMesh, not vanilla OpenWrt. Historically
    when the CI build silently dropped the ``lime-*`` packages (or the DUT's
    U-Boot fell back to autobooting from on-flash OpenWrt), the tests still
    matched the catch-all ``root@.*?[#$]`` prompt against ``root@OpenWrt`` /
    ``root@(none)`` and went on to fail mysteriously much later (SSH
    timeouts, ``batctl_not_found`` warnings, br-lan never coming up).

    Detect that here, right after the shell is ready, and abort the run with
    an unmistakable diagnostic:

    - ``/etc/lime_release`` is shipped by the ``lime-system`` package
      (templated by [packages/lime-system/Makefile]) so its presence proves
      ``lime-system`` is actually installed in the booted rootfs.
    - ``batctl`` proves ``batctl-default`` (and the batman-adv kernel module
      it pulls in) made it into the image.

    Set ``LIBREMESH_RUNTIME_CHECK=0`` to skip (e.g. when intentionally
    booting a non-LibreMesh image during local debugging).
    """
    if os.environ.get("LIBREMESH_RUNTIME_CHECK", "1").strip().lower() in (
        "0",
        "false",
        "no",
    ):
        logger.warning(
            "LIBREMESH_RUNTIME_CHECK disabled — skipping LibreMesh runtime guard"
        )
        return

    try:
        _, _, rc_release = shell.run("test -f /etc/lime_release")
    except Exception as exc:
        logger.error("Failed to query /etc/lime_release: %s", exc)
        rc_release = 1

    try:
        _, _, rc_batctl = shell.run("command -v batctl >/dev/null 2>&1")
    except Exception as exc:
        logger.error("Failed to query batctl availability: %s", exc)
        rc_batctl = 1

    if rc_release == 0 and rc_batctl == 0:
        try:
            release_lines, _, _ = shell.run("cat /etc/lime_release 2>/dev/null")
            if release_lines:
                logger.info("LibreMesh runtime guard OK: %s", release_lines[0].strip())
        except Exception:
            logger.info("LibreMesh runtime guard OK")
        return

    logger.error(
        "LibreMesh runtime guard FAILED: lime_release_present=%s, batctl_present=%s",
        rc_release == 0,
        rc_batctl == 0,
    )

    diagnostic_cmds = (
        ("banner", "cat /etc/banner 2>/dev/null | head -10"),
        ("openwrt_release", "cat /etc/openwrt_release 2>/dev/null"),
        ("hostname", "uname -n; cat /proc/sys/kernel/hostname 2>/dev/null"),
        (
            "lime_packages",
            "(apk list -I 2>/dev/null || opkg list-installed 2>/dev/null) | "
            "grep -E '^(lime|shared-state|batctl|babeld)' || "
            "echo '(no lime/shared-state/batctl/babeld packages installed)'",
        ),
        ("dmesg_tail", "dmesg 2>/dev/null | tail -30"),
    )
    for label, cmd in diagnostic_cmds:
        try:
            output, _, _ = shell.run(cmd)
            logger.error("=== %s ===\n%s", label, "\n".join(output) if output else "")
        except Exception as exc:
            logger.error("Failed to capture %s: %s", label, exc)

    import pytest as _pytest

    _pytest.exit(
        "DUT booted a non-LibreMesh image (missing /etc/lime_release and/or batctl). "
        "The CI build did not produce a LibreMesh firmware, or U-Boot autobooted from "
        "flash instead of TFTP. See diagnostic dump above (banner / openwrt_release / "
        "hostname / installed packages / dmesg). Set LIBREMESH_RUNTIME_CHECK=0 to "
        "bypass during local debugging.",
        returncode=4,
    )


# ---------------------------------------------------------------------------
# Fixed IP helpers
# ---------------------------------------------------------------------------


def generate_mesh_ssh_ip(place_name: str) -> str:
    """Generate a deterministic mesh SSH/control IP in the 10.13.200.x range.

    The last octet is derived from a hash of the place name, ensuring each
    DUT gets a unique, repeatable address (range: .1 - .254).
    """
    md5_hash = hashlib.md5(place_name.encode()).hexdigest()
    hash_value = int(md5_hash[:8], 16) % 253 + 1
    return f"{MESH_SSH_IP_PREFIX}.{hash_value}"


# Backward-compatible alias for older imports.
generate_fixed_ip = generate_mesh_ssh_ip


def extract_ipv4_addresses(output: list[str] | str) -> list[str]:
    """Extract unique IPv4 addresses from command output, preserving order."""
    text = "\n".join(output) if isinstance(output, list) else output
    seen = set()
    addresses = []
    for addr in re.findall(r"\b(\d+\.\d+\.\d+\.\d+)\b", text):
        if addr in seen:
            continue
        seen.add(addr)
        addresses.append(addr)
    return addresses


def is_mesh_ipv4(address: str) -> bool:
    """Return True for real LibreMesh br-lan addresses, excluding mesh SSH IPs
    and network/broadcast addresses."""
    try:
        ip_addr = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        return False
    if ip_addr not in MESH_IP_NETWORK or ip_addr in FIXED_IP_NETWORK:
        return False
    return (
        ip_addr != MESH_IP_NETWORK.network_address
        and ip_addr != MESH_IP_NETWORK.broadcast_address
    )


def select_primary_ipv4(addresses: list[str] | str) -> str | None:
    """Return the first IPv4 address found, or None when no address exists."""
    extracted = extract_ipv4_addresses(addresses)
    return extracted[0] if extracted else None


def select_mesh_ipv4(addresses: list[str] | str) -> str | None:
    """Return the first real LibreMesh IPv4 address, excluding mesh SSH IPs."""
    for address in extract_ipv4_addresses(addresses):
        if is_mesh_ipv4(address):
            return address
    return None


def get_ssh_target_ip(target) -> str | None:
    """Extract the IP that SSHDriver will connect to from NetworkService.

    The SSHDriver binds to a NetworkService resource whose address comes from
    the exporter config (for example ``192.168.1.1%vlan104`` in isolated mode).
    We must configure the DUT with that same IP so SSH can reach it when that
    path is preferred.
    """
    try:
        ssh = target.get_driver("SSHDriver", activate=False)
        addr = ssh.networkservice.address
        logger.info("NetworkService address for SSH: %s", addr)
        if "%" in addr:
            addr = addr.split("%")[0]
        return addr if addr else None
    except Exception as e:
        logger.warning("Could not read NetworkService address: %s", e)
        return None


def _install_mesh_vlan_proxy_patch() -> None:
    """Force labgrid's SSH ProxyCommand to bind to the mesh VLAN.

    ``mesh_vlan_multi`` (and ``conftest_mesh`` boot paths) move DUTs to the
    shared mesh VLAN (200) and set ``TFTP_SERVER_IP``.  The exporter, however,
    advertises SSH as
    ``192.168.1.1%vlan104`` (its isolated control plane).  Mutating
    ``ssh.networkservice.address`` does not survive activation: the next
    ``Target.update_resources()`` call (triggered when ``get_driver`` activates
    the SSHDriver) repolls ``RemoteNetworkService`` from the coordinator and
    restores ``%vlan104``, so ``labgrid-bound-connect vlan104 ...`` still gets
    used and SSH times out.

    Patch :class:`ProxyManager.get_command` instead: when ``TFTP_SERVER_IP`` is
    set (mesh mode), swap the parsed ``ifname`` for ``LG_MESH_VLAN_IFACE``
    (default ``vlan200``) before the ``labgrid-bound-connect`` argv is built.
    The control plane address inside ``-l root <ip>%<iface>`` is irrelevant
    when ProxyCommand handles the transport.
    """
    try:
        from labgrid.util import proxy as _lg_proxy
    except Exception as exc:
        logger.debug("Cannot patch labgrid ProxyManager: %s", exc)
        return

    pm_cls = _lg_proxy.ProxyManager
    if getattr(pm_cls, "_libremesh_mesh_vlan_patched", False):
        return

    original_get_command = pm_cls.get_command

    def patched_get_command(cls, res, host, port, ifname=None):
        if ifname and os.environ.get("TFTP_SERVER_IP", "").strip():
            new_ifname = os.environ.get("LG_MESH_VLAN_IFACE", "vlan200").strip()
            if new_ifname and new_ifname != ifname:
                logger.info(
                    "Mesh VLAN proxy override: ifname %s -> %s (TFTP_SERVER_IP set)",
                    ifname,
                    new_ifname,
                )
                ifname = new_ifname
        return original_get_command.__func__(cls, res, host, port, ifname)

    pm_cls.get_command = classmethod(patched_get_command)
    pm_cls._libremesh_mesh_vlan_patched = True


def align_ssh_networkservice_with_mesh_vlan(target) -> None:
    """Ensure outbound SSH uses the mesh VLAN interface.

    Installs an idempotent monkey-patch on :class:`ProxyManager.get_command`.
    A direct mutation of ``ssh.networkservice.address`` does not work because
    labgrid resynchronises ``RemoteNetworkService`` from the coordinator the
    moment the SSHDriver is activated.  See :func:`_install_mesh_vlan_proxy_patch`.
    """
    if not os.environ.get("TFTP_SERVER_IP", "").strip():
        return
    _install_mesh_vlan_proxy_patch()


def is_qemu_target(target) -> bool:
    """Detect if the current target is a QEMU VM (not a physical DUT)."""
    try:
        ssh = target.get_driver("SSHDriver", activate=False)
        return ssh.networkservice.address == "127.0.0.1"
    except Exception:
        return False


def find_lan_interface(shell, max_wait: int = 60) -> str | None:
    """Wait for an UP LAN interface suitable for IP assignment.

    Waits up to *max_wait* seconds for br-lan (created by LibreMesh after
    wifi/batman init, can take 50+ seconds on slow devices like LibreRouter).
    Falls back to eth0 or the default-route interface if they are UP.
    """
    deadline = time.time() + max_wait
    while time.time() < deadline:
        _, _, rc = shell.run("ip -o link show br-lan 2>/dev/null | grep -q 'state UP'")
        if rc == 0:
            return "br-lan"

        for iface in ("eth0",):
            _, _, rc = shell.run(
                f"ip -o link show {iface} 2>/dev/null | grep -q 'state UP'"
            )
            if rc == 0:
                return iface

        stdout, _, rc = shell.run(
            "ip route show default 2>/dev/null | awk '{print $5}' | head -1"
        )
        if rc == 0 and stdout and stdout[0].strip():
            iface = stdout[0].strip()
            _, _, rc2 = shell.run(f"ip -o link show {iface} 2>/dev/null | grep -q .")
            if rc2 == 0:
                return iface

        remaining = int(deadline - time.time())
        if remaining > 0:
            logger.info(
                "Waiting for LAN interface to come UP (%ds remaining)", remaining
            )
            time.sleep(5)
    return None


def _start_ip_watchdog(shell, fixed_ip: str, original_iface: str):
    """Keep the fixed IP reachable while OpenWrt/LibreMesh reconfigures the network.

    Init scripts may create br-lan (absorbing eth0), restart the network, or
    reconfigure br-lan multiple times - all of which can remove the fixed IP.
    This watchdog runs for 300 seconds, checking every 3 seconds that the IP
    is on the best routable interface (br-lan when UP, otherwise original_iface).

    When br-lan is UP the IP MUST be on br-lan (not on a bridge port like eth0,
    which is L2-only and unreachable from outside).  The watchdog migrates the
    IP if needed.
    """
    watchdog_script = (
        f"END=$(($(date +%s)+300)); "
        f'while [ "$(date +%s)" -lt "$END" ]; do '
        f'  if ip link show br-lan 2>/dev/null | grep -q "state UP"; then '
        f"    WANT=br-lan; "
        f"  else "
        f"    WANT={original_iface}; "
        f"  fi; "
        f'  ip addr show "$WANT" 2>/dev/null | grep -q "{fixed_ip}" || '
        f'    {{ ip addr add {fixed_ip}/16 dev "$WANT" 2>/dev/null; }}; '
        f"  sleep 3; "
        f"done"
    )
    shell.run(f"({watchdog_script}) &")
    logger.info(
        "Started IP watchdog (300s): will keep %s on br-lan (fallback %s)",
        fixed_ip,
        original_iface,
    )


def configure_fixed_ip(
    shell,
    target,
    place_name: str | None = None,
    fixed_ip: str | None = None,
    prefer_networkservice: bool = True,
) -> str | None:
    """Configure a fixed IP on the LAN interface via serial for stable SSH.

    Resolution order:
      1. ``fixed_ip`` argument, when provided.
      2. NetworkService address, when ``prefer_networkservice`` is True.
      3. Hash-based IP derived from *place_name* (or ``LG_PLACE``).

    Returns the fixed IP string if configured, ``None`` otherwise.
    """
    time.sleep(5)

    networkservice_ip = get_ssh_target_ip(target)

    if networkservice_ip and networkservice_ip in ("127.0.0.1", "::1", "localhost"):
        logger.info(
            "QEMU target detected (NetworkService address=%s); "
            "skipping fixed IP configuration (strategy handles port forwarding)",
            networkservice_ip,
        )
        return None

    if not fixed_ip:
        if prefer_networkservice and networkservice_ip:
            fixed_ip = networkservice_ip
        else:
            place_name = place_name or getenv("LG_PLACE", "")
            if not place_name or place_name == "+":
                logger.warning(
                    "Cannot determine SSH target IP (prefer_networkservice=%s, LG_PLACE=%s)",
                    prefer_networkservice,
                    place_name,
                )
                return None
            fixed_ip = generate_mesh_ssh_ip(place_name)
            logger.info(
                "Using hash-based fallback IP %s for place %s", fixed_ip, place_name
            )
    logger.info("SSH target IP resolved to %s", fixed_ip)

    max_attempts = 3
    retry_delay = 15
    for attempt in range(1, max_attempts + 1):
        iface = find_lan_interface(shell)
        if not iface:
            # Transient on slow devices: LibreMesh creates br-lan only after
            # wifi/batman init, which on a LibreRouter booted from ramdisk
            # exceeds find_lan_interface's own wait. Returning here defeated
            # the retry loop, so the first test to need SSH failed while every
            # later one succeeded against the same DUT.
            if attempt < max_attempts:
                logger.warning(
                    "No LAN interface up yet (attempt %d/%d); retrying in %ds",
                    attempt,
                    max_attempts,
                    retry_delay,
                )
                time.sleep(retry_delay)
                continue
            logger.error(
                "No suitable network interface found for fixed IP after %d attempts",
                max_attempts,
            )
            return None
        logger.info(
            "Using interface %s for fixed IP (attempt %d/%d)",
            iface,
            attempt,
            max_attempts,
        )

        _, _, rc = shell.run(f"ip addr show {iface} | grep -q '{fixed_ip}'")
        if rc == 0:
            logger.info("Fixed IP %s already configured on %s", fixed_ip, iface)
            return fixed_ip

        logger.info("Configuring fixed IP %s on %s", fixed_ip, iface)
        shell.run(f"ip addr add {fixed_ip}/16 dev {iface} 2>/dev/null || true")

        stdout, stderr, rc = shell.run(f"ip addr show {iface} | grep '{fixed_ip}'")
        if rc == 0:
            logger.info("Fixed IP %s configured successfully on %s", fixed_ip, iface)
            _start_ip_watchdog(shell, fixed_ip, iface)
            return fixed_ip

        if attempt < max_attempts:
            err_msg = (
                " ".join(stderr)
                if stderr
                else " ".join(stdout)
                if stdout
                else "exit %d" % rc
            )
            logger.warning(
                "Failed to verify IP on %s (attempt %d/%d): %s; retrying in %ds",
                iface,
                attempt,
                max_attempts,
                err_msg,
                retry_delay,
            )
            time.sleep(retry_delay)
        else:
            logger.error(
                "Failed to configure fixed IP %s on %s after %d attempts",
                fixed_ip,
                iface,
                max_attempts,
            )
            return None


def query_node_ip(target, timeout: int = 120, prefer_mesh: bool = False) -> str:
    """Query the booted node's IPv4 via serial console (ShellDriver).

    When ``prefer_mesh`` is True, waits for a real LibreMesh br-lan address in
    ``10.13.0.0/16`` while excluding the mesh SSH/control range
    ``10.13.200.0/24``.
    Falls back to the first IPv4 seen only if no real mesh address appears
    before timeout.
    """
    shell = target.get_driver("ShellDriver")
    deadline = time.time() + timeout

    time.sleep(5)

    ip_cmd = (
        "ip -4 -o addr show br-lan 2>/dev/null || "
        "ip -4 -o addr show eth0 2>/dev/null || "
        "ip -4 -o addr show dev $(ip route show default "
        "| awk '{print $5}' | head -1) 2>/dev/null"
    )

    fallback_ip = None

    while time.time() < deadline:
        try:
            output = shell.run_check(ip_cmd)
            if prefer_mesh:
                mesh_ip = select_mesh_ipv4(output)
                if mesh_ip:
                    return mesh_ip
                fallback_ip = fallback_ip or select_primary_ipv4(output)
            else:
                primary_ip = select_primary_ipv4(output)
                if primary_ip:
                    return primary_ip
        except Exception:
            pass
        time.sleep(5)

    if prefer_mesh and fallback_ip:
        logger.warning(
            "Could not determine a real mesh IP within %ss, falling back to %s",
            timeout,
            fallback_ip,
        )
        return fallback_ip

    logger.warning("Could not determine IP for node, returning empty")
    return ""


# ---------------------------------------------------------------------------
# labnet.yaml target resolution
# ---------------------------------------------------------------------------


def resolve_target_yaml(place_name: str, *, repo_root: Path | None = None) -> str:
    """Resolve the target YAML path for a labgrid place via labnet.yaml.

    The place name format is ``<lab>-<host>-<device_instance>``.  The function
    looks up the device instance in ``labnet.yaml`` using three strategies:

    1. Top-level ``devices`` section (legacy format with device metadata).
    2. Lab ``device_instances`` mapping (instance → base device).
    3. Lab ``devices`` list (device name matches target YAML directly).

    Returns an absolute path string to the target YAML, or raises
    ``FileNotFoundError`` if no match is found.
    """
    repo_root = repo_root or REPO_ROOT
    labnet_path = resolve_labnet_path(repo_root)

    parts = place_name.split("-", 2)
    if len(parts) < 3:
        raise ValueError(f"Cannot parse device instance from place: {place_name}")
    device_instance = parts[2]

    if not labnet_path.exists():
        raise FileNotFoundError(f"labnet.yaml not found at {labnet_path}")

    with open(labnet_path) as f:
        labnet = yaml.safe_load(f)

    top_devices = labnet.get("devices", {})

    if device_instance in top_devices:
        device_config = top_devices[device_instance]
        target_name = device_config.get("target_file", device_instance)
        target_file = repo_root / f"targets/{target_name}.yaml"
        if target_file.exists():
            return str(target_file)

    matched_base_device: str | None = None
    matched_lab: str | None = None
    for lab_name, lab_config in labnet.get("labs", {}).items():
        device_instances = lab_config.get("device_instances", {})
        for base_device, instances in device_instances.items():
            if device_instance in instances:
                matched_base_device = base_device
                matched_lab = lab_name
                if base_device in top_devices:
                    device_config = top_devices[base_device]
                    target_name = device_config.get("target_file", base_device)
                    target_file = repo_root / f"targets/{target_name}.yaml"
                    if target_file.exists():
                        return str(target_file)
                fallback = repo_root / f"targets/{base_device}.yaml"
                if fallback.exists():
                    return str(fallback)

    for lab_name, lab_config in labnet.get("labs", {}).items():
        lab_devices = lab_config.get("devices", [])
        if device_instance in lab_devices:
            target_file = repo_root / f"targets/{device_instance}.yaml"
            if target_file.exists():
                return str(target_file)
            matched_lab = lab_name

    targets_dir = repo_root / "targets"
    available = (
        sorted(p.name for p in targets_dir.glob("*.yaml"))
        if targets_dir.is_dir()
        else []
    )
    raise FileNotFoundError(
        f"No target YAML found for place {place_name} "
        f"(instance: {device_instance}, "
        f"matched_lab={matched_lab}, base_device={matched_base_device}). "
        f"repo_root={repo_root}, labnet={labnet_path}, "
        f"available targets: {available}"
    )


# ---------------------------------------------------------------------------
# batman-adv auto-fix
# ---------------------------------------------------------------------------


def enable_batman_bridge_loop_avoidance(shell) -> bool:
    """Enable batman-adv bridge loop avoidance when supported.

    Our physical mesh tests bridge ``bat0`` into ``br-lan`` while also keeping
    direct host access on the same Ethernet segment. On some devices (notably
    the BananaPi R4) this can trigger bridge loops unless batman-adv's bridge
    loop avoidance is enabled.
    """
    commands = [
        "batctl meshif bat0 bridge_loop_avoidance 1",
        "batctl meshif bat0 bl 1",
        "batctl bridge_loop_avoidance 1",
        "batctl bl 1",
    ]

    for command in commands:
        try:
            stdout, stderr, rc = shell.run(command)
        except Exception:
            continue
        if rc == 0:
            logger.info("Enabled batman-adv bridge loop avoidance using: %s", command)
            return True
        logger.debug(
            "Could not enable bridge loop avoidance with %s (rc=%s, stdout=%s, stderr=%s)",
            command,
            rc,
            stdout,
            stderr,
        )

    logger.info("batman-adv bridge loop avoidance not available on this device")
    return False


def ensure_batman_mesh(target, place_name: str | None = None) -> bool:
    """Ensure batman-adv has at least one active mesh interface.

    If batman has no active interfaces, detect ports with carrier and
    configure batman on them.  This handles devices like BananaPi R4 where
    LibreMesh misconfigures mesh on SFP ports instead of DSA ports.

    When *place_name* is provided the fixed SSH IP is re-applied after a
    network restart so the node stays reachable.

    Returns ``True`` if batman is active (either already or after fix).
    """
    shell = target.get_driver("ShellDriver")

    try:
        output = shell.run_check("batctl if 2>/dev/null || echo 'batctl_not_found'")
        batctl_output = "\n".join(output)
    except Exception as e:
        logger.warning("Failed to run batctl if: %s", e)
        return False

    if "batctl_not_found" in batctl_output:
        logger.info("batctl not available on this device, skipping batman fix")
        return False

    active_lines = [
        line
        for line in batctl_output.splitlines()
        if line.strip() and "active" in line.lower()
    ]
    if active_lines:
        logger.info("Batman-adv already has active interfaces: %s", active_lines)
        enable_batman_bridge_loop_avoidance(shell)
        return True

    logger.warning("Batman-adv has no active interfaces, attempting auto-fix")

    try:
        output = shell.run_check("ls /sys/class/net/ | grep -E '^(lan|eth)[0-9]+$'")
        all_ifaces = [iface.strip() for iface in output if iface.strip()]
        logger.info("All matching interfaces: %s", all_ifaces)
    except Exception as e:
        logger.warning("Failed to list interfaces: %s", e)
        all_ifaces = []

    dsa_ports = [i for i in all_ifaces if i.startswith("lan") or i.startswith("wan")]
    eth_ports = [i for i in all_ifaces if i.startswith("eth")]

    dsa_conduits: set[str] = set()
    for iface in eth_ports:
        try:
            out = shell.run_check(
                f"ls /sys/class/net/{iface}/dsa 2>/dev/null && echo 'is_dsa_conduit' "
                f"|| echo 'not_dsa_conduit'"
            )
            if "is_dsa_conduit" in "\n".join(out):
                dsa_conduits.add(iface)
                logger.info("Interface %s is a DSA conduit (master), excluding", iface)
        except Exception:
            pass

    candidates = dsa_ports + [i for i in eth_ports if i not in dsa_conduits]
    logger.info(
        "Candidate interfaces (DSA ports first, conduits excluded): %s", candidates
    )

    if not candidates:
        logger.warning("No candidate interfaces found")
        return False

    fixed = False
    for iface in candidates:
        try:
            carrier_out = shell.run_check(
                f"cat /sys/class/net/{iface}/carrier 2>/dev/null || echo 0"
            )
            carrier = carrier_out[0].strip() if carrier_out else "0"
        except Exception:
            carrier = "0"

        if carrier != "1":
            logger.info("Interface %s has no carrier, skipping", iface)
            continue

        logger.info("Configuring batman-adv on %s (has carrier)", iface)
        batadv_dev = f"lm_net_{iface}_batadv_dev"
        batadv_if = f"lm_net_{iface}_batadv_if"
        try:
            shell.run_check(f"uci set network.{batadv_dev}=device")
            shell.run_check(f"uci set network.{batadv_dev}.name={iface}_29")
            shell.run_check(f"uci set network.{batadv_dev}.type=8021ad")
            shell.run_check(f"uci set network.{batadv_dev}.ifname={iface}")
            shell.run_check(f"uci set network.{batadv_dev}.vid=29")
            shell.run_check(f"uci set network.{batadv_if}=interface")
            shell.run_check(f"uci set network.{batadv_if}.proto=batadv_hardif")
            shell.run_check(f"uci set network.{batadv_if}.master=bat0")
            shell.run_check(f"uci set network.{batadv_if}.mtu=1500")
            shell.run_check(f"uci set network.{batadv_if}.device={iface}_29")
            shell.run_check("uci commit network")
            logger.info("UCI config applied for %s, restarting network...", iface)
            shell.run_check("/etc/init.d/network restart")
            time.sleep(30)
            if place_name:
                ssh_ip = generate_mesh_ssh_ip(place_name)
                logger.info("Re-applying fixed IP %s after network restart", ssh_ip)
                configure_fixed_ip(
                    shell,
                    target,
                    place_name=place_name,
                    fixed_ip=ssh_ip,
                    prefer_networkservice=False,
                )
            fixed = True
            break
        except Exception as e:
            logger.warning("Failed to configure batman on %s: %s", iface, e)
            continue

    if fixed:
        try:
            output = shell.run_check("batctl if 2>/dev/null")
            batctl_output = "\n".join(output)
            active_lines = [
                line
                for line in batctl_output.splitlines()
                if line.strip() and "active" in line.lower()
            ]
            if active_lines:
                logger.info("Batman-adv now has active interfaces: %s", active_lines)
                enable_batman_bridge_loop_avoidance(shell)
                return True
            else:
                logger.warning("Batman still has no active interfaces after fix")
        except Exception as e:
            logger.warning("Failed to verify batman status: %s", e)

    logger.warning("Could not activate batman-adv mesh")
    return False
