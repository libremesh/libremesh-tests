import enum
import hashlib
import ipaddress
import logging
import os
import threading
import time

import attr
from labgrid.driver import TFTPProviderDriver
from labgrid.factory import target_factory
from labgrid.resource.remote import RemoteTFTPProvider
from labgrid.strategy.common import Strategy, StrategyError

logger = logging.getLogger(__name__)

TFTP_DOWNLOAD_TIMEOUT = 120
TFTP_RETRY_INTERVAL = 5
DEFAULT_UBOOT_RETRIES = 2
DEFAULT_UBOOT_RETRY_COOLDOWN = 5
DEFAULT_UBOOT_RETRY_BUDGET = 360
SERIAL_DRAIN_CHUNK = 4096
SERIAL_DRAIN_TIMEOUT = 0.5
SERIAL_DRAIN_TOTAL_MAX = 3.0
DEFAULT_UBOOT_INTERRUPT_SPAM_SEC = 12.0
DEFAULT_UBOOT_INTERRUPT_SPAM_INTERVAL = 0.05


class Status(enum.Enum):
    unknown = 0
    off = 1
    uboot = 2
    shell = 3


def _read_int_env(name: str, default: int) -> int:
    """Return a non-negative integer from the environment."""
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


def _read_float_env(name: str, default: float) -> float:
    """Return a non-negative float from the environment."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Invalid float for %s=%r, using default %.3f", name, raw, default
        )
        return default
    if value < 0:
        logger.warning(
            "Negative float for %s=%r, using default %.3f", name, raw, default
        )
        return default
    return value


def _retry_action(
    action, cleanup, retries: int, cooldown: int, label: str, sleep_fn=time.sleep
):
    """Run an action with bounded retries and best-effort cleanup between tries."""
    max_attempts = max(1, retries + 1)
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            return action()
        except Exception as exc:
            last_error = exc
            if attempt >= max_attempts:
                raise
            logger.warning(
                "%s failed on attempt %d/%d (%s), retrying in %ds",
                label,
                attempt,
                max_attempts,
                type(exc).__name__,
                cooldown,
            )
            try:
                cleanup()
            except Exception:
                logger.warning("%s cleanup failed before retry", label, exc_info=True)
            sleep_fn(cooldown)

    raise last_error


@target_factory.reg_driver
@attr.s(eq=False)
class UBootTFTPStrategy(Strategy):
    """Strategy that boots via U-Boot TFTP with retry logic for STP delay.

    After a PoE power cycle the switch port may need 30-60s to reach
    forwarding state (STP convergence, link negotiation).  Download
    commands (tftp/dhcp) extracted from init_commands are retried with
    a 60s per-attempt timeout until TFTP_DOWNLOAD_TIMEOUT expires.

    The TFTP server IP can be overridden via TFTP_SERVER_IP env var.
    This is used by multi-node mesh tests where ``mesh_vlan_multi`` moves
    DUTs to VLAN 200 before boot, making the exporter's isolated-VLAN
    external_ip unreachable.
    """

    bindings = {
        "power": "PowerProtocol",
        "console": "ConsoleProtocol",
        "uboot": "LinuxBootProtocol",
        "shell": "ShellDriver",
        "tftp": "TFTPProviderDriver",
    }
    tftp: TFTPProviderDriver

    status = attr.ib(default=Status.unknown)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()
        self._download_cmds = []
        self._base_init_commands = tuple(self.uboot.init_commands)

    def _download_with_retry(self, download_cmd):
        """Run a U-Boot download command with retries for link-up delay."""
        deadline = time.monotonic() + TFTP_DOWNLOAD_TIMEOUT
        attempt = 0

        while True:
            attempt += 1
            remaining = deadline - time.monotonic()
            logger.info(
                "TFTP download attempt %d, cmd='%s', %.0fs remaining",
                attempt,
                download_cmd,
                remaining,
            )
            try:
                self.uboot.run_check(download_cmd, timeout=60)
                logger.info("TFTP download succeeded on attempt %d", attempt)
                return
            except Exception as e:
                remaining = deadline - time.monotonic()
                if remaining <= TFTP_RETRY_INTERVAL:
                    raise StrategyError(
                        f"TFTP download failed after {attempt} attempts "
                        f"({TFTP_DOWNLOAD_TIMEOUT}s): {e}"
                    ) from e
                logger.warning(
                    "TFTP attempt %d failed (%s), retrying in %ds (%.0fs left)",
                    attempt,
                    type(e).__name__,
                    TFTP_RETRY_INTERVAL,
                    remaining,
                )
                time.sleep(TFTP_RETRY_INTERVAL)

    def _derive_ethaddr(self):
        """Return a deterministic locally-administered unicast MAC for this place.

        Some mt7622 Belkin RT3200 units have a degraded/erased factory MTD
        partition whose MAC region reads all 0xFF. U-Boot then rejects
        ``ff:ff:ff:ff:ff:ff`` as an illegal address, ARP never resolves and
        TFTP fails with "ARP Retry count exceeded". Forcing a valid unicast
        MAC into the U-Boot env before the transfer restores it; Linux still
        derives its own MAC after boot, so this only affects the TFTP stage.

        The MAC is derived per-place so that nodes sharing VLAN 200 during
        mesh tests never collide. Override with LG_UBOOT_ETHADDR.
        """
        override = os.environ.get("LG_UBOOT_ETHADDR", "").strip()
        if override:
            return override
        place = (
            os.environ.get("LG_PLACE", "").strip()
            or getattr(self.target, "name", "")
            or "labgrid"
        )
        digest = hashlib.sha256(place.encode()).digest()
        # First octet 0x02: locally-administered (bit 1 set), unicast (bit 0 clear).
        octets = [0x02] + list(digest[:5])
        return ":".join(f"{b:02x}" for b in octets)

    def _resolve_tftp_server_ip(self):
        """Return the TFTP server IP, preferring env override over exporter config."""
        override = os.environ.get("TFTP_SERVER_IP", "").strip()
        if override:
            logger.info("Using TFTP_SERVER_IP override: %s", override)
            return override
        exporter_ip = self.target.get_resource(
            RemoteTFTPProvider, wait_avail=False
        ).external_ip
        logger.info("Using exporter external_ip: %s", exporter_ip)
        return exporter_ip

    def _prepare_uboot_commands(self, staged_file, tftp_server_ip, staged_initrd=None):
        """Prepare a fresh set of U-Boot init commands for this boot attempt."""
        bootfile_cmds = (f"setenv bootfile {staged_file}",)
        if staged_initrd:
            bootfile_cmds += (f"setenv bootfile_initrd {staged_initrd}",)

        init_commands = bootfile_cmds + self._base_init_commands

        if tftp_server_ip:
            tftp_dut_ip = ipaddress.ip_address(tftp_server_ip) + 1
            init_commands = (
                f"setenv serverip {tftp_server_ip}",
                f"setenv ipaddr {tftp_dut_ip}",
            ) + init_commands

        # Force a valid unicast MAC so units with an erased factory MAC
        # (reads as ff:ff:ff:ff:ff:ff) can still ARP and TFTP. See
        # _derive_ethaddr for rationale. Opt out with LG_UBOOT_SET_ETHADDR=0.
        #
        # ethaddr is write-once in U-Boot, so a plain `setenv ethaddr` fails
        # with `Can't overwrite "ethaddr"`. The `-f` (force) flag bypasses
        # the protection; `|| true` keeps a valid-MAC unit booting even if a
        # given U-Boot build rejects the override.
        if _read_int_env("LG_UBOOT_SET_ETHADDR", 1):
            ethaddr = self._derive_ethaddr()
            init_commands = (f"setenv -f ethaddr {ethaddr} || true",) + init_commands

        download_prefixes = ("tftp", "dhcp")
        self.uboot.init_commands = tuple(
            c for c in init_commands if not c.strip().startswith(download_prefixes)
        )
        self._download_cmds = [
            c for c in init_commands if c.strip().startswith(download_prefixes)
        ]

    def _effective_uboot_retries(self) -> int:
        """Cap configured U-Boot retries so one child cannot exhaust the boot budget."""
        configured = _read_int_env("LG_MESH_UBOOT_RETRIES", DEFAULT_UBOOT_RETRIES)
        login_timeout = int(getattr(self.uboot, "login_timeout", 60) or 60)
        login_timeout = max(1, login_timeout)
        max_attempts = max(1, DEFAULT_UBOOT_RETRY_BUDGET // login_timeout)
        effective = min(configured, max_attempts - 1)
        if effective != configured:
            logger.info(
                "Capping U-Boot retries from %d to %d for login_timeout=%ds "
                "(budget=%ds)",
                configured,
                effective,
                login_timeout,
                DEFAULT_UBOOT_RETRY_BUDGET,
            )
        return effective

    def _drain_serial_buffer(self):
        """Consume stale data from the pexpect buffer after a power cycle.

        Bounded by SERIAL_DRAIN_TOTAL_MAX so that a DUT which ended up
        booting into Linux (continuous serial output) does not block
        the strategy indefinitely.
        """
        deadline = time.monotonic() + SERIAL_DRAIN_TOTAL_MAX
        try:
            while time.monotonic() < deadline:
                data = self.console.read(
                    size=SERIAL_DRAIN_CHUNK, timeout=SERIAL_DRAIN_TIMEOUT
                )
                if not data:
                    break
        except Exception:
            pass

    def _spam_uboot_interrupt(self, during=None):
        """Blast the U-Boot interrupt byte at a steady rate around power-up.

        Rationale: when the serial console is proxied over a high-latency SSH
        tunnel (e.g. remote developer running via labgrid-coordinator
        ProxyJump + WireGuard), labgrid's pattern-match on the autoboot string
        can react too late and U-Boot auto-boots the kernel. The UBootDriver
        then waits up to login_timeout seconds for a prompt that will never
        appear and fails with TIMEOUT.

        Writing the interrupt character continuously during the expected
        autoboot window lands the stop signal inside U-Boot regardless of
        jitter. Bytes at the U-Boot prompt are harmless (they produce empty
        command echoes that are consumed by the next expect).

        ``during`` is the power-cycle call. It runs while a background thread
        keeps writing, because the call does not necessarily return at the
        moment power is restored: relay-driven DUTs cycle in ~9 s, but PoE
        ports driven over SSH to the switch take 90-100 s, during which the
        DUT powers up and boots past the autoboot window. Writing only after
        the call returns misses that window entirely.

        Opt-out with LG_MESH_UBOOT_INTERRUPT_SPAM_SEC=0.
        """
        duration = _read_float_env(
            "LG_MESH_UBOOT_INTERRUPT_SPAM_SEC", DEFAULT_UBOOT_INTERRUPT_SPAM_SEC
        )
        if duration <= 0:
            if during is not None:
                during()
            return

        interval = _read_float_env(
            "LG_MESH_UBOOT_INTERRUPT_SPAM_INTERVAL",
            DEFAULT_UBOOT_INTERRUPT_SPAM_INTERVAL,
        )
        raw_char = getattr(self.uboot, "interrupt", None) or "\n"
        interrupt_bytes = raw_char.encode("ASCII")

        logger.info(
            "Spamming U-Boot interrupt during power-cycle and for %.1fs after "
            "it returns (interval=%.3fs)",
            duration,
            interval,
        )

        stop = threading.Event()
        state = {"writes": 0, "errors": 0}

        def _write_loop():
            # Writes while the DUT is still powered off raise; keep going so
            # the spam resumes as soon as the console comes back, instead of
            # giving up before the autoboot window even opens.
            while not stop.is_set():
                try:
                    self.console.write(interrupt_bytes)
                    state["writes"] += 1
                except Exception:
                    state["errors"] += 1
                    if state["errors"] == 1:
                        logger.debug(
                            "U-Boot interrupt spam write failed", exc_info=True
                        )
                stop.wait(interval)

        writer = threading.Thread(
            target=_write_loop, name="uboot-interrupt-spam", daemon=True
        )
        writer.start()
        try:
            if during is not None:
                during()
            stop.wait(duration)
        finally:
            stop.set()
            writer.join(timeout=interval * 4 + 1.0)

        logger.debug(
            "U-Boot interrupt spam sent %d writes (%d failed)",
            state["writes"],
            state["errors"],
        )

    def _transition_to_uboot_once(self):
        """Power-cycle the node and activate the U-Boot console."""
        self.target.activate(self.tftp)
        staged_file = self.tftp.stage(self.target.env.config.get_image_path("root"))

        staged_initrd = None
        try:
            initrd_path = self.target.env.config.get_image_path("initrd")
            staged_initrd = self.tftp.stage(initrd_path)
            logger.info("Staged initrd image: %s", staged_initrd)
        except KeyError:
            pass

        tftp_server_ip = self._resolve_tftp_server_ip()

        self.target.deactivate(self.console)
        self.target.activate(self.power)
        self.target.activate(self.console)

        # ORDER MATTERS: spam ACROSS the power cycle, and BEFORE drain.
        # The DUT begins producing serial output immediately after
        # power-on, and the autoboot window is only ~1-3 s wide. If we
        # drain first, the drain blocks for as long as the DUT keeps
        # talking (boot banner, Linux bootlog, etc.), by which point the
        # kernel is booting. Spamming first ensures our interrupt bytes
        # hit the serial inside that narrow window; the drain afterwards
        # consumes our own echoes and the boot banner so that pexpect's
        # buffer is clean when _await_prompt() runs.
        self._spam_uboot_interrupt(during=self.power.cycle)
        self._drain_serial_buffer()

        self._prepare_uboot_commands(staged_file, tftp_server_ip, staged_initrd)
        self.target.activate(self.uboot)
        self.status = Status.uboot

    def transition_to_uboot_with_retry(self):
        """Reach the U-Boot prompt with bounded retries."""
        retries = self._effective_uboot_retries()
        cooldown = _read_int_env(
            "LG_MESH_UBOOT_RETRY_COOLDOWN", DEFAULT_UBOOT_RETRY_COOLDOWN
        )
        _retry_action(
            self._transition_to_uboot_once,
            lambda: self.transition(Status.off),
            retries=retries,
            cooldown=cooldown,
            label="U-Boot activation",
        )

    def run_download_commands(self):
        """Execute prepared TFTP/DHCP download commands."""
        if self.status != Status.uboot:
            self.transition_to_uboot_with_retry()
        for cmd in self._download_cmds:
            self._download_with_retry(cmd)

    def boot_kernel(self):
        """Issue the boot command and wait for the kernel handoff."""
        self.uboot.boot("")
        self.uboot.await_boot()

    def activate_shell(self):
        """Activate the shell driver after the kernel has booted."""
        self.target.activate(self.shell)
        self.status = Status.shell

    def transition(self, status):
        if not isinstance(status, Status):
            status = Status[status]
        if status == Status.unknown:
            raise StrategyError(f"can not transition to {status}")
        elif status == self.status:
            return
        elif status == Status.off:
            self.target.deactivate(self.console)
            self.target.activate(self.power)
            self.power.off()
        elif status == Status.uboot:
            self.transition_to_uboot_with_retry()
        elif status == Status.shell:
            self.transition(Status.uboot)
            self.run_download_commands()
            self.boot_kernel()
            self.activate_shell()
        else:
            raise StrategyError(f"no transition found from {self.status} to {status}")
        self.status = status

    def force(self, status):
        if not isinstance(status, Status):
            status = Status[status]
        if status == Status.off:
            self.target.activate(self.power)
        elif status == Status.uboot:
            self.target.activate(self.uboot)
        elif status == Status.shell:
            self.target.activate(self.shell)
        else:
            raise StrategyError("can not force state {}".format(status))
        self.status = status
