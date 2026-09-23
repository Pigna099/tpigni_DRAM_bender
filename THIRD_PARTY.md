# Sources and notices

The experiment organization, baseline methodology, bisection resolution, and
legacy hammer command schedule follow
[CMU-SAFARI/DejaVu](https://github.com/CMU-SAFARI/DejaVu/tree/aa7a72339184bcdbed41aa5bc71328d97b3ff230),
reference commit `aa7a72339184bcdbed41aa5bc71328d97b3ff230`:

- `characterization/acmin_core.py`: bounds, absolute bisection, measurement sequence.
- `characterization/acmin_runner.py`: physical neighbors, seeded sampling, boundary exclusion.
- `characterization/experiment.py`, `characterization/runner.py`: logging and runner organization.
- `run_acmin_dejavu.py`: default sweep, patterns, seed, repetition count, and baseline recovery.
- `dct/src/programs/{singleside_hammer,doubleside_hammer,init_row,init_rows,read_rows}.cpp`: emitted schedules.

DejaVu: Haocong Luo, İsmail Emir Yüksel, Ataberk Olgun, Nisa Bostanci,
Orhun Ecemiş, Abdullah Giray Yağlıkçı, and Onur Mutlu, “DejaVu: Why You
Should Write to Your DRAM Rows Twice, Carefully,” ISCA 2026.
The referenced snapshot has no top-level license file. This attribution does
not assign a new license to the DejaVu artifact. This project implements the
baseline experiment with the public V2 Python API; it does not vendor DejaVu's
old Python bindings or C++ API.

[DRAM Bender V2](external/dram-bender/README.md) is a pinned Git submodule at
`037f09c5802c08f46ed02265603bb0dd1d8f69e7`. Its
[MIT notice](external/dram-bender/LICENSE), source attribution, documentation,
and bundled driver [license notices](external/dram-bender/xdma/LICENSE) remain
intact. The root `LICENSE` retains the original repository's SAFARI notice.
The nested bitstream submodule is pinned by V2 at
`424dc72ae7b80ff205193747c72d6ec6a26e3ff2`.

DRAM Bender: Ataberk Olgun et al., “DRAM Bender: An Extensible and Versatile
FPGA-Based Infrastructure to Easily Test State-of-the-Art DRAM Chips,”
IEEE TCAD 42(12), 2023. DOI: 10.1109/TCAD.2023.3282172.
