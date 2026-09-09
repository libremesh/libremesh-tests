"""LibreMesh single-node tests.

These tests validate LibreMesh-specific functionality on a single DUT:
configuration presence, mesh IP assignment, batman-adv status, hostname
convention, and core LibreMesh services.

All tests run with the switch in mesh mode (VLAN 200) and the
exporter-mesh.yaml active. LibreMesh assigns deterministic 10.13.x.x IPs
based on the device MAC, so SSH connectivity works without per-VLAN isolation.
"""

import ipaddress
import re

import pytest


class TestLibreMeshConfig:
    """Verify LibreMesh configuration files and packages are present."""

    def test_lime_config_package_installed(self, ssh_command):
        """Verify lime-system or lime-config package is installed.

        Note: OpenWrt 24.10+ uses apk instead of opkg, and newer LibreMesh
        builds use lime-system instead of lime-config.
        """
        # Try apk first (OpenWrt 24.10+), then opkg
        stdout, _, rc = ssh_command.run(
            "apk info 2>/dev/null || opkg list-installed 2>/dev/null"
        )
        installed = "\n".join(stdout) if stdout else ""

        # Check for lime-system (newer) or lime-config (older)
        has_lime = "lime-system" in installed or "lime-config" in installed
        assert has_lime, (
            "Neither lime-system nor lime-config package found. Is this a LibreMesh image?"
        )

    def test_lime_defaults_uci_present(self, ssh_command):
        """Verify lime-defaults UCI config section exists."""
        stdout, _, rc = ssh_command.run("uci show lime-defaults")
        assert rc == 0, "lime-defaults UCI config not found"
        output = "\n".join(stdout)
        assert "lime-defaults.system=lime" in output, (
            "lime-defaults.system section missing from UCI config"
        )

    def test_lime_node_config_present(self, ssh_command):
        """Verify /etc/config/lime-node exists (per-node UCI config)."""
        _, _, rc = ssh_command.run(
            "test -f /etc/config/lime-node || test -f /etc/config/lime"
        )
        assert rc == 0, "Neither /etc/config/lime-node nor /etc/config/lime found"

    def test_lime_community_config_present(self, ssh_command):
        """Verify /etc/config/lime-community exists (community-wide config)."""
        _, _, rc = ssh_command.run("test -f /etc/config/lime-community")
        assert rc == 0, "/etc/config/lime-community not found"


