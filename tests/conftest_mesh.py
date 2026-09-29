"""
Multi-node mesh fixtures for LibreMesh testing.

Supports two modes:

1. Physical (default): LG_MESH_PLACES, LG_IMAGE/LG_IMAGE_MAP.
   Boots N DUTs in parallel via labgrid (one subprocess per node).

2. Virtual: LG_VIRTUAL_MESH=1, VIRTUAL_MESH_IMAGE, VIRTUAL_MESH_NODES.
   Launches N QEMU VMs with vwifi, no labgrid.

Requires LG_MESH_PLACES (comma-separated place names) and either:
  - LG_IMAGE: single image path used for all nodes (backward compatible).
  - LG_IMAGE_MAP: per-place image paths, format "place1=/path/img1,place2=/path/img2".
    Falls back to LG_IMAGE for any place not listed in the map.

Optional: LG_MESH_KEEP_POWERED=1 to leave nodes powered on after tests (for SSH/serial
debugging). Handled by mesh_boot_node.py subprocess; the labgrid place is still released.

Usage (physical, mixed device types):
    export LG_MESH_PLACES="labgrid-fcefyn-openwrt_one,labgrid-fcefyn-bananapi_bpi-r4"
    export LG_IMAGE_MAP="labgrid-fcefyn-openwrt_one=/srv/tftp/firmwares/openwrt_one/libremesh/lime-24.10.5-mediatek-filogic-openwrt_one-initramfs.itb,labgrid-fcefyn-bananapi_bpi-r4=/srv/tftp/firmwares/bananapi_bpi-r4/libremesh/lime-24.10.5-mediatek-filogic-bananapi_bpi-r4-initramfs-recovery.itb"
    uv run pytest tests/test_mesh.py -v --log-cli-level=INFO

Usage (single image for all nodes):
    export LG_MESH_PLACES="labgrid-fcefyn-belkin_rt3200_2,labgrid-fcefyn-belkin_rt3200_3"
    export LG_IMAGE="/srv/tftp/firmwares/belkin_rt3200/libremesh/lime-24.10.5-mediatek-mt7622-linksys_e8450-initramfs-kernel.bin"
    uv run pytest tests/test_mesh.py -v --log-cli-level=INFO
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from json import JSONDecodeError
from pathlib import Path
from typing import Optional

import pytest
from conftest_vlan import _resolve_proxy_host
from lime_helpers import REPO_ROOT, generate_mesh_ssh_ip, resolve_target_yaml

# Allow importing scripts from repo root
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger(__name__)

BOOT_SCRIPT = Path(__file__).parent / "mesh_boot_node.py"

# One U-Boot capture attempt costs: power cycle + interrupt-spam tail +
# serial drain + UBootDriver login_timeout. The power cycle dominates and
# depends on the power backend: Arduino relay DUTs cycle in ~9s, while PoE
# ports go through PDUDaemon -> SSH -> TP-Link switch and take 88-102s
# (measured on openwrt_one). UBootTFTPStrategy retries the capture up to
# 1 + LG_MESH_UBOOT_RETRIES times, so this budget must cover the slowest
# backend times the full retry count. It previously assumed a single retry
# at ~130s, which killed openwrt_one 20s after its third attempt had already
# captured U-Boot and completed TFTP.
UBOOT_ATTEMPT_WORST_CASE = 145
UBOOT_MAX_ATTEMPTS = 3
# TFTP download + kernel handoff + LibreMesh init + fixed-IP assignment.
POST_UBOOT_BOOT_BUDGET = 150

BOOT_TIMEOUT_BASE = UBOOT_ATTEMPT_WORST_CASE * UBOOT_MAX_ATTEMPTS + POST_UBOOT_BOOT_BUDGET
BOOT_TIMEOUT_PER_NODE = 30
NETWORK_SETTLE_TIMEOUT = 60
SUBPROCESS_SHUTDOWN_TIMEOUT = 30
SUBPROCESS_KILL_TIMEOUT = 10
BOOT_PROGRESS_LOG_INTERVAL = 30
BOOT_STATUS_POLL_INTERVAL = 2
NETWORK_SETTLE_POLL_INTERVAL = 5
SSH_COMMAND_TIMEOUT = 120
VWIFI_SETUP_TIMEOUT = 240
VWIFI_SETUP_RETRIES = 2
BOOT_LOG_TAIL_CHARS = 3000

SSH_TRANSIENT_EXIT_CODE = 255
SSH_TRANSIENT_RETRIES = 3
SSH_TRANSIENT_RETRY_DELAY = 3


def _is_transient_ssh_failure(returncode: int) -> bool:
    """Exit code 255 signals an SSH transport error (connection refused, reset,
    timeout at the TCP/SSH layer), not a failure of the remote command itself.
    These are inherently transient on mesh networks where routing and bridge
    state can fluctuate briefly."""
    return returncode == SSH_TRANSIENT_EXIT_CODE


class SSHProxy:
    """Lightweight SSH client using subprocess, compatible with labgrid SSHDriver API.

    Two modes:
    - Physical (vlan_iface set): Connects via ``labgrid-bound-connect`` through a VLAN.
      When ``LG_PROXY`` is set the bound-connect runs on the lab host via SSH
      (the host owns the VLAN interface and ``sudo NOPASSWD`` for the binary);
      when not set it runs locally (lab host or CI runner).
    - Virtual (vlan_iface None): Direct SSH to host:port (e.g. 127.0.0.1:2222).

    Transparently retries on SSH transport errors (exit code 255) which are
    transient on mesh networks due to routing convergence and bridge state
    fluctuations.
    """

    def __init__(
        self,
        host: str,
        vlan_iface: Optional[str] = "vlan200",
        username: str = "root",
        port: int = 22,
    ):
        self._host = host
        self._vlan_iface = vlan_iface
        self._username = username
        self._port = port

    def _build_ssh_cmd(self, command: str) -> list[str]:
        base = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ConnectTimeout=30",
        ]
        if self._vlan_iface:
            bound_connect_cmd = (
                f"sudo /usr/local/sbin/labgrid-bound-connect "
                f"{self._vlan_iface} {self._host} {self._port}"
            )
            # When LG_PROXY is set the mesh VLAN interface lives on the lab
            # host, not on the local machine. Run labgrid-bound-connect there
            # via SSH (the proxy host owns sudo NOPASSWD for the binary).
            proxy_host = _resolve_proxy_host()
            if proxy_host:
                proxy_cmd = f"ssh -o BatchMode=yes {proxy_host} {bound_connect_cmd}"
            else:
                proxy_cmd = bound_connect_cmd
            base.extend(
                ["-o", f"ProxyCommand={proxy_cmd}", f"{self._username}@{self._host}"]
            )
        else:
            base.extend(["-p", str(self._port), f"{self._username}@{self._host}"])
        base.append(command)
        return base

    def run_check(self, command: str, timeout: int = SSH_COMMAND_TIMEOUT) -> list[str]:
        """Run a command and return stdout lines. Raises on non-zero exit.

        Retries up to SSH_TRANSIENT_RETRIES times on SSH transport errors
        (exit code 255) before propagating the failure.
        """
        last_result = None
        for attempt in range(1, SSH_TRANSIENT_RETRIES + 1):
            result = subprocess.run(
                self._build_ssh_cmd(command),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                if attempt > 1:
                    logger.info(
                        "SSH to %s succeeded on retry %d/%d",
                        self._host,
                        attempt,
                        SSH_TRANSIENT_RETRIES,
                    )
                return [line for line in result.stdout.splitlines() if line]

            last_result = result
            if not _is_transient_ssh_failure(result.returncode):
                break
            if attempt < SSH_TRANSIENT_RETRIES:
                logger.warning(
                    "SSH to %s failed with transient error (rc=%d, attempt %d/%d), "
                    "retrying in %ds",
                    self._host,
                    result.returncode,
                    attempt,
                    SSH_TRANSIENT_RETRIES,
                    SSH_TRANSIENT_RETRY_DELAY,
                )
                time.sleep(SSH_TRANSIENT_RETRY_DELAY)

        raise subprocess.CalledProcessError(
            last_result.returncode,
            command,
            output=last_result.stdout,
            stderr=last_result.stderr,
        )

    def run(
        self, command: str, timeout: int = SSH_COMMAND_TIMEOUT
    ) -> tuple[list[str], list[str], int]:
        """Run a command and return (stdout_lines, stderr_lines, exit_code).

        Retries up to SSH_TRANSIENT_RETRIES times on SSH transport errors
        (exit code 255) before returning the failure.
        """
        try:
            last_result = None
            for attempt in range(1, SSH_TRANSIENT_RETRIES + 1):
                result = subprocess.run(
                    self._build_ssh_cmd(command),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if result.returncode == 0 or not _is_transient_ssh_failure(
                    result.returncode
                ):
                    if attempt > 1 and result.returncode == 0:
                        logger.info(
                            "SSH to %s succeeded on retry %d/%d",
                            self._host,
                            attempt,
                            SSH_TRANSIENT_RETRIES,
                        )
                    stdout = [line for line in result.stdout.splitlines() if line]
                    stderr = [line for line in result.stderr.splitlines() if line]
                    return stdout, stderr, result.returncode

                last_result = result
                if attempt < SSH_TRANSIENT_RETRIES:
                    logger.warning(
                        "SSH to %s failed with transient error (rc=%d, attempt %d/%d), "
                        "retrying in %ds",
                        self._host,
                        result.returncode,
                        attempt,
                        SSH_TRANSIENT_RETRIES,
                        SSH_TRANSIENT_RETRY_DELAY,
                    )
                    time.sleep(SSH_TRANSIENT_RETRY_DELAY)

            stdout = [line for line in last_result.stdout.splitlines() if line]
            stderr = [line for line in last_result.stderr.splitlines() if line]
            return stdout, stderr, last_result.returncode
        except subprocess.TimeoutExpired:
            return [], ["SSH command timed out"], 1


@dataclass
class MeshNode:
    """Represents a booted mesh DUT with SSH access."""

    place: str
    ssh: SSHProxy
    mesh_ip: str = ""
    _process: Optional[subprocess.Popen] = field(default=None, repr=False)
    _stop_file: str = field(default="", repr=False)

    @property
    def ip(self) -> str:
        """Backward-compatible alias for older tests that still read ``.ip``."""
        return self.mesh_ip


def _build_mesh_ssh_ip_map(places: list[str]) -> dict[str, str]:
    """Return a unique mesh SSH/control IP for each place, failing on collisions."""
    mesh_ssh_ip_map = {}
    seen = {}
    for place in places:
        mesh_ssh_ip = generate_mesh_ssh_ip(place)
        if mesh_ssh_ip in seen:
            raise ValueError(
                f"Duplicate mesh SSH IP {mesh_ssh_ip} for {seen[mesh_ssh_ip]} and {place}"
            )
        mesh_ssh_ip_map[place] = mesh_ssh_ip
        seen[mesh_ssh_ip] = place
    return mesh_ssh_ip_map


def _get_coordinator_address() -> str:
    return os.environ.get("LG_COORDINATOR", "localhost:20408")


def _get_vlan_iface() -> str:
    return os.environ.get("LG_MESH_VLAN_IFACE", "vlan200")


def _get_mesh_tftp_ip() -> str:
    """TFTP server IP on the mesh VLAN (host address on vlan200)."""
    return os.environ.get("LG_MESH_TFTP_IP", "192.168.200.1")


def _compute_boot_timeout(node_count: int) -> int:
    """Scale the boot timeout with node count.

    The U-Boot gate serializes power-cycle + TFTP capture across all nodes.
    The base timeout covers 3 nodes running the full U-Boot retry policy on
    the slowest power backend (see BOOT_TIMEOUT_BASE); each extra node adds
    BOOT_TIMEOUT_PER_NODE seconds.
    """
    extra_nodes = max(0, node_count - 3)
    return BOOT_TIMEOUT_BASE + extra_nodes * BOOT_TIMEOUT_PER_NODE


def _compute_network_settle_timeout(node_count: int) -> int:
    """Return a convergence window that scales mildly with topology size.

    The base timeout must be generous enough to cover batman-adv/babeld
    convergence *plus* possible late network restarts (which can temporarily
    remove the fixed SSH IP).  Each additional node adds 15s because
    batman-adv OGM propagation and babeld route announcement are not instant.
    """
    extra_nodes = max(0, node_count - 3)
    return NETWORK_SETTLE_TIMEOUT + extra_nodes * 20


def _resolve_image_map() -> dict[str, str]:
    """Parse LG_IMAGE_MAP into {place: image_path}.

    Format: "place1=/path/img1,place2=/path/img2"
    Falls back to empty dict if not set; callers fall back to LG_IMAGE.
    """
    raw = os.environ.get("LG_IMAGE_MAP", "").strip()
    if not raw:
        return {}
    result = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if "=" not in entry:
            continue
        place, _, path = entry.partition("=")
        result[place.strip()] = path.strip()
    return result


def _get_image_for_place(
    place: str, image_map: dict[str, str], default_image: str
) -> str:
    """Return the image path for a given place, with fallback to default."""
    return image_map.get(place, default_image)


def _launch_boot_subprocess(
    place: str, image: str, target_yaml: str, coordinator: str, tmpdir: str
) -> tuple[subprocess.Popen, str, str, str]:
    """Launch mesh_boot_node.py as a subprocess for one place."""
    status_file = os.path.join(tmpdir, f"status_{place}.json")
    stop_file = os.path.join(tmpdir, f"stop_{place}")
    log_file = os.path.join(tmpdir, f"boot_{place}.log")

    cmd = [
        "uv",
        "run",
        "python",
        str(BOOT_SCRIPT),
        "--place",
        place,
        "--image",
        image,
        "--target-yaml",
        target_yaml,
        "--coordinator",
        coordinator,
        "--status-file",
        status_file,
        "--stop-file",
        stop_file,
    ]

    logger.info("Launching boot subprocess for %s", place)
    log_fh = open(log_file, "w")
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(REPO_ROOT),
    )
    return proc, status_file, stop_file, log_file


def _read_status_file(status_file: str) -> Optional[dict]:
    """Return parsed child status when available and complete."""
    if not os.path.exists(status_file):
        return None
    try:
        with open(status_file) as f:
            return json.load(f)
    except (OSError, JSONDecodeError):
        return None


def _tail_boot_log(log_file: str, max_chars: int = 160) -> str:
    """Return the last non-empty line from a boot log for progress messages."""
    if not log_file or not os.path.exists(log_file):
        return ""
    try:
        with open(log_file) as f:
            lines = [line.strip() for line in f if line.strip()]
    except OSError:
        return ""
    if not lines:
        return ""
    tail = lines[-1]
    if len(tail) > max_chars:
        return tail[: max_chars - 3] + "..."
    return tail


def _dump_boot_log(place: str, log_file: str):
    """Read and log the boot subprocess output for debugging."""
    if not log_file or not os.path.exists(log_file):
        return
    try:
        with open(log_file) as f:
            content = f.read()
        if content.strip():
            logger.info(
                "=== Boot log for %s ===\n%s\n=== End boot log ===",
                place,
                content[-BOOT_LOG_TAIL_CHARS:],
            )
    except OSError:
        logger.debug("Could not read boot log for %s", place)


def _shutdown_subprocess(proc: subprocess.Popen, stop_file: str, place: str):
    """Signal a boot subprocess to shut down and wait for it."""
    try:
        Path(stop_file).touch()
    except OSError:
        pass

    try:
        proc.wait(timeout=SUBPROCESS_SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        logger.warning("Boot subprocess for %s didn't exit, sending SIGTERM", place)
        proc.terminate()
        try:
            proc.wait(timeout=SUBPROCESS_KILL_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.warning("Killing boot subprocess for %s", place)
            proc.kill()


def _shutdown_subprocesses(
    procs: dict[str, subprocess.Popen], stop_files: dict[str, str]
):
    """Best-effort shutdown for every launched mesh boot subprocess."""
    for place, proc in procs.items():
        if proc.poll() is None:
            _shutdown_subprocess(proc, stop_files.get(place, ""), place)


def _configure_vwifi_node(
    ssh: SSHProxy,
    vwifi_server_ip: str,
    node_index: int,
    timeout: int = VWIFI_SETUP_TIMEOUT,
):
    """Configure vwifi-client and restart wireless on a virtual node.

    Networking: LibreMesh absorbs eth0/eth1 into br-lan, breaking SLIRP. We use
    a dedicated eth2 (10.99.0.0/24 SLIRP) that LibreMesh doesn't touch, and point
    vwifi-client to the SLIRP gateway (10.99.0.2) where vwifi-server listens.

    Wireless band override (root cause): LibreMesh defaults to 5GHz channel 48.
    hostapd on mac80211_hwsim fails with "Could not determine operating frequency"
    in 5GHz—the virtual driver does not provide the frequency info hostapd expects.
    As a result, the phy never gets a channel set, wlan0-mesh stays NO-CARRIER,
    no beacons are transmitted, and mesh nodes never discover each other.
    We override to 2.4GHz channel 1 after lime-config so hostapd succeeds and
    the mesh forms.
    """
    mac_hex = f"{node_index:02x}"
    eth2_ip = f"10.99.0.{10 + node_index}"
    setup_script = (
        "set -eu; "
        f"ip link set eth2 nomaster 2>/dev/null || true; "
        f"ip addr flush dev eth2 2>/dev/null || true; "
        f"ip addr add {eth2_ip}/24 dev eth2; "
        f"ip link set eth2 up; "
        f"service vwifi-client stop 2>/dev/null || true; "
        f"uci set vwifi.config.server_ip='{vwifi_server_ip}'; "
        f"uci set vwifi.config.mac_prefix='02:00:00:00:00:{mac_hex}'; "
        f"uci set vwifi.config.enabled='1'; "
        f"uci commit vwifi; "
        f"service vwifi-client start; "
        f"sleep 5; "
        f"lime-config; "
        # Override to 2.4GHz ch1: hostapd fails "Could not determine operating
        # frequency" on 5GHz with mac80211_hwsim; phy stays unchanneled, mesh
        # NO-CARRIER, no beacons. 2.4GHz works. See docs root cause section.
        f"uci set wireless.radio0.channel='1'; "
        f"uci set wireless.radio0.band='2g'; "
        f"uci set wireless.radio0.htmode='HT20'; "
        f"uci commit wireless; "
        f"wifi down; "
        f"sleep 1; "
        f"wifi up"
    )
    stdout, stderr, rc = ssh.run(setup_script, timeout=timeout)
    return rc == 0


def _mesh_nodes_virtual():
    """Launch virtual mesh (QEMU + vwifi) and yield MeshNode list.

    Uses LG_VIRTUAL_MESH=1, VIRTUAL_MESH_IMAGE, VIRTUAL_MESH_NODES.

    If vwifi-server is running, configures each VM's vwifi-client to connect
    to it, then runs lime-config + wifi up so nodes form a mesh over simulated
    WiFi (802.11 via mac80211_hwsim + vwifi).
    """
    from scripts.virtual_mesh_launcher import launch_virtual_mesh

    image = os.environ.get("VIRTUAL_MESH_IMAGE", "").strip()
    if not image:
        pytest.skip("VIRTUAL_MESH_IMAGE required when LG_VIRTUAL_MESH=1")

    n_nodes = int(os.environ.get("VIRTUAL_MESH_NODES", "3"))
    logger.info("Virtual mesh: launching %d nodes with image %s", n_nodes, image)

    nodes_raw, cleanup = launch_virtual_mesh(n_nodes=n_nodes, image_path=image)

    mesh_nodes_list = []
    for n in nodes_raw:
        ssh = SSHProxy(host=n.host, vlan_iface=None, port=n.port)
        node = MeshNode(place=n.place_id, ssh=ssh, mesh_ip="")
        mesh_nodes_list.append(node)

    skip_vwifi = os.environ.get("VIRTUAL_MESH_SKIP_VWIFI", "").strip() == "1"
    if not skip_vwifi:
        # In user-mode networking, we use a dedicated eth2 interface on a separate
        # SLIRP network (10.99.0.0/24) for vwifi-client connectivity. The gateway
        # is 10.99.0.2. vwifi-server listens on 0.0.0.0:8214, reachable from guests
        # via this gateway IP.
        vwifi_server_ip = os.environ.get("VIRTUAL_MESH_VWIFI_HOST", "10.99.0.2")
        logger.info(
            "Configuring vwifi-client on %d nodes (server=%s)",
            len(mesh_nodes_list),
            vwifi_server_ip,
        )
        failed_nodes = []
        for i, node in enumerate(mesh_nodes_list, start=1):
            ok = False
            for retry in range(1, VWIFI_SETUP_RETRIES + 1):
                ok = _configure_vwifi_node(node.ssh, vwifi_server_ip, i)
                if ok:
                    logger.info("Node %s: vwifi-client configured", node.place)
                    break
                logger.warning(
                    "Node %s: vwifi-client setup failed (attempt %d/%d, timeout=%ds)",
                    node.place,
                    retry,
                    VWIFI_SETUP_RETRIES,
                    VWIFI_SETUP_TIMEOUT,
                )
            if not ok:
                failed_nodes.append(node.place)
        if failed_nodes:
            cleanup()
            pytest.fail(
                f"vwifi-client setup failed on {failed_nodes} after "
                f"{VWIFI_SETUP_RETRIES} attempts each "
                f"(timeout={VWIFI_SETUP_TIMEOUT}s). "
                f"Mesh cannot form without all nodes connected."
            )

        convergence_wait = int(os.environ.get("VIRTUAL_MESH_CONVERGENCE_WAIT", "60"))
        logger.info(
            "Waiting %ds for mesh convergence (batman-adv/babeld over vwifi)...",
            convergence_wait,
        )
        time.sleep(convergence_wait)
    else:
        logger.info("Skipping vwifi-client configuration (VIRTUAL_MESH_SKIP_VWIFI=1)")

    settle_timeout = _compute_network_settle_timeout(len(mesh_nodes_list))
    logger.info(
        "All %d virtual nodes booted, waiting %ds for network to settle",
        len(mesh_nodes_list),
        settle_timeout,
    )
    _wait_for_network(mesh_nodes_list, timeout=settle_timeout)

    yield mesh_nodes_list

    logger.info("Tearing down %d virtual mesh nodes", len(mesh_nodes_list))
    cleanup()


@pytest.fixture(scope="session")
def mesh_nodes(request, mesh_vlan_multi):
    """Boot mesh DUTs and yield MeshNode list. Physical or virtual based on LG_VIRTUAL_MESH.

    Depends on mesh_vlan_multi to ensure all DUT ports are switched to
    VLAN 200 before booting nodes (and restored on session teardown).

    Each MeshNode has:
      - .ssh (SSHProxy): run_check(cmd) -> list[str], run(cmd) -> (stdout, stderr, code)
      - .place (str): labgrid place name or virtual-mesh-N
      - .mesh_ip (str): node's real br-lan IPv4 address (empty for virtual until queried)

    Physical: LG_MESH_PLACES, LG_IMAGE/LG_IMAGE_MAP.
    Virtual: LG_VIRTUAL_MESH=1, VIRTUAL_MESH_IMAGE, VIRTUAL_MESH_NODES.
    """
    if os.environ.get("LG_VIRTUAL_MESH") == "1":
        yield from _mesh_nodes_virtual()
        return

    places_str = os.environ.get("LG_MESH_PLACES", "")
    if not places_str:
        pytest.skip("LG_MESH_PLACES not set")

    places = [p.strip() for p in places_str.split(",") if p.strip()]
    if not places:
        pytest.skip("LG_MESH_PLACES must contain at least 1 place")

    try:
        mesh_ssh_ip_map = _build_mesh_ssh_ip_map(places)
    except ValueError as e:
        pytest.fail(str(e))

    image_map = _resolve_image_map()
    default_image = os.environ.get("LG_IMAGE", "")
    if not image_map and not default_image:
        pytest.skip("LG_IMAGE or LG_IMAGE_MAP must be set")

    coordinator = _get_coordinator_address()
    vlan_iface = _get_vlan_iface()

    mesh_tftp_ip = _get_mesh_tftp_ip()
    os.environ["TFTP_SERVER_IP"] = mesh_tftp_ip
    logger.info(
        "Mesh test: booting %d nodes in parallel (TFTP_SERVER_IP=%s): %s",
        len(places),
        mesh_tftp_ip,
        places,
    )
    logger.info("Reserved mesh SSH IPs: %s", mesh_ssh_ip_map)

    tmpdir = tempfile.mkdtemp(prefix="mesh_boot_")
    procs = {}
    status_files = {}
    stop_files = {}
    log_files = {}

    nodes = []
    failed = []
    seen_ssh_ips = {}

    try:
        for place in places:
            image_path = _get_image_for_place(place, image_map, default_image)
            if not image_path:
                pytest.fail(
                    f"No image configured for place {place} "
                    "(set LG_IMAGE or add it to LG_IMAGE_MAP)"
                )
            target_yaml = resolve_target_yaml(place)
            logger.info(
                "Node %s: target YAML %s, image %s", place, target_yaml, image_path
            )
            proc, status_file, stop_file, log_file = _launch_boot_subprocess(
                place,
                image_path,
                target_yaml,
                coordinator,
                tmpdir,
            )
            procs[place] = proc
            status_files[place] = status_file
            stop_files[place] = stop_file
            log_files[place] = log_file

        pending = set(places)
        boot_timeout = _compute_boot_timeout(len(places))
        logger.info("Boot timeout for %d nodes: %ds", len(places), boot_timeout)
        deadline = time.time() + boot_timeout
        next_progress_log = time.time() + BOOT_PROGRESS_LOG_INTERVAL

        while pending and time.time() < deadline:
            progress = False
            for place in list(pending):
                status = _read_status_file(status_files[place])
                if status is None and procs[place].poll() is not None:
                    status = _read_status_file(status_files[place])
                    if status is None:
                        status = {
                            "place": place,
                            "ok": False,
                            "error": "subprocess exited early",
                        }

                if status is None:
                    continue

                progress = True
                pending.remove(place)

                if status.get("ok"):
                    ssh_ip = status.get("ssh_ip") or status.get("ip", "")
                    mesh_ip = status.get("mesh_ip") or status.get("ip", "")
                    if not ssh_ip:
                        logger.error("Node %s booted but did not report ssh_ip", place)
                        failed.append(place)
                        continue
                    if ssh_ip in seen_ssh_ips:
                        logger.error(
                            "Duplicate ssh_ip %s reported by %s and %s",
                            ssh_ip,
                            seen_ssh_ips[ssh_ip],
                            place,
                        )
                        failed.append(place)
                        continue
                    seen_ssh_ips[ssh_ip] = place
                    ssh = SSHProxy(host=ssh_ip, vlan_iface=vlan_iface)
                    node = MeshNode(
                        place=place,
                        ssh=ssh,
                        mesh_ip=mesh_ip,
                        _process=procs[place],
                        _stop_file=stop_files[place],
                    )
                    nodes.append(node)
                    attempts_used = status.get("attempts_used", 1)
                    logger.info(
                        "Node %s booted successfully (ssh_ip=%s, mesh_ip=%s, attempts=%s)",
                        place,
                        ssh_ip,
                        mesh_ip,
                        attempts_used,
                    )
                else:
                    error = status.get("error", "unknown")
                    stage = status.get("failure_stage", "unknown")
                    error_type = status.get("error_type", "unknown")
                    attempts_used = status.get("attempts_used", "?")
                    summary = status.get("error_summary", error)
                    logger.error(
                        "Node %s failed to boot after %s attempts at stage %s (%s): %s",
                        place,
                        attempts_used,
                        stage,
                        error_type,
                        summary,
                    )
                    _dump_boot_log(place, log_files[place])
                    failed.append(place)

            if pending and time.time() >= next_progress_log:
                summaries = []
                for place in places:
                    if place not in pending:
                        continue
                    proc_state = "running" if procs[place].poll() is None else "exited"
                    tail = _tail_boot_log(log_files[place]) or "no recent log line"
                    summaries.append(f"{place} [{proc_state}] {tail}")
                logger.info(
                    "Still waiting for %d node boot statuses after %ds: %s",
                    len(pending),
                    boot_timeout - max(0, int(deadline - time.time())),
                    summaries,
                )
                next_progress_log = time.time() + BOOT_PROGRESS_LOG_INTERVAL

            if pending and not progress:
                time.sleep(BOOT_STATUS_POLL_INTERVAL)

        for place in list(pending):
            logger.error("Timeout waiting for %s to boot (>%ds)", place, boot_timeout)
            _dump_boot_log(place, log_files[place])
            failed.append(place)

        if failed:
            pytest.fail(
                f"Not all mesh nodes booted: {len(nodes)}/{len(places)} "
                f"(failed: {failed})",
                pytrace=False,
            )

        nodes.sort(key=lambda n: places.index(n.place))

        settle_timeout = _compute_network_settle_timeout(len(nodes))
        logger.info(
            "All %d nodes booted, waiting %ds for network to settle",
            len(nodes),
            settle_timeout,
        )
        _wait_for_network(nodes, timeout=settle_timeout)

        yield nodes
    finally:
        if procs:
            logger.info("Tearing down %d mesh boot subprocesses", len(procs))
            _shutdown_subprocesses(procs, stop_files)
        os.environ.pop("TFTP_SERVER_IP", None)
        if not failed:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            logger.info("Boot logs preserved in %s for debugging", tmpdir)


def _wait_for_network(nodes: list[MeshNode], timeout: int = NETWORK_SETTLE_TIMEOUT):
    """Wait until all nodes respond to SSH echo.

    Uses the ``run`` method (which already retries on SSH exit code 255)
    to avoid raising on transient SSH transport errors that are common
    right after boot while batman-adv and babeld are still converging.
    """
    deadline = time.time() + timeout
    pending = set(range(len(nodes)))
    next_status_log = time.time() + 30

    while pending and time.time() < deadline:
        for i in list(pending):
            try:
                stdout, _, rc = nodes[i].ssh.run("echo mesh-ready")
                if rc == 0 and "mesh-ready" in stdout:
                    logger.info("Node %s: SSH reachable", nodes[i].place)
                    pending.discard(i)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if pending:
            if time.time() >= next_status_log:
                remaining = max(0, int(deadline - time.time()))
                names = [nodes[i].place for i in pending]
                logger.info(
                    "Still waiting for %d node(s) to become SSH reachable (%ds remaining): %s",
                    len(pending),
                    remaining,
                    names,
                )
                next_status_log = time.time() + 30
            time.sleep(NETWORK_SETTLE_POLL_INTERVAL)

    if pending:
        names = [nodes[i].place for i in pending]
        pytest.fail(
            f"Nodes not reachable via SSH after {timeout}s: {names}",
            pytrace=False,
        )
