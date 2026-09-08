# Target Device Quirks

Device-specific boot behaviors, workarounds, and configuration notes for Labgrid targets.

## Table of Contents

- [LibreRouter v1 (ath79)](#librerouter-v1-ath79)
- [OpenWrt One (filogic)](#openwrt-one-filogic)
- [Linksys E8450 / Belkin RT3200 (mt7622)](#linksys-e8450--belkin-rt3200-mt7622)
- [BananaPi BPi-R4 (mt7988)](#bananapi-bpi-r4-mt7988)

## Common Patterns

### Strict Shell Prompt

All physical targets use a strict prompt regex that only matches LibreMesh's default hostname `LiMe-<6 hex>`. This ensures tests never engage with intermediate prompts like `root@(none):~#` or `root@OpenWrt:~#` that appear before `lime-config` completes. If the prompt times out, investigate the initramfs or boot sequence rather than relaxing the regex.

### UBI Overlay Wipe

Filogic/mt7622/mt7988 devices with prior OpenWrt installations have a `rootfs_data` UBI volume on NAND. During preinit, `mount_root` finds it and overlays it on the in-RAM rootfs, replacing our TFTP'd LibreMesh userspace with whatever was previously flashed. The `ubi remove rootfs_data || true` commands in `init_commands` prevent this by removing the stale volume before boot. The `|| true` makes it idempotent on virgin devices.

### FIT Configuration Pinning

Devices booting FIT images need `setenv bootconf config-1` to match the configuration node name in our generated FIT. Without this, U-Boot may use a saved `bootconf` from previous firmware, causing `Could not find configuration node` errors. The setenv is RAM-only (no `saveenv`), preserving the device's persistent environment.

### Environment-side bootargs Override

Even though `pi-lime-packages` embeds `bootargs` in the FIT configuration node, devices may have saved `bootargs` in U-Boot env with flash-oriented values like `root=/dev/fit0`. Some U-Boot versions prefer env-side over FIT-side bootargs, causing the kernel to ignore our initramfs. Setting `bootargs` explicitly in `init_commands` (RAM-only) closes this gap.

---

## LibreRouter v1 (ath79)

**Target files (two variants, each with a short-name symlink):**

| File | When to use | Required env vars |
|---|---|---|
| `targets/librerouter_librerouter-v1.yaml` (canonical) | Default. Boots a self-contained `*-initramfs-kernel.bin` (LibreMesh releases, source builds, mesh tests, `openwrt-tests` healthcheck via `labnet.yaml`). | `LG_IMAGE` |
| `targets/librerouter_v1.yaml` (symlink) | Same as above; kept for callers that use the short OpenWrt device profile name (`dut-config.yaml` + `pi-lime-packages` `matrix.device`). | `LG_IMAGE` |
| `targets/librerouter_librerouter-v1-dual-tftp.yaml` (canonical) | `pi-lime-packages` / `lime-packages` CI when ImageBuilder emits `kernel.bin` + `rootfs.uimage` separately. | `LG_IMAGE` + `LG_IMAGE_INITRD` |
| `targets/librerouter_v1-dual-tftp.yaml` (symlink) | Same as above, short-name flavor used by `tools/ci/lab_stage_firmware.sh` (`DUAL_ENV="targets/${DEVICE}-dual-tftp.yaml"`). | `LG_IMAGE` + `LG_IMAGE_INITRD` |

### Two naming conventions

The librerouter has two device names in the wider testbed ecosystem:

- **`librerouter_v1`** (short, matches the OpenWrt ath79 device profile) is used by `fcefyn_testbed_utils/configs/dut-config.yaml`, the `pi-lime-packages` build matrix, and any script that starts from the OpenWrt device profile.
- **`librerouter_librerouter-v1`** (long, `<vendor>_<profile>`) is used by `openwrt-tests/labnet.yaml`, labgrid `RemotePlace` names, and the exporter YAML.

The canonical file uses the long form so `conftest_mesh.py::resolve_target_yaml()` (which resolves through `labnet.yaml`) finds it directly. The short-form symlinks let `lime-packages` CI and any other short-name caller resolve to the same file without duplicating the YAML.

### Dual-TFTP Boot

The ath79 ImageBuilder cannot produce an initramfs kernel (`CONFIG_INITRAMFS_SOURCE` requires kernel recompilation). CI uses a dual-TFTP approach:

| Address | File | Description |
|---------|------|-------------|
| `0x82000000` | `kernel.bin` | uImage lzma kernel + appended DTB |
| `0x84000FC0` | `rootfs.uimage` | uImage ramdisk wrapping newc CPIO |

The ramdisk loads to `0x84000FC0` so the CPIO data (past the 64-byte uImage header) starts at `0x84001000` - page-aligned as required by the kernel.

### Why bootm with Two Arguments

The ath79 kernel has `CONFIG_MIPS_CMDLINE_FROM_DTB=y`, which reads `chosen/bootargs` from the embedded DTB and ignores U-Boot's `bootargs` environment variable. Parameters like `rd_start`/`rd_size` passed via bootargs never reach the kernel.

Using `bootm 0x82000000 0x84000FC0` tells U-Boot about both images natively. The `do_bootm_linux()` function passes `initrd_start`/`initrd_end` to the MIPS kernel via `linux_env_set()`, bypassing the kernel command-line entirely.

### U-Boot Interrupt

LibreRouter's Atheros U-Boot 1.1.x uses a simple "Hit any key" loop with 2s `CONFIG_BOOTDELAY`. A single newline aborts autoboot - `tftpstrategy.py` sends these for ~12s to bracket the window.

---

## OpenWrt One (filogic)

**Target file:** `targets/openwrt_one.yaml`

### UBI Overlay Issue

OpenWrt 24.10 on filogic uses fitblk + UBIFS overlay even for initramfs boots. If the device has a `rootfs_data` volume from a previous sysupgrade, `mount_root` overlays it on our in-RAM rootfs. Symptoms: kernel version matches today's build, but `/etc/banner`, hostname, and userspace come from flash. Solution: `ubi remove rootfs_data || true` before boot.

### bootconf and bootargs

- `setenv bootconf config-1` - pins FIT configuration node
- `setenv bootargs "console=ttyS0,115200n1 pci=pcie_bus_perf"` - overrides any saved flash-boot args

---

## Linksys E8450 / Belkin RT3200 (mt7622)

**Target file:** `targets/linksys_e8450.yaml`

### Static TFTP vs DHCP

This target uses `tftpboot` instead of `dhcp` for two reasons:

1. **Performance**: `tftpstrategy.py` already injects `serverip`/`ipaddr` before init_commands. DHCP just re-derives the same IPs via BOOTP broadcast - pure overhead on a fixed CI subnet.

2. **Factory partition corruption**: Some Belkin RT3200 units that experienced KOD (Kernel of Death) under 24.10 have corrupted `factory` MTD partitions. The nvmem-cells for MAC address resolve to garbage, causing DHCP to fail (no matching reservation in dnsmasq). The 30s BOOTP timeout triggers the hardware watchdog, resetting the device before our boot completes.

With `tftpboot`, U-Boot replies based on IP, so DHCP reservations are not needed.

### Erased factory MAC (ff:ff:ff:ff:ff:ff)

When a unit's `factory` MTD MAC region is fully erased (all `0xFF`, e.g. after further NAND degradation on a power outage), U-Boot computes `ethaddr = ff:ff:ff:ff:ff:ff`. Unlike a random garbage unicast MAC, the all-ones broadcast address is **rejected** by U-Boot's network stack:

```
Error: ethernet@1b100000 address ff:ff:ff:ff:ff:ff illegal value
TFTP from server 192.168.100.1; our IP address is 192.168.100.2
Loading: *
ARP Retry count exceeded; starting again
```

ARP never resolves and TFTP fails on every attempt. Observed on `belkin_rt3200_1` and `belkin_rt3200_3` after a lab power outage; `belkin_rt3200_2` kept a valid factory MAC and was unaffected.

`tftpstrategy.py` mitigates this by forcing a valid locally-administered unicast MAC into the U-Boot env before the download commands. `ethaddr` is write-once in U-Boot, so a plain `setenv ethaddr` fails with `Can't overwrite "ethaddr"`; the strategy uses the force flag (`setenv -f ethaddr <mac> || true`) to bypass the protection. The MAC is derived deterministically per Labgrid place (`_derive_ethaddr`) so nodes sharing VLAN 200 during mesh tests do not collide. This only affects the TFTP stage; Linux still derives its own MAC after boot. Override with `LG_UBOOT_ETHADDR`, disable with `LG_UBOOT_SET_ETHADDR=0`.

### Required bootargs

```
console=ttyS0,115200n1 swiotlb=512 pci=pcie_bus_perf
```

- `swiotlb=512` - REQUIRED for mt7915e WiFi DMA buffers. Without it, the kernel auto-calculates 0KB and WiFi probe fails with ENOMEM.
- `pci=pcie_bus_perf` - PCIe MPS tuning from filogic baseline.

### Related Documentation

- Factory corruption analysis: `docs/followups/belkin_rt3200_factory_corruption_post_kod.md`
- Layout 1.0 / 24.10 KOD root cause: `pi-lime-packages/docs/followups/belkin_rt3200_layout_1_0_dtb_patch.md`

---

## BananaPi BPi-R4 (mt7988)

**Target file:** `targets/bananapi_bpi-r4.yaml`

### bootconf Override Required

The upstream OpenWrt U-Boot env sets `bootconf` to `config-mt7988a-bananapi-bpi-r4` by default, which does NOT match our generated FIT (`config-1`). Without the override, bootm fails with `Could not find configuration node`.

### UBI Overlay

Same as other filogic/mt7622 targets. Without `ubi remove rootfs_data`, preinit tries to overlay flash on initramfs, `pivot_root` fails, and `lime-config` never runs (device stays at `root@(none):~#`).
