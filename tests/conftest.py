# Copyright 2023 by Garmin Ltd. or its subsidiaries
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import os
import sys
from os import getenv
from pathlib import Path

import pytest

logger = logging.getLogger(__name__)

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from lime_helpers import (  # noqa: E402
    align_ssh_networkservice_with_mesh_vlan,
    assert_libremesh_runtime,
    configure_fixed_ip,
    ensure_batman_mesh,
    is_qemu_target,
    lime_report_dir,
    lime_report_filename,
    resolve_target_yaml,
    save_lime_report,
    suppress_kernel_console,
)

logger = logging.getLogger(__name__)

pytest_plugins = ["conftest_mesh", "conftest_vlan"]

device = getenv("LG_ENV", "Unknown").split("/")[-1].split(".")[0]


def _resolve_target_from_place() -> str | None:
    """Auto-set LG_ENV from LG_PLACE when LG_ENV is not provided."""
    lg_env = getenv("LG_ENV")
    lg_place = getenv("LG_PLACE")

    if lg_env or not lg_place:
        return lg_env

    try:
        return resolve_target_yaml(lg_place)
    except (ValueError, FileNotFoundError):
        return None


def pytest_addoption(parser):
    parser.addoption("--firmware", action="store", default="firmware.bin")


def pytest_configure(config):
    config._metadata = getattr(config, "_metadata", {})
    config._metadata["version"] = "12.3.4"
    config._metadata["environment"] = "staging"

    config.addinivalue_line("markers", "mesh: multi-node LibreMesh mesh tests")

    resolved_env = _resolve_target_from_place()
    if resolved_env:
        os.environ["LG_ENV"] = resolved_env


def pytest_collection_modifyitems(config, items):
    """Skip multi-node mesh tests unless a mesh harness is requested.

    Two opt-ins are supported:

    * ``LG_MESH_PLACES`` (CSV of labgrid place names) drives the physical
      mesh path: ``conftest_mesh.py`` resolves each place into a target
      and the tests run against real hardware.
    * ``LG_VIRTUAL_MESH=1`` drives the QEMU+vwifi virtual mesh path:
      ``fork-libremesh-virtual-mesh`` spawns N x86-64 QEMU instances and
      ties them through ``vwifi-server`` so 802.11s-over-mac80211_hwsim
      forms an actual mesh on the host. ``VIRTUAL_MESH_NODES`` and
      ``VIRTUAL_MESH_IMAGE`` are read by that harness, not by this hook.

    Either env var being set is enough to keep the ``mesh``-marked tests
    in the collection. The CI workflow's ``test-mesh-qemu`` job exports
    only ``LG_VIRTUAL_MESH=1`` (not ``LG_MESH_PLACES``) — without this
    early-return the QEMU mesh tests would always be skipped despite a
    correctly-provisioned vwifi/QEMU harness.
    """
    if os.environ.get("LG_MESH_PLACES", "").strip():
        return
    if os.environ.get("LG_VIRTUAL_MESH", "").strip() == "1":
        return
    skip_mesh = pytest.mark.skip(
        reason="Set LG_MESH_PLACES or LG_VIRTUAL_MESH=1 to run multi-node mesh tests",
    )
    for item in items:
        if "mesh" in item.keywords:
            item.add_marker(skip_mesh)


def ubus_call(command, namespace, method, params={}):
    output = command.run_check(f"ubus call {namespace} {method} '{json.dumps(params)}'")

    try:
        return json.loads("\n".join(output))
    except json.JSONDecodeError:
        return {}


@pytest.fixture(scope="session", autouse=True)
def setup_env(pytestconfig):
    """Inject --firmware into labgrid's env config when labgrid is active.

    Unlike upstream, this fixture does NOT take labgrid's ``env`` fixture as a
    parameter.  Reason: multi-node mesh tests (conftest_mesh.py) run without
    ``--lg-env`` and therefore labgrid never creates an ``env``.  If this
    fixture depended on ``env``, pytest would skip or error on every mesh test.
    We resolve the env from pytestconfig.stash instead, which is a no-op when
    labgrid is inactive.
    """
    try:
        from labgrid.pytestplugin.hooks import LABGRID_ENV_KEY

        env = pytestconfig.stash.get(LABGRID_ENV_KEY, None)
    except (ImportError, KeyError):
        env = None
    if env is not None:
        env.config.data.setdefault("images", {})["firmware"] = pytestconfig.getoption(
            "firmware"
        )


