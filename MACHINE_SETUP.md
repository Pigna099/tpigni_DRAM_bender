# DRAM Bender V2 on safari-fpga62

Verified on 2026-09-23: the V2 Python and C++ APIs work with this machine's
U200 and Samsung module. No reboot was needed. The upstream API source is
unchanged; machine configuration and helpers are in `local/`.

## Use it now

From `/home/hluo/dram-rd-test`, run applications as your normal user:

```bash
source .venv/bin/activate
python local/verify_sa0.py
```

This checks five patterns, including distinct bank/row/word tags. It overwrites
physical rows 0..31 in all 16 banks of rank 0, checks every returned word, then
resets the execution state and enables auto-refresh. Expected result: five
`PASS` lines, 20 MiB checked, zero mismatches. Use `--dry-run` to print the row
mapping without opening the board. Use `--output NEW_FILE.json` to save a report.

For a single-row example:

```bash
python external/dram-bender/examples/read_write.py --board u200 --pci-bdf 0000:01:00.0 \
    --xdma-channel 0 --bank 0 --row 0 --pattern 0xDEADBEEF
```

The example's `--row` is a logical DRAM address. For Samsung physical row
coordinates, apply SA0 explicitly before passing a row to a program:

```python
from drambender import builtin_programs
from drambender.api import DDR4Target, open_board
from drambender.rows import Row

target = DDR4Target(rank=0)
programs = builtin_programs.configure(target=target)
row = Row(physical_id=8, row_mapping="SA0")
assert row.logical_id == 14
# Use row.logical_id with programs.write_row(...) / programs.read_row(...).
# Open using: open_board(target, pci_bdf="0000:01:00.0", xdma_channel=0)
```

SA0 is the supplied mapping `physical ^ (((physical >> 3) & 1) * 0x6)`.
These tests exercise mapped addressing and check for aliasing in the tested
set; they do not independently establish physical adjacency or characterize
RowHammer vulnerability. Validation covers rank 0, not the entire DIMM.

## Installed configuration

| Item | Value |
| --- | --- |
| Upstream | [CMU-SAFARI/DRAM-Bender, branch v2](https://github.com/CMU-SAFARI/DRAM-Bender/tree/v2) |
| Source commit | `037f09c5802c08f46ed02265603bb0dd1d8f69e7` |
| Bitstream submodule commit | `424dc72ae7b80ff205193747c72d6ec6a26e3ff2` |
| Bitstream | `external/dram-bender/hw/prebuilt/XCU200/XCU200_C1_UDIMM_ANYRANK_x8.bit` |
| PCI endpoint / XDMA channel | `0000:01:00.0` / `0` |
| JTAG serial | `21291036400NA` |
| DIMM configuration | User-selected C1 UDIMM x8 bitstream; Samsung SA0 mapping |
| Memory target | Rank 0, 128 cachelines/row, stride 8, 16 uint32 words/cacheline; 8192 bytes/row |
| Kernel | `6.8.0-138-generic` |
| Loaded driver | This checkout's `drambender_xdma`, streaming credits enabled |
| Access | Existing `smc_user` group; DMA nodes remain mode `0660` |
| Python | `.venv`, Python 3.12.3, editable `drambender` 0.1.0 package from V2 |
| C++ | C++20, GCC 13.3.0; original library retained at `build/internal-board-tests/libDRAMBender.a` |
| FPGA programming tool | Installed Vivado Lab 2022.1 successfully programmed the released image |

Bitstream SHA-256:
`d329dfc346ae7b42b35215c5c2cd9385670eab3dac5057f9da285775a129255f`.
The package version is `0.1.0` even on the V2 branch; use the Git commit to
identify this release. Machine-readable settings are in
[`local/u200_samsung_sa0.json`](local/u200_samsung_sa0.json).

## Verification results

| Check | Result |
| --- | --- |
| FPGA DDR4 calibration | `CAL PASS`; one rank and eight data bytes reported by MIG |
| Python dependencies | `pip check` passed |
| Upstream host suite | 11/11 CTests passed |
| Upstream single-row example | 2048 uint32 words matched `0xDEADBEEF` |
| `u200-smoke` | C++ and Python read/write and full-reset recovery passed |
| `u200-correctness --rw-rows 4096` | C++ and Python each verified 33,554,432 bytes in bank 0; zero mismatches |
| Local SA0 test | Five patterns × 16 banks × 32 rows; 20,971,520 bytes verified; zero mismatches |

The correctness profile also ran its VM/hardware timing diagnostic; it is
non-gating and is not a timing calibration. Detailed logs and JSON results are
in [`build/machine-check/`](build/machine-check/). The durable summary is
[`local/verification-2026-09-23.json`](local/verification-2026-09-23.json).

An initial version of the local SA0 test reached its 120-second limit while
JIT-compiling 512 distinct tuple patterns, after four hardware patterns had
passed. The final test uses the underlying Python builder for the one-off
address tags, while retaining JIT use for reusable patterns; all five phases
then passed in 2.39 seconds. The initial log/report are retained separately.

To rerun the upstream tests:

```bash
python external/dram-bender/internal_tests/run_tests.py host
python external/dram-bender/internal_tests/run_tests.py board --profile u200-smoke \
    --pci-bdf 0000:01:00.0 --xdma-channel 0
python external/dram-bender/internal_tests/run_tests.py board --profile u200-correctness \
    --pci-bdf 0000:01:00.0 --xdma-channel 0 --rw-rows 4096
```

Run board tests serially: the driver grants exclusive ownership of the endpoint.
See the upstream [Python tutorial](external/dram-bender/docs/tutorials/writing-programs.md) and
[C++ API reference](external/dram-bender/docs/reference/cpp-api.md) for experiment development.

## After a reboot, power cycle, or kernel upgrade

The driver was loaded from this checkout. There is no automatic module-loading
or kernel-rebuild service. JTAG programming is volatile and does not update
the board's flash. A power cycle or the legacy `/etc/smc_infra` setup can restore
an older image/driver, which does not use the V2 protocol.

If the V2 image is still programmed, load the matching driver:

```bash
sudo /usr/bin/make -f local/Makefile load-driver
```

To restore the complete tested setup, with no active FPGA applications:

```bash
mkdir -p build/machine-check
./external/dram-bender/xdma/build_driver.sh -j 4
sudo /usr/bin/make -f local/Makefile program-hardware
sudo /usr/bin/make -f local/Makefile load-driver
.venv/bin/python local/verify_sa0.py
```

The administration helper uses this account's existing sudo permission for
`/usr/bin/make`. It checks the host, refuses an in-use endpoint or multiple
XDMA endpoints, unloads the old driver, removes only this FPGA PCI function,
programs the specified JTAG serial, and rescans the upstream bridge. It neither
reboots the host nor edits the legacy infrastructure. Applications use ordinary
user permissions. After a kernel upgrade, install matching kernel headers and
rebuild the driver before loading it.

## Characterization project relocation

The verified upstream source now lives in the pinned `external/dram-bender`
submodule. The bitstream and built driver moved with it; the existing
`build/machine-check/` evidence and original verification JSON are preserved.
Their paths describe the original verification layout. The editable Python
package was reinstalled from its new path. New upstream CMake builds live
under `external/dram-bender/build/` to avoid reusing stale CMake paths.