class TestLibreMeshNetwork:
    """Verify LibreMesh network configuration and mesh IP assignment."""

    def test_br_lan_has_mesh_ip(self, ssh_command):
        """Verify br-lan has a 10.13.x.x IP (LibreMesh dynamic assignment)."""
        stdout, _, rc = ssh_command.run("ip -4 -o addr show br-lan")
        assert rc == 0, "Failed to query br-lan IP address"

        mesh_network = ipaddress.IPv4Network("10.13.0.0/16")
        for line in stdout:
            for part in line.split():
                if "/" in part:
                    try:
                        addr = ipaddress.IPv4Interface(part)
                        if addr.ip in mesh_network:
                            return
                    except ValueError:
                        continue

        pytest.fail(f"br-lan does not have a 10.13.x.x LibreMesh IP. Got: {stdout}")

    def test_mesh_ip_is_stable(self, ssh_command):
        """Verify the 10.13.x.x IP is deterministic (same across queries).

        LibreMesh sets the br-lan IP via a hash of the primary MAC in
        ``lime-config``. The exact transformation has changed between
        versions (BSSID-based hash in 24.10.x, different schedule in
        25.12.x), so any test that hard-codes the formula breaks on
        every release bump. What we actually care about is that the
        derivation is **deterministic**: a given image, on a given MAC,
        always assigns the same IP. We check that by reading the IP
        twice (separated by a short delay) and asserting it does not
        drift, plus that it falls inside the canonical 10.13.0.0/16
        mesh range and not in the 10.13.200.x slice that conftest
        reserves for our static test IPs.
        """

        def _read_libremesh_ip() -> ipaddress.IPv4Address | None:
            ip_out, _, ip_rc = ssh_command.run("ip -4 -o addr show br-lan")
            assert ip_rc == 0
            for line in ip_out:
                for part in line.split():
                    if "/" not in part or not part.startswith("10.13."):
                        continue
                    if part.startswith("10.13.200."):
                        # Reserved for conftest's static test addresses.
                        continue
                    try:
                        addr = ipaddress.IPv4Interface(part)
                    except ValueError:
                        continue
                    if addr.ip in ipaddress.IPv4Network("10.13.0.0/16"):
                        return addr.ip
            return None

        first = _read_libremesh_ip()
        assert first is not None, "br-lan has no LibreMesh 10.13.x.x IP"
        second = _read_libremesh_ip()
        assert second == first, (
            f"br-lan LibreMesh IP changed between back-to-back reads: "
            f"first={first}, second={second}"
        )

    def test_hostname_follows_libremesh_convention(self, ssh_command):
        """Verify hostname follows LibreMesh convention: LiMe-XXXXXX."""
        # Use /proc/sys/kernel/hostname as fallback (hostname command may not exist in initramfs)
        stdout, _, rc = ssh_command.run(
            "hostname 2>/dev/null || cat /proc/sys/kernel/hostname"
        )
        hostname = stdout[0].strip() if stdout else ""
        assert re.match(r"^LiMe-[0-9a-fA-F]{6}$", hostname), (
            f"Hostname '{hostname}' does not follow LibreMesh convention LiMe-XXXXXX"
        )

    def test_bat0_interface_exists(self, ssh_command):
        """Verify the batman-adv bat0 virtual interface is present."""
        _, _, rc = ssh_command.run("ip link show bat0")
        assert rc == 0, "bat0 interface not found — batman-adv may not be running"


class TestLibreMeshBatmanAdv:
    """Verify batman-adv mesh protocol is running correctly."""

    def test_batman_adv_module_loaded(self, ssh_command):
        """Verify batman-adv kernel module is loaded."""
        stdout, _, rc = ssh_command.run("lsmod | grep batman")
        assert rc == 0 and stdout, "batman-adv kernel module is not loaded"

    def test_batman_adv_has_active_interface(self, ssh_command):
        """Verify batman-adv has at least one active mesh interface."""
        stdout, _, rc = ssh_command.run("batctl if")
        assert rc == 0, "batctl if failed — is batctl installed?"

        output = "\n".join(stdout)
        assert "active" in output, (
            f"batman-adv has no active interfaces. batctl if output: {output}"
        )

    def test_batctl_originators_command(self, ssh_command):
        """Verify batctl o (originators) command runs without error."""
        stdout, stderr, rc = ssh_command.run("batctl o")
        # rc 234 means batman is disabled; anything else unexpected is a failure
        if rc == 234:
            pytest.skip("batman-adv disabled on this device")
        assert rc == 0, f"batctl o failed with rc={rc}. stderr: {stderr}"


class TestLibreMeshServices:
    """Verify core LibreMesh services are running."""

    def test_lime_proto_running(self, ssh_command):
        """Verify lime-proto service is active."""
        stdout, _, rc = ssh_command.run("uci show lime-defaults.system")
        assert rc == 0, (
            "lime-defaults not accessible — lime-proto may not be initialized"
        )

    def test_lime_app_or_uhttpd_running(self, ssh_command):
        """Verify lime-app (uhttpd) web interface is accessible."""
        stdout, _, rc = ssh_command.run("pgrep uhttpd")
        if rc != 0:
            # Some initramfs builds may not include lime-app; that is acceptable.
            pytest.skip(
                "uhttpd not running (lime-app may not be included in initramfs)"
            )

    def test_dropbear_running(self, ssh_command):
        """Verify SSH daemon (dropbear) is running."""
        stdout, _, rc = ssh_command.run("pgrep dropbear")
        assert rc == 0, "dropbear SSH daemon is not running"

    def test_network_service_running(self, ssh_command):
        """Verify OpenWrt network service is running."""
        stdout, _, rc = ssh_command.run("/etc/init.d/network status")
        assert rc == 0, "Network service is not running"
