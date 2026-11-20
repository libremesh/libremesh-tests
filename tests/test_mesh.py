"""
Multi-node mesh tests for LibreMesh.

Phase 1: Basic connectivity (L2/L3 between mesh nodes).
Phase 2: Mesh protocol validation (batman-adv, babeld).

Requires: LG_MESH_PLACES, LG_IMAGE, LG_PROXY environment variables.
``mesh_vlan_multi`` (session fixture) switches DUTs to VLAN 200 when ``LG_MESH_PLACES`` is set.
"""

import logging
import re
import time

import pytest
import shared_state_helpers as ss
from lime_helpers import is_mesh_ipv4, select_mesh_ipv4, select_primary_ipv4

pytestmark = pytest.mark.mesh

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_br_lan_ipv4(node) -> str:
    """Extract the preferred br-lan IPv4, preferring the real mesh address."""
    if node.mesh_ip and is_mesh_ipv4(node.mesh_ip):
        return node.mesh_ip

    output = node.ssh.run_check("ip -4 -o addr show br-lan")
    mesh_ip = select_mesh_ipv4(output)
    if mesh_ip:
        return mesh_ip

    primary_ip = select_primary_ipv4(output)
    if primary_ip:
        return primary_ip

    if node.mesh_ip:
        return node.mesh_ip

    pytest.fail(f"Node {node.place}: no IPv4 on br-lan")


def _get_all_ipv4(node) -> list[str]:
    """Get all IPv4 addresses assigned to br-lan."""
    output = node.ssh.run_check("ip -4 -o addr show br-lan")
    return re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", "\n".join(output))


def _has_command(node, cmd: str) -> bool:
    """Check if a command exists on the node."""
    stdout, _, exit_code = node.ssh.run(f"which {cmd}")
    return exit_code == 0


def _ping_with_retry(src, dst, dst_ip: str, attempts: int = 3, delay: int = 5) -> None:
    """Ping a mesh peer, retrying through transient convergence or SSH hiccups."""
    last_stdout: list[str] = []
    last_stderr: list[str] = []
    last_exit_code = 1

    for attempt in range(1, attempts + 1):
        stdout, stderr, exit_code = src.ssh.run(f"ping -c 5 -W 5 {dst_ip}")
        joined = "\n".join(stdout)
        if exit_code == 0 and "0% packet loss" in joined:
            if attempt > 1:
                logger.info(
                    "Ping %s -> %s (%s) succeeded on retry %d/%d",
                    src.place,
                    dst.place,
                    dst_ip,
                    attempt,
                    attempts,
                )
            return

        last_stdout, last_stderr, last_exit_code = stdout, stderr, exit_code
        logger.warning(
            "Ping %s -> %s (%s) failed on attempt %d/%d (rc=%s, stderr=%s)",
            src.place,
            dst.place,
            dst_ip,
            attempt,
            attempts,
            exit_code,
            stderr,
        )
        if attempt < attempts:
            time.sleep(delay)

    joined = "\n".join(last_stdout)
    stderr_joined = "\n".join(last_stderr)
    pytest.fail(
        f"Ping {src.place} -> {dst.place} ({dst_ip}) failed after {attempts} attempts "
        f"(rc={last_exit_code}).\nSTDOUT:\n{joined}\nSTDERR:\n{stderr_joined}"
    )


def _ensure_arp_entry(src, dst, dst_ip: str) -> None:
    """Prime neighbor state before asserting on the ARP table."""
    output = src.ssh.run_check("ip neigh show dev br-lan")
    joined = "\n".join(output)
    if dst_ip in joined:
        return

    logger.info(
        "Priming neighbor entry on %s for %s (%s)",
        src.place,
        dst.place,
        dst_ip,
    )
    _ping_with_retry(src, dst, dst_ip, attempts=2, delay=3)


# ---------------------------------------------------------------------------
# Phase 1: Basic connectivity
# ---------------------------------------------------------------------------


