# DRAM read-disturbance characterization

This project runs two experiments on DRAM Bender V2:

- **HCFirst:** find a hammer count that produces a victim-row bit flip, for
  each value in a legacy tAggON sweep.
- **BER:** measure bit errors at one fixed tAggON and hammer count.

The setup is an Alveo U200, channel C1, with a Samsung module and SA0 row
mapping. Experiments run on a DRAM module under test in a fully owned and
controlled FPGA. They do not target host memory or a production system.
This is laboratory characterization; there is no attack.

## Directory structure

```text
main.py                         Command-line entry point and run loop
characterization/
  hcfirst_runner.py             Boundary tests and HCFirst bisection
  ber_runner.py                 Repeated trials at a fixed timing and count
  measurement.py                Initialization, execution, readback, XOR masks
  programs.py                   Explicit FPGA programs
  timing.py                     Duration model derived from emitted programs
  config.py                     Arguments, row selection, and validation
  checkpoint.py                 Manifests, result archives, CSV, and resume
  audit.py                      Workload summary and decoded command traces
external/dram-bender/            Pinned upstream V2 submodule
local/
  u200_samsung_sa0.json          Verified machine and bitstream identifiers
  verify_sa0.py                 Initialization/readback check with SA0 mapping
  verify_interrupted_resume.py  Hardware interruption and recovery check
  pilot/                        V1 pilot results and report
tests/                          Hardware-free tests
docs/methodology.md              Timing details and differences from DejaVu
MACHINE_SETUP.md                 FPGA programming, driver, and recovery notes
results/                        Your experiment runs; ignored by Git
```

`main.py` selects a runner and commits each completed work unit. A work unit
is one victim row, pattern, timing, and hammer mode, with all its repetitions.
Each probe initializes both neighbors, initializes the victim, hammers, and
reads the victim. The four FPGA programs are separate and appear in that
order in `measurement.py`.

## Installation

The FPGA image and XDMA driver must already be configured. Follow
[MACHINE_SETUP.md](MACHINE_SETUP.md) when preparing or recovering a machine.
The Python setup requires Python 3.10 or later, a C++20 compiler, and the
upstream V2 build prerequisites.

```bash
bash setup.sh
source .venv/bin/activate
python -m unittest discover -s tests -v
```

`setup.sh` initializes the pinned submodules and installs both packages into
`.venv`. It does not program the FPGA. The installed `dram-characterize`
command accepts the same arguments as `python main.py`.

For timed runs with automatic result-folder creation, use
`automate_experiment.py`. Put the normal `main.py` command after `--`; the
wrapper creates a new timestamped folder and sends a graceful stop when the
duration expires.

## Counts, timing, and temperature

HC is counted **per aggressor**. Double-sided HC=50,000 emits 50,000 ACTs to
each neighbor, or 100,000 hammer ACTs in total. Initialization and readback
ACTs are excluded from that count.

`--taggon` uses DejaVu's encoded **legacy steps**, not nanoseconds. Values 0
and 1 produce the same schedule. HCFirst uses the tighter of these two bounds:

- 1,000,000 total hammer ACTs per probe (`--total-act-ceiling`).
- 60 ms for the complete hammer program (`--duration-limit-ms`).

At legacy 0, the maximum HC is 1,000,000 single-sided or 500,000 double-sided.
At legacy 2322, the time limit reduces these counts to 860 and 430. The optional
`--count-ceiling` adds a limit per aggressor. BER checks its explicit HC against
the duration limit. To change that limit, use `--duration-limit-ms` or
`--no-duration-limit`. Host time is recorded separately.

To enable the fixed FT200 controller monitor, pass a JSON configuration with
`--temperature-config temperature.warm-50c.json`. Its `dram_target` is also
used as the recorded temperature label, so the target is specified only once.
The monitor uses the vendored FT200 implementation in
`characterization/ft200/ft200.py`, opens device IDs 0 and 1 at addresses 2
and 1, and restores both controllers to `safe_target` when the run exits. The
JSON configuration is resolved once at
startup; it is not reread or rewritten during the run.

The target is saved as `Temperature`; measured controller samples are logged
in the run events. The FT200 dependencies (`minimalmodbus`
and `pyserial`) are imported only when `--temperature-config` is used.

## Preview a run without hardware

```bash
python main.py hcfirst --rows 8,16 --patterns 00 --repetitions 3 \
  --hammer-modes upper,lower,double --taggon 0,5,2322 --plan
```

