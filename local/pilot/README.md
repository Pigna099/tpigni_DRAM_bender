# V1 hardware pilot

Run on 2026-09-23 using the owned U200/C1 FPGA and Samsung SA0 module.
Endpoint: `0000:01:00.0`, XDMA channel 0, bank 1, rank 0.
**Temperature: unknown.** Temperature was externally controlled and was not
measured by the code. All manifests and archives use `schema_version: 1`.

## HCFirst and BER

These measurements use physical victim row 8, victim pattern `00`, aggressor
pattern `ff`, and legacy tAggON=0. Counts are per aggressor. Each point has
three independent repetitions. BER uses a separate fixed-count run at
HC=50,000, reading 65,536 bits per trial.

| Mode | HCFirst repetitions | Median HCFirst | BER flip counts | Aggregate BER |
| --- | --- | ---: | --- | ---: |
| Upper single-sided | 187,507; 183,600; 187,507 | 187,507 | 0; 0; 0 | 0 |
| Lower single-sided | 265,625; 257,812; 281,195 | 265,625 | 0; 0; 0 | 0 |
| Alternating double-sided | 27,350; 27,350; 27,350 | 27,350 | 74; 75; 75 | 0.001139322916666667 |

The searches used the default 1,000,000 total-ACT and 60 ms limits. At legacy
0, the maximum count was 1,000,000 single-sided or 500,000 double-sided;
both programs model 54.000048 ms. All returned brackets have width at most 10.

At legacy 2322, all nine searches were right-censored. No flips occurred at
the tested maximum of 860 single-sided or 430 double-sided HC, each modeling
59.997918 ms. Their HCFirst fields are null. These limits are not measured
HCFirst values.

## Checks

- 32 hardware-free tests passed, including timing/count traces, bisection,
  masks, temperature fields, checkpoint recovery, and result permissions.
- 48 HC=0 controls passed with zero flips: rows 8 and 16, all four patterns,
  all three modes, and two repetitions.
- 18 HCFirst searches produced nine bracketed and nine right-censored results.
- 9 fixed-count BER trials completed; results are in the table above.
- The interruption check sent SIGTERM during the second unit. Refresh was
  restored, the first archive stayed byte-identical, and the interrupted unit
  reran. CSV was rebuilt after deletion, with 100 unique result rows.
- All 35 archives, 175 results, and 343 saved probes were validated without
  pickle. Rebuilding CSV preserved manifest and archive hashes. Results retain
  their masks, temperature labels, and count/duration bounds.
- Published manifests, CSV files, and archives are readable by all users.

## Files

- [Commands](commands.json): exact invocations; use fresh output directories to rerun.
- [Report](report.json): configuration identifiers, code fingerprint, and checks.
- [Audit](audit.json): decoded programs for legacy 0, 1, 5, and 2322 in all modes.
- [Control CSV](control/summary.csv), [HCFirst CSV](hcfirst/summary.csv), and
  [BER CSV](ber/summary.csv). Each run's `units/` directory contains the masks.
- [Recovery report](recovery/recovery_report.json): interruption and resume evidence.

Full-run commands and the result format are in the [project README](../../README.md).
