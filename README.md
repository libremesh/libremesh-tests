# libremesh-tests

Pytest suite for **LibreMesh** on real hardware (labgrid) and **virtual mesh** (QEMU + vwifi).

Covers LibreMesh single-node validation, multi-node mesh connectivity, and virtual QEMU mesh.
The shared device/lab inventory (`labnet.yaml`) is maintained upstream in
[aparcar/openwrt-tests](https://github.com/aparcar/openwrt-tests) and is not duplicated here.

## Requirements

- Python 3.13+ and [`uv`](https://docs.astral.sh/uv/)
- On the lab host: labgrid client, local coordinator (loopback :20408), exporter
- For QEMU: `qemu-system-x86_64` and related packages (see virtual mesh docs)

## Setup

```shell
cd /path/to/libremesh-tests
uv sync
```

**`labnet.yaml` location:** pytest resolves the inventory file in this order:
`LABNET_PATH` (explicit file path), `OPENWRT_TESTS_DIR/labnet.yaml`, or a **sibling clone**
`../openwrt-tests/labnet.yaml` (recommended local layout: check out
[aparcar/openwrt-tests](https://github.com/aparcar/openwrt-tests) next to this repository).
For CI or ad-hoc use without that layout, set `LABNET_PATH` directly.

**Dynamic VLAN switching:** Multi-node mesh tests only (`LG_MESH_PLACES` set) run `switch-vlan`
to move DUTs to mesh VLAN (200) on the **lab host** via SSH (the same host as `LG_PROXY`).
Single-node tests keep each DUT on its isolated exporter VLAN. No local
`dut-config.yaml` or `labgrid-switch-abstraction` install is needed on the developer
machine - only on the lab host. If VLANs are already correct, `VLAN_SWITCH_DISABLED=1`
skips switch commands.

Place-to-DUT name mapping defaults to the FCEFyN convention (strips `labgrid-fcefyn-`). For
other labs, override with `PLACE_PREFIX` (e.g. `export PLACE_PREFIX="labgrid-mylab-"`, or
`PLACE_PREFIX=""` to fall back to stripping up to the second hyphen).

Lab infrastructure (coordinator, exporter, TFTP, Ansible) is documented in
**fcefyn_testbed_utils** and upstream **aparcar/openwrt-tests** (`ansible/`);
this repo does not ship `ansible/`.

## Targets shipped here

Only DUTs used for LibreMesh / FCEFyN workflows:

| File | Role |
|------|------|
| `targets/openwrt_one.yaml` | OpenWrt One |
| `targets/bananapi_bpi-r4.yaml` | Banana Pi R4 |
| `targets/linksys_e8450.yaml` | Belkin RT3200 / Linksys E8450 |
| `targets/librerouter_librerouter-v1.yaml` | LibreRouter v1 (single-image, default). `librerouter_v1.yaml` is a symlink to this file for callers using the short OpenWrt profile name. |
| `targets/librerouter_librerouter-v1-dual-tftp.yaml` | LibreRouter v1 (kernel + rootfs.uimage, for lime-packages CI). `librerouter_v1-dual-tftp.yaml` is a symlink to this file. |
| `targets/qemu_x86-64_libremesh.yaml` | QEMU x86-64 LibreMesh |

## Running tests

### Single-node LibreMesh (physical DUT)

```shell
export LG_PLACE=labgrid-fcefyn-belkin_rt3200_2
export LG_PROXY=labgrid-fcefyn
export LG_IMAGE=/srv/tftp/firmwares/.../lime-....bin

uv run labgrid-client lock
uv run pytest tests/test_libremesh.py --log-cli-level=CONSOLE -v
uv run labgrid-client unlock
```

`LG_IMAGE` must be a real file on the machine running labgrid-client (see remote access docs
in fcefyn_testbed_utils).

### Single-node on QEMU (LibreMesh image)

```shell
uv run pytest tests/test_base.py tests/test_lan.py tests/test_libremesh.py \
  --lg-env targets/qemu_x86-64_libremesh.yaml \
  --firmware /path/to/lime-x86-64-generic-ext4-combined.img \
  --lg-log --log-cli-level=CONSOLE --lg-colored-steps -v
```

### Multi-node mesh (physical)

Requires `LG_MESH_PLACES`, images, and mesh VLAN (see `tests/conftest_mesh.py`).

```shell
export LG_MESH_PLACES="labgrid-fcefyn-openwrt_one,labgrid-fcefyn-bananapi_bpi-r4"
export LG_IMAGE_MAP="labgrid-fcefyn-openwrt_one=/path/img1.itb,labgrid-fcefyn-bananapi_bpi-r4=/path/img2.itb"

uv run pytest tests/test_mesh.py -v --log-cli-level=INFO
```

### Virtual mesh (QEMU + vwifi, no labgrid)

```shell
export LG_VIRTUAL_MESH=1
export VIRTUAL_MESH_IMAGE=/path/to/lime-vwifi-x86-64-ext4-combined.img
export VIRTUAL_MESH_NODES=3

uv run pytest tests/test_mesh.py -v --log-cli-level=INFO
```

## Firmware catalog

`configs/firmware-catalog.yaml` lists TFTP paths for CI and local scripts. Resolve paths with:

```shell
uv run python scripts/resolve_firmware_from_catalog.py linksys_e8450
```

## CI

This repository is a **pure test library**. The active LibreMesh CI
(per-PR build + QEMU + opt-in physical, plus the daily lab
validation cron) lives in
[`fcefyn-testbed/lime-packages/.github/workflows/build-firmware.yml`](https://github.com/fcefyn-testbed/lime-packages/blob/master/.github/workflows/build-firmware.yml)
and **checks out this repo at runtime** to get the pytest suites,
labgrid env files, virtual-mesh launcher and the helper modules
under `tests/`.

Only one workflow runs against this repository on push/PR:

- `.github/workflows/formal.yml` - `ruff` + `isort` + cross-version
  `uv sync`. Pure Python lint, no lab access.

The legacy `daily.yml` and `pull_requests.yml` workflows that used
to ride the lab from this repo were retired in May 2026 when the
CI was unified in `fcefyn-testbed/lime-packages`. The shared
`configs/firmware-catalog.yaml` and `scripts/resolve_firmware_from_catalog.py`
remain available for local manual runs.

## Relationship with openwrt-tests

`libremesh-tests` started as a GitHub fork of
[aparcar/openwrt-tests](https://github.com/aparcar/openwrt-tests). The fork
was dropped once the suite had its own CI cycle and tracking upstream changes
was no longer necessary.

### Reused from openwrt-tests

- **`labnet.yaml`** - shared device/lab inventory (resolved from a sibling
  `../openwrt-tests/` clone or via `LABNET_PATH`).
- **Labgrid coordinator and exporter** - each lab runs its own
  `labgrid-coordinator` locally (loopback :20408), deployed by the upstream
  Ansible playbook `playbook_labgrid.yml` in `openwrt-tests`. Paul's VM
  (`global-coordinator` hostname) is an **SSH gateway** only - it does not run
  `labgrid-coordinator`. Both openwrt-tests CI (GitHub-hosted runners tunneling
  via the gateway) and lime-packages CI (self-hosted runner on the lab host)
  reach the **same local coordinator**, ensuring shared concurrency control.
  WireGuard provides SSH transport between the gateway VM and the labs.
- **Base test fixtures** (`ssh_command`, `shell_command`, target resolution) -
  imported at runtime via labgrid and the upstream helpers.

### Rewritten / added for LibreMesh

| Component | Description |
|-----------|-------------|
| `strategies/tftpstrategy.py` | Adapted from upstream `UBootTFTPStrategy`: adds U-Boot interrupt spam tolerant to high-latency proxied consoles, `TFTP_SERVER_IP` override for mesh VLANs, and retry-with-cooldown logic. |
| `strategies/qemunetworkstrategy_libremesh.py` | QEMU network strategy for LibreMesh with `vwifi` virtual WiFi. |
| `tests/conftest_mesh.py`, `tests/conftest_vlan.py` | Multi-node orchestration and dynamic VLAN switching fixtures (no equivalent in openwrt-tests). |
| `tests/test_libremesh.py` | Single-node LibreMesh validation (packages, addressing, hostname, bat0, services). |
| `tests/test_mesh.py` | Multi-node mesh suite (ping, batman-adv, babeld, convergence). |

## Contributing a lab

See [docs/CONTRIBUTING_LAB.md](docs/CONTRIBUTING_LAB.md).

## Docs

- [docs/CONTRIBUTING_LAB.md](docs/CONTRIBUTING_LAB.md) - lab onboarding
- [docs/labs/fcefynlab.md](docs/labs/fcefynlab.md) - FCEFyN hardware reference