The JSON output shows selected rows, mapped addresses, workload size, effective
timing, count limits, and decoded command traces. Add
`--audit-file build/hcfirst-plan.json` to save it. Choose a new filename for
each saved audit.

## Run experiments

Start with an initialization/readback control. HC=0 skips hammer ACTs:

```bash
python main.py ber --rows 8,16 --patterns 00,ff,55,aa \
  --hammer-modes upper,lower,double --repetitions 3 \
  --taggon 0 --hc 0 --temperature-label '50 C' --output-dir results/control
```

With FT200 temperature control enabled:

```bash
python main.py ber --rows 8,16 --patterns 00 --repetitions 3 \
  --taggon 0 --hc 0 \
  --temperature-config temperature.warm-50c.json --output-dir results/control-ft200
```

The same experiment, automated for 20 minutes at 50 C:

```bash
python automate_experiment.py \
  --duration-minutes 20 \
  --temperature-config temperature.warm-50c.json \
  -- ber --rows 8,16 --patterns 00 --taggon 0 --hc 50000 --repetitions 100000
```

The extra `--` separates wrapper options from the experiment command. The
wrapper creates a directory such as
`results/20260923-173000_ber_A1V0_-10C`, where `A1V0` identifies the
aggressor/victim pattern, adds `--output-dir`, and sends `SIGTERM` after 20
minutes so FPGA refresh and temperature cleanup can run. When the process
exits, it automatically writes `plot.png` in the same result folder. If
multiple victim patterns are selected, all pattern labels are included in the
folder name.

BER can also sweep HC in one run and one `summary.csv`:

```bash
python main.py ber --rows 8,16 --patterns 00 --taggon 0 \
  --hc-start 10000 --hc-end 50000 --hc-step 10000 --repetitions 3 \
  --temperature-label '50 C' --output-dir results/ber-hc-sweep
```

The BER plot detects multiple `hc_per_aggressor` values and plots median BER
and flipped bits against HC, with a 10th-90th percentile band across the
recorded victim rows and repetitions.

Run independent HCFirst searches over several timings:

```bash
python3 main.py hcfirst --rows 8,16 --patterns 00 \
  --hammer-modes upper,lower,double --repetitions 3 \
  --taggon 0,5,2322 --temperature '50 C' --output-dir results/hcfirst
```

Run BER at one fixed timing and count:

```bash
python3 main.py ber --rows 8,16 --patterns 00 \
  --hammer-modes upper,lower,double --repetitions 3 \
  --taggon 0 --hc 50000 --temperature '50 C' --output-dir results/ber
```

Plot a result CSV or result directory:

```bash
python3 plot_results.py results/20260923-170126_ber_-20C
```

This creates `plot.png` in that result directory with flipped bits and BER
versus repetition. To combine multiple result CSVs, pass them together and
choose an output path with `--output`.

To combine every immediate result directory into four pattern-specific plots
(BER A0V1, BER A1V0, HCFirst A1V1, and HCFirst A0V0):

```bash
python3 plot_all_results.py
```

This writes the aggregate plots under `results/analysis/`, with a `.txt`
sidecar listing the source run IDs. The HCFirst plots use one consistent color
per tested temperature and include a temperature legend.

Each automated run also appends a row to `results/runs_index.csv` and enriches
its manifest with the run ID, round, canonical pattern, target and measured
temperature statistics, launcher answers, and review fields.

The script also writes BER-only plots restricted to HC per aggressor from
300,000 through 500,000:

```text
results/ber_a0v1_hc300k_500k_results.png
results/ber_a1v0_hc300k_500k_results.png
```

`upper` means physical victim + 1; `lower` means physical victim - 1. `double`
alternates between them. Both neighbors are initialized in every mode, with
data complementary to the victim pattern unless `--aggressor-pattern` is set.
`--rows` always takes physical row numbers; mapping happens before program
construction.

For a full sampled run, set `MODULE_ROWS` to the module's actual row count per
bank. Random sampling uses physical rows from `1` through
`MODULE_ROWS - 2`, leaving the final row available as the required upper
neighbor. The code does not infer the module's density.

```bash
: "${MODULE_ROWS:?Set MODULE_ROWS to the actual rows per bank}"
python main.py hcfirst --row-count "$MODULE_ROWS" \
  --module Samsung-C1 --temperature '50 C' --output-dir results/hcfirst-full

python main.py ber --row-count "$MODULE_ROWS" --module Samsung-C1 \
  --taggon 0 --hc 50000 --temperature '50 C' --output-dir results/ber-full
```