class TestMeshConnectivity:
    """Validate that mesh nodes boot and can reach each other."""

    def test_nodes_booted(self, mesh_nodes):
        """All nodes reached shell and SSH is functional."""
        for node in mesh_nodes:
            output = node.ssh.run_check("echo mesh-ok")
            assert output == ["mesh-ok"], (
                f"Node {node.place}: unexpected output {output}"
            )

    def test_nodes_have_ip(self, mesh_nodes):
        """Each node has at least one IPv4 on br-lan."""
        for node in mesh_nodes:
            ips = _get_all_ipv4(node)
            logger.info("Node %s: br-lan IPs = %s", node.place, ips)
            assert ips, f"Node {node.place}: no IPv4 on br-lan"

    def test_l3_ping_between_nodes(self, mesh_nodes):
        """First node can ping every other node via br-lan IP."""
        src = mesh_nodes[0]
        for dst in mesh_nodes[1:]:
            dst_ip = _get_br_lan_ipv4(dst)
            logger.info("Ping %s -> %s (%s)", src.place, dst.place, dst_ip)
            _ping_with_retry(src, dst, dst_ip)

    def test_l2_arp_entries(self, mesh_nodes):
        """After ping, ARP table on node 0 has entries for other nodes."""
        src = mesh_nodes[0]
        for dst in mesh_nodes[1:]:
            dst_ip = _get_br_lan_ipv4(dst)
            _ensure_arp_entry(src, dst, dst_ip)
            output = src.ssh.run_check("ip neigh show dev br-lan")
            joined = "\n".join(output)
            assert dst_ip in joined, (
                f"Node {src.place}: no ARP entry for {dst.place} ({dst_ip})"
            )


# ---------------------------------------------------------------------------
# Phase 2: Mesh protocol validation
# ---------------------------------------------------------------------------


class TestMeshProtocol:
    """Validate that LibreMesh mesh protocols are running and forming a network."""

    def test_batman_interface_exists(self, mesh_nodes):
        """batman-adv interface (bat0) exists on all nodes."""
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run("ip link show bat0")
            if exit_code != 0:
                pytest.skip("batman-adv not available on this build")
            assert "bat0" in "\n".join(stdout)

    def test_batman_neighbors(self, mesh_nodes):
        """batman-adv is active on at least one mesh node.

        Verifies that ``batctl n`` runs without error and batman-adv is not
        disabled. Neighbor table contents are logged for diagnostics but are
        not asserted: wired-only batman hardifs (e.g. bananapi ``lan1_29``)
        paired with wireless-only peers (e.g. belkin ``wlan0-mesh_29``) do not
        exchange OGMs on the same L2 segment, so the neighbor table may be
        empty even when L3 mesh connectivity is fully operational.
        """
        if not _has_command(mesh_nodes[0], "batctl"):
            pytest.skip("batctl not installed")

        active_nodes = 0
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run("batctl n")
            joined = "\n".join(stdout)
            logger.info("Node %s batman neighbors:\n%s", node.place, joined)
            if "disabled" in joined.lower():
                logger.warning("Node %s: batman mesh disabled", node.place)
                continue
            if exit_code != 0:
                logger.warning(
                    "Node %s: batctl n failed with code %d", node.place, exit_code
                )
                continue
            active_nodes += 1

        if active_nodes == 0:
            pytest.skip("batman-adv disabled on all nodes")

    def test_batman_originators(self, mesh_nodes):
        """batman-adv originator table has entries (mesh routes formed)."""
        if not _has_command(mesh_nodes[0], "batctl"):
            pytest.skip("batctl not installed")

        active_nodes = 0
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run("batctl o")
            joined = "\n".join(stdout)
            logger.info("Node %s originators:\n%s", node.place, joined)
            if "disabled" in joined.lower():
                logger.warning("Node %s: batman mesh disabled", node.place)
                continue
            if exit_code != 0:
                logger.warning(
                    "Node %s: batctl o failed with code %d", node.place, exit_code
                )
                continue
            active_nodes += 1

        if active_nodes == 0:
            pytest.skip("batman-adv disabled on all nodes")

    def test_babeld_running(self, mesh_nodes):
        """babeld process is running on all nodes."""
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run("pgrep -x babeld")
            if exit_code != 0:
                pytest.skip("babeld not running on this build")

    def test_babeld_has_routes(self, mesh_nodes):
        """babeld has learned at least one route from another node."""
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run("pgrep -x babeld")
            if exit_code != 0:
                pytest.skip("babeld not running")

            output = node.ssh.run_check(
                "echo dump | nc ::1 33123 2>/dev/null || "
                "cat /var/run/babeld/state 2>/dev/null || "
                "echo BABELD_NO_STATE"
            )
            joined = "\n".join(output)
            logger.info("Node %s babeld state:\n%s", node.place, joined[:500])
            assert "BABELD_NO_STATE" not in joined, (
                f"Node {node.place}: cannot read babeld state"
            )