@pytest.fixture
def shell_command(strategy):
    try:
        strategy.transition("shell")
        shell = strategy.shell
        suppress_kernel_console(shell)
    except Exception:
        logger.exception("Failed to transition to state shell")
        pytest.exit("Failed to transition to state shell", returncode=3)

    # Hard-fail before any test logic if the booted image is not LibreMesh.
    # Catches both "CI build silently dropped lime-*" and "DUT autobooted
    # from on-flash OpenWrt" failure modes — see assert_libremesh_runtime()
    # docstring. Kept OUTSIDE the broad except above on purpose: the helper
    # raises pytest.exit (a subclass of Exception in modern pytest), so
    # wrapping it here would mask the detailed diagnostic with the generic
    # "Failed to transition to state shell" message.
    assert_libremesh_runtime(shell)
    return shell


@pytest.fixture
def ssh_command(shell_command, target):
    if not is_qemu_target(target):
        ensure_batman_mesh(target)
        # mesh_vlan_multi / conftest_mesh set TFTP_SERVER_IP and VLAN 200;
        # keep SSH ProxyCommand bind iface in sync (exporter still shows %vlanNNN).
        align_ssh_networkservice_with_mesh_vlan(target)

    fixed_ip = configure_fixed_ip(shell_command, target)
    if fixed_ip:
        logger.info("Using fixed IP %s for SSH", fixed_ip)

    ssh = target.get_driver("SSHDriver")
    return ssh


@pytest.fixture(scope="session", autouse=True)
def collect_lime_report(pytestconfig):
    """Collect lime-report after all session tests finish.

    Runs as a session-scoped autouse fixture so it tears down BEFORE
    labgrid's ``env.cleanup()`` — the SSHDriver is still active here.

    Output path: ``LIME_REPORT_OUTPUT`` if set, otherwise
    ``lime-report.md`` inside the directory resolved by
    :func:`lime_helpers.lime_report_dir` (``LIME_REPORT_DIR`` or the
    ``--lg-log`` directory). Every CI job passes ``--lg-log``, so physical
    and QEMU runs both end with a report next to the console logs.

    Resolves the labgrid target lazily via pytestconfig.stash instead of
    depending on the ``target`` fixture directly, mirroring the pattern used
    by ``setup_env``. This is required because multi-node mesh tests
    (conftest_mesh.py) run without ``--lg-env`` and therefore labgrid never
    creates an ``env``. If this fixture depended on ``target``, every mesh
    session would be aborted with ``missing environment config (use --lg-env)``.
    Mesh sessions collect one report per node in the ``mesh_nodes`` teardown
    (conftest_mesh.py), while the VMs/DUTs are still up.
    """
    yield

    explicit_output = os.environ.get("LIME_REPORT_OUTPUT", "").strip()
    if explicit_output:
        report_path = Path(explicit_output)
    else:
        report_dir = lime_report_dir(pytestconfig)
        if report_dir is None:
            return
        report_path = report_dir / lime_report_filename()

    try:
        from labgrid.pytestplugin.hooks import LABGRID_ENV_KEY

        env = pytestconfig.stash.get(LABGRID_ENV_KEY, None)
    except (ImportError, KeyError):
        env = None

    if env is None:
        # No labgrid env active (multi-node mesh session): the per-node
        # reports are written by the mesh_nodes fixture teardown.
        return

    try:
        target = env.get_target()
        if target is None:
            return
        ssh = target.get_driver("SSHDriver")
    except Exception as exc:  # noqa: BLE001 - report collection must not abort teardown
        msg = f"_Could not collect lime-report: {exc}_"
        print(f"\n[WARN] {msg}", file=sys.stderr, flush=True)
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(msg + "\n")
        except OSError:
            pass
        return

    ok = save_lime_report(report_path, ssh.run)
    status = "collected" if ok else "NOT collected (see file for reason)"
    print(f"\nlime-report {status} → {report_path}", flush=True)