| Option | Default |
| --- | --- |
| Endpoint / XDMA channel | `0000:01:00.0` / `0` |
| Bank / rank / mapping | `1` / `0` / `SA0` |
| Sample count / seed | `256` / `123` |
| Victim patterns | `00,ff,55,aa` |
| Hammer mode | `double` |
| Repetitions per unit | `50` |
| HCFirst legacy sweep | `0,5,10,20,50,75,130,258,500,1000,2322` |
| Victim write recovery | `4` fabric cycles |

BER requires both `--taggon` and `--hc`. Use `--verbose` to log every probe,
or `--progress-seconds` to change the progress interval. Run
`python main.py hcfirst --help` or `python main.py ber --help` for all options.

## Results

Each run produces this structure:

```text
results/hcfirst/
  manifest.json
  events.log
  summary.csv
  units/
    r000008_p00_t0_mdouble.npz
    ...
```

The manifest and archives use **format V1** (`schema_version: 1`).
`manifest.json` records the resolved arguments, selected rows, temperature,
one experiment-code fingerprint, and the fixed API/bitstream identifiers.
`events.log` records progress, host elapsed time, and any errors.

`summary.csv` has one row per repetition. Its main fields are:

| Field | Meaning |
| --- | --- |
| `victim`, `pattern`, `steps`, `mode` | Physical victim row, victim byte, legacy tAggON, hammer mode |
| `Temperature` | Externally supplied temperature label |
| `repetition` | Repetition index, starting at zero |
| `hcfirst` | Returned flipping count for HCFirst; empty for a no-flip search |
| `hc_per_aggressor`, `total_hammer_acts` | Selected count and total hammer ACTs |
| `bracket_low`, `bracket_high` | HCFirst's last nonflipping and flipping bounds |
| `search_max` | Largest count allowed in that search |
| `flips`, `tested_bits`, `ber` | Bit errors, bits read, and their ratio |
| `hammer_model_ns`, `host_wall_seconds` | Modeled hammer duration and host elapsed time |
| `status` | Search outcome, fixed-count measurement, or control |

HCFirst returns `bracketed` when it finds a flipping upper bound within 10 of
the nonflipping lower bound. `left_censored` means the minimum count already
flipped; the returned count is that minimum. `right_censored` means the maximum
count did not flip; `hcfirst` is empty in CSV and null in JSON. The tested
maximum is retained. A search ceiling is never reported as an observed HCFirst.

BER trials have status `measured`, or `control` for HC=0. Each trial reads
65,536 victim bits, so 70 flips gives BER `70 / 65536`, approximately
`0.001068`. Zero-error trials are saved too. Transport failures abort the unit
and are not recorded as DRAM errors.

Each `.npz` contains all repetitions for its work unit:

- `metadata`: JSON with results, probe histories, brackets, temperature, and timing.
- `result_masks`: `uint8` XOR masks, shape `[repetitions, 8192]`.
- `probe_masks`: `uint8` XOR masks for every probe, shape `[probes, 8192]`.

Read an archive without pickle:

```python
import json
from pathlib import Path
import numpy as np

path = next(Path("results/hcfirst/units").glob("*.npz"))
with np.load(path, allow_pickle=False) as archive:
    metadata = json.loads(archive["metadata"].item())
    print(metadata["results"][0])
    mask = archive["result_masks"][0]
    print("Flipped bits:", np.unpackbits(mask).sum())
```

The masks follow received byte order; bit 0 is the least significant bit of
each byte. HCFirst saves the mask from its returned flipping count, or from
the censoring bound when the search is censored.

## Stop and resume

Press Ctrl-C to stop. Normal exit, SIGINT, SIGTERM, and handled errors reset
the FPGA and enable refresh. Refresh is disabled during measurement; the
60 ms limit applies to each hammer program, not the entire session.

Rerun the same command with the same output directory to resume. Completed
units are skipped; an interrupted unit reruns all its repetitions. The `.npz`
archives are the saved data, and CSV is rebuilt from them. A completed resume
does not open the FPGA. Configuration, temperature, or code changes require
a new output directory. A lock prevents concurrent writers to a run.

## Validation and references

[The V1 pilot](local/pilot/README.md) records HCFirst and BER measurements,
HC=0 controls, and interruption recovery on the FPGA. To rerun the recovery
check in a fresh directory:

```bash
python local/verify_interrupted_resume.py --output-dir results/recovery-check
```

See [methodology](docs/methodology.md) for the emitted schedule, exact timing,
and intentional differences from DejaVu. [THIRD_PARTY.md](THIRD_PARTY.md)
records source attribution and upstream licenses.
