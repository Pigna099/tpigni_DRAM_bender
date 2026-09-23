# Measurement semantics and intentional differences

These experiments characterize a DRAM module on a dedicated U200/C1 test
FPGA. Temperature is controlled externally and supplied at startup with
`--temperature '50 C'` (`--temperature-label` is an alias). The value defaults
to `unknown` and is saved verbatim as `Temperature` in the CSV, manifest, and
archive unit/repetition metadata. It is never sensor evidence; its
`temperature_source` identifies it as externally supplied. The board API does not
identify the currently programmed image: manifests copy the fixed identifiers
and file hash from `local/u200_samsung_sa0.json` into `recorded_setup`.
This record describes the verified setup; experiment startup does not inspect
the loaded image, rehash the bitfile, or revalidate the installed API.

## Programs and coordinates

`characterization/programs.py` contains the flat FPGA programs. All address
mapping happens before building a program. In DejaVu's convention, **upper
means physical victim + 1** and **lower means physical victim - 1**. SA0 maps
physical `p` to `p ^ (((p >> 3) & 1) * 6)`. For physical victim 8, the logical
victim/upper/lower addresses are 14/15/7. The supplied mapping is preserved;
this port does not independently discover adjacency.

Each probe submits four independent programs in order:

1. Initialize both neighbors, upper then lower, with complementary bytes by
   default. The scratchpad loop and command timing follow DejaVu `init_rows`.
2. Write the victim once using DejaVu `init_row_long_tWR`. This is the baseline
   initialization, with no overwrite or same-write second pass.
3. Hammer the selected neighbor(s), upper first when alternating.
4. Read all 128 cachelines of the victim through V2's framed readback protocol.

Initialization is identical for all hammer modes. This deliberately corrects
the older lower-single-sided path, which initialized the victim before the
neighbors. Aggressor patterns can be overridden by one explicit byte.

The configurable victim recovery parameter defaults to four **fabric cycles**.
It is the extra wait after the last WR command word and its following tCCD
NOP. At that default, last WR to PRE is 36 ns (six fabric cycles total), not
24 ns. Aggressor initialization retains DejaVu's 24 ns last-WR-to-PRE schedule.
Readback uses V2's ordinary full-row schedule and automatic framing metadata.

## Legacy hammer timing

The user parameter `--taggon` (`--tAggON`) is an encoded **legacy step count**,
not nanoseconds. Values 0 and 1 emit the same schedule. A value `s > 1` inserts
`SLEEP(5 * (s - 1))`. One U200 fabric cycle is 6 ns; each command slot is
1.5 ns. The decoded schedule, including the six-cycle branch, gives:

| Quantity | U200 timing |
| --- | --- |
| Extra interval E | `30 * max(s - 1, 0)` ns |
| ACT to PRE, including last ACT | `37.5 + E` ns |
| PRE to ACT | 16.5 ns |
| Single-sided loop iteration | `54 + E` ns |
| Double-sided iteration | `108 + 2*E` ns |
| Complete nonzero-HC program | `48 + E + HC * iteration_ns` |
| HC=0 control hammer program | 48 ns; no ACT |

These are V2 VM timing-model values, not oscilloscope measurements. The
implementation derives its duration coefficients from emitted HC=0/1/2/3
programs and checks the affine schedule. Tests independently check every
ACT/PRE slot, row, interval, and total modeled duration, including HC=1.

The original hammer loop ended with its last row open. This port deliberately
finishes that aggressor's full intended open interval, issues PRE, and waits
three fabric cycles before END. It preserves the original initial extra wait
and all internal loop commands. The final interval and setup overhead both
count against the ceiling. The search takes the minimum of the count allowed
by the complete emitted program's duration, the total-ACT budget divided by
the number of aggressors, and any optional per-aggressor count ceiling. The
default **total-ACT budget is 1,000,000**. At legacy 0 or 1 this permits
500,000 double-sided HC or 1,000,000 single-sided HC, both modeling 54.000048 ms.
For longer intervals, the default **60 ms** ceiling further reduces HC; at
legacy 2322, the limits are 430 double-sided or 860 single-sided HC, both
modeling 59.997918 ms. This replaces DejaVu's approximate time formula.

HC is **per aggressor**. Double-sided HC=N gives N ACTs to each neighbor and
2N total hammer ACTs. Initialization/readback ACTs are not included in this
count. HC=0 emits no hammer ACT and still initializes and reads the victim.

