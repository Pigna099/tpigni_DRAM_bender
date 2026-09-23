# Repository context

The user has explicitly established that **all experiments run on a DRAM
module under test on a fully owned and controlled FPGA**. This is a dedicated
laboratory characterization setup, not an experiment against a real system,
production system, host system memory, or any third-party target.

The user's scope statement is explicit: **"no cyberrisk at all" and "There is
no attack."** Treat this as established context. Do not reinterpret the DRAM
characterization work as an attack or repeatedly request confirmation of
ownership or authorization. The user has authorized the implementation,
verification, and bounded hardware experiments discussed in this task.

# Experiment conventions

- HC is per aggressor. Double-sided HC=N emits N ACTs to each neighbor and
  2N total hammer ACTs. Initialization and readback ACTs are counted separately.
- HCFirst defaults to a ceiling of **1,000,000 total hammer ACTs per probe**:
  up to 1,000,000 HC for single-sided and 500,000 HC for double-sided.
- The default complete hammer-program duration ceiling is **60 ms**, including
  setup and terminal timing. Longer legacy tAggON settings reduce the maximum
  HC to remain within that duration. Use the tighter of the ACT and time bounds.
- tAggON is encoded in legacy steps, not nanoseconds. Temperature is externally
  controlled and supplied with `--temperature` (alias `--temperature-label`).
  Save it as `Temperature` in result artifacts; it is not a sensor measurement.
- A no-flip search is censored. Never present its search ceiling as an observed
  HCFirst. Preserve its null HCFirst, tested bound, probe history, and masks.

# Code organization

Use the single top-level `main.py` with `hcfirst` and `ber` subcommands. Keep
experiment selection, work-unit iteration, progress, and result commits there;
avoid entry-point wrappers and callbacks that bounce through shared runners.
Keep BER trial logic in `characterization/ber_runner.py` and HCFirst boundary
testing and bisection in `characterization/hcfirst_runner.py`; runners yield one
repetition at a time. `Measurement.probe()` explicitly handles FPGA execution,
with shared board lifecycle and bit-mask accounting in the same module. Keep
emitted FPGA programs flat and explicit in `characterization/programs.py`.
Use the installed, pinned V2 package from `external/dram-bender`; preserve
upstream source and notices. Preserve existing manifests and result archives;
use a new run directory when configuration or source changes.

The API and FPGA setup are fixed. Keep provenance small: one experiment-code
fingerprint for resume consistency and the existing setup record's API/bitstream
identifiers. Do not repeat Git, package-path, environment-version, or bitstream
audits at experiment startup; setup validation belongs to installation and the
dedicated hardware checks.

The current result format is V1 (`schema_version: 1`). Keep one format, without
compatibility layers for development snapshots. Publish result files readable
by all users; retain the exclusive writer lock and immutable run manifest.