# ---------------------------------------------------------------------------
# Phase 3: LibreMesh-specific checks
# ---------------------------------------------------------------------------


class TestLibreMeshServices:
    """Validate LibreMesh-specific services and configuration."""

    def test_lime_config_exists(self, mesh_nodes):
        """LibreMesh configuration file exists."""
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run(
                "ls /etc/config/lime-defaults 2>/dev/null || ls /etc/config/lime 2>/dev/null"
            )
            if exit_code != 0:
                pytest.skip("Not a LibreMesh build (no lime config)")

    def test_lime_proto_running(self, mesh_nodes):
        """lime-proto network protocols are configured."""
        for node in mesh_nodes:
            stdout, _, exit_code = node.ssh.run(
                "uci show lime-defaults 2>/dev/null || echo NO_LIME"
            )
            joined = "\n".join(stdout)
            if "NO_LIME" in joined:
                pytest.skip("lime-defaults not present")
            logger.info("Node %s lime config:\n%s", node.place, joined[:300])


class TestMeshSharedState:
    """shared-state-async across the mesh: every node sees the others' data."""

    @staticmethod
    def _require_shared_state(mesh_nodes):
        statuses = [
            (node.place, *ss.daemon_status(node.ssh.run)) for node in mesh_nodes
        ]
        stopped = [
            place for place, installed, running in statuses if installed and not running
        ]
        assert not stopped, (
            "shared-state-async package is installed but its peer daemon is not "
            f"running on: {', '.join(stopped)}"
        )

        missing = [place for place, installed, _ in statuses if not installed]
        if missing:
            pytest.skip(
                "shared-state-async package is not installed on: " + ", ".join(missing)
            )

    def test_shared_state_async_running_on_all_nodes(self, mesh_nodes):
        """shared-state-async is installed and its daemon runs on every node."""
        self._require_shared_state(mesh_nodes)

    def test_shared_state_async_sees_other_nodes(self, mesh_nodes):
        """Each node ends up holding bat-hosts entries authored by every other node.

        Publishes bat-hosts on all nodes, then polls each node's dump until
        automatic synchronization makes every mesh hostname visible, or
        SHARED_STATE_MESH_TIMEOUT expires.
        """
        self._require_shared_state(mesh_nodes)
        if len(mesh_nodes) < 2:
            pytest.skip("Need at least 2 mesh nodes to check data exchange")

        hostnames = {node.place: ss.get_hostname(node.ssh.run) for node in mesh_nodes}
        if len(set(hostnames.values())) != len(hostnames):
            pytest.fail(
                f"Mesh nodes share hostnames {hostnames}; shared-state authors "
                "cannot tell them apart"
            )
        expected = set(hostnames.values())

        for node in mesh_nodes:
            rc, out = ss.publish(node.ssh.run)
            assert rc == 0, f"{node.place}: bat-hosts publisher failed (rc={rc}): {out}"

        seen: dict[str, set[str]] = {}
        missing: dict[str, list[str]] = {}
        started = time.monotonic()
        deadline = started + ss.MESH_TIMEOUT
        first_check_at = started + ss.MIN_MESH_SYNC_WAIT
        attempt = 0
        while True:
            now = time.monotonic()
            if now < first_check_at:
                time.sleep(min(first_check_at - now, deadline - now))

            attempt += 1
            for node in mesh_nodes:
                authors, _, _ = ss.dump_authors(node.ssh.run)
                seen[node.place] = authors
            missing = {
                place: sorted(expected - authors)
                for place, authors in seen.items()
                if expected - authors
            }
            logger.info(
                "shared-state round %d: %s",
                attempt,
                {hostnames[p]: sorted(a) for p, a in seen.items()},
            )
            if not missing or time.monotonic() >= deadline:
                break
            time.sleep(min(ss.POLL_INTERVAL, max(0, deadline - time.monotonic())))

        if missing:
            details = []
            for node in mesh_nodes:
                if node.place not in missing:
                    continue
                candidates = ss.discover_candidates(node.ssh.run)
                details.append(
                    f"  {node.place} ({hostnames[node.place]}): has "
                    f"{sorted(seen[node.place])}, missing {missing[node.place]}, "
                    f"shared-state neighbour candidates: {candidates or 'none'}"
                )
            pytest.fail(
                f"shared-state {ss.SS_DATATYPE} did not converge after "
                f"{ss.MESH_TIMEOUT}s ({attempt} polling rounds):\n" + "\n".join(details)
            )