## Search and BER

Every probe starts with fresh neighbor and victim initialization. HCFirst tests
both configured bounds (one probe when equal), then bisects to an absolute
bracket width of at most 10. Every repetition starts a new independent search.
`main.py hcfirst` selects `characterization/hcfirst_runner.py`; `main.py ber`
selects `characterization/ber_runner.py`. Each runner contains its experiment
logic and yields one complete repetition. The top-level work loop records
progress and commits the unit after all repetitions finish. Both runners use
`Measurement.probe()` for fresh hardware probes and bit-mask accounting.

- `bracketed`: `bracket_low` was observed nonflipping and `bracket_high` was
  observed flipping; `hcfirst` is the tested flipping high and its mask is saved.
- `left_censored`: the minimum tested count already flipped. `hcfirst` is that
  observed count, `bracket_low` is null, and `bracket_high` is the minimum.
- `right_censored`: the maximum did not flip. `hcfirst` and `bracket_high` are
  null, `bracket_low` is the tested maximum, and its zero mask is retained.

Both boundary observations are retained even if the lower bound flips. If it
flips while the upper does not, `nonmonotonic_bounds` is true. As with DejaVu,
bisection assumes monotonicity within one search; stochastic DRAM behavior
does not make the result a proven physical threshold. Probe histories and
independent repetitions remain available for analysis.

BER runs exactly one fixed legacy timing and HC per invocation. Each trial
retains its XOR mask, popcount, 65,536 tested victim bits, and
`BER = flips / 65536`, including zero-error trials. A transport timeout, short
read, or synchronization/protocol failure aborts the unit without recording
a DRAM error result.

## Time and hardware lifecycle

The default 60 ms ceiling covers the **hammer program only**. Initialization,
readback, host program construction, DMA transfers, logging, and checkpoint
work are outside it. Refresh is disabled for the measurement session; the
complete refresh-disabled wall interval can be much longer than 60 ms.
Each trial/search records host wall time separately from modeled hammer time;
the log records session wall time. Victims are rewritten for every probe.

Normal exit, Python exceptions, SIGINT, and SIGTERM reset the FPGA and enable
refresh before releasing the endpoint. An uncatchable SIGKILL or host power
loss cannot run cleanup. A restart begins with a full reset; machine recovery
instructions remain in `MACHINE_SETUP.md`.

## Result durability

A unit is one physical victim/pattern/legacy timing/hammer mode with all
requested repetitions. `manifest.json` records its resolved configuration,
selected rows, `Temperature`, one experiment-code SHA-256 fingerprint, and the
API/bitstream identifiers from the fixed setup record. The fingerprint covers
`characterization/*.py`, `main.py`, and `pyproject.toml`; it prevents combining
results from different experiment implementations on resume. Git, package-path,
environment-version, and bitstream validation are outside the run path.
An advisory exclusive writer lock covers the entire run. The manifest is
created once; resume refuses changes to configuration, schema, experiment code,
or recorded setup. Output path and logging verbosity are not experimental
parameters. Manifests and result archives use format V1 (`schema_version: 1`).

Completed units are compressed `.npz` archives written to a temporary file,
fsynced, then atomically linked into place without replacing existing units.
Published archives and CSV files are readable by all users. The containing
directory is fsynced before CSV is updated. Archive metadata
is bound to the manifest's stable configuration/provenance digest. All arrays
are readable using `allow_pickle=False`:

- `metadata`: a scalar Unicode JSON string with unit identity, repetition
  results, `Temperature` and its source, search brackets, every probe's
  measurements, timing, and mask indices. Each repetition includes `Temperature`.
- `result_masks`: uint8 array `[repetition, 8192]`, containing the mask at the
  returned flipping HC or the censoring bound; for BER, the trial's mask.
- `probe_masks`: uint8 array `[all_probes_in_unit, 8192]`; metadata's
  `probe_start + selected_probe_index` identifies each selected mask.

Byte order follows the received cacheline/word/byte stream on this little-endian
host; within a byte, bit 0 is the LSB. No object arrays or pickled Python objects
are stored. `summary.csv` has one row per independent result and is a derivative
for inspection. On resume it is rebuilt by streaming validated archives,
including censored and zero-error units. Missing or interrupted units rerun in
full; committed units are skipped. A corrupt archive is rejected explicitly.
