"""Locked runs, immutable manifests, atomic authoritative NPZ work units."""

from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import tempfile

import numpy as np

from .config import ROOT
from .measurement import flip_count

SCHEMA_VERSION = 1
CSV_FIELDS = ["unit_id", "experiment", "module", "Temperature", "temperature_label", "temperature_source",
              "victim", "pattern", "aggressor_pattern", "steps", "mode", "bank", "rank", "mapping",
              "repetition", "status", "hcfirst", "hc_per_aggressor", "total_hammer_acts",
              "bracket_low", "bracket_high", "search_min", "search_max", "search_max_total_acts", "resolution",
              "flips", "tested_bits", "ber", "hammer_model_ns", "total_probe_hammer_ns",
              "host_wall_seconds", "probe_count", "nonmonotonic_bounds",
              "dram_temperature", "liquid_temperature"]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(path):
    with Path(path).open("rb") as f:
        h = hashlib.sha256()
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
        return h.hexdigest()


def provenance():
    """Identify experiment code and record the fixed setup; no runtime setup audit."""
    files = sorted((ROOT / "characterization").glob("*.py")) + [ROOT / "main.py", ROOT / "pyproject.toml"]
    hashes = {str(p.relative_to(ROOT)): sha256(p) for p in files}
    setup = json.loads((ROOT / "local/u200_samsung_sa0.json").read_text())
    return {"experiment_sha256": hashlib.sha256(canonical(hashes).encode()).hexdigest(),
            "recorded_setup": {k: setup[k] for k in
                               ("source_commit", "bitstream_commit", "bitstream", "bitstream_sha256")}}


def work_units(config):
    for victim in config.rows:
        for pattern in config.patterns:
            for steps in config.taggon_steps:
                for mode in config.modes:
                    if config.experiment == "ber" and len(config.hc_values) > 1:
                        for hc in config.hc_values:
                            yield {"victim": victim, "pattern": pattern, "steps": steps,
                                   "mode": mode, "hc": hc}
                    else:
                        yield {"victim": victim, "pattern": pattern, "steps": steps, "mode": mode}


def unit_id(unit):
    suffix = f"_h{unit['hc']}" if "hc" in unit else ""
    return f"r{unit['victim']:06d}_p{unit['pattern']:02x}_t{unit['steps']}_m{unit['mode']}{suffix}"


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def atomic_file(path, *, replace=False):
    """Publish a fully fsynced file on the same filesystem; no partial units."""
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".pending-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            yield f
            f.flush()
            os.fchmod(f.fileno(), 0o644)
            os.fsync(f.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)  # atomic, refuses to overwrite a committed unit
            temporary.unlink()
        fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class RunStore:
    def __init__(self, directory, config):
        self.directory = Path(directory)
        self.config = config
        self.expected = {unit_id(u): u for u in work_units(config)}
        self.completed = set()
        self.lock = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = (self.directory / ".writer.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError(f"another writer owns {self.directory}") from None
        try:
            path = self.directory / "manifest.json"
            stable = {"schema_version": SCHEMA_VERSION, "configuration": self.config.as_dict(),
                      "Temperature": self.config.temperature_label, "provenance": provenance()}
            self.manifest_id = hashlib.sha256(canonical(stable).encode()).hexdigest()
            if path.exists():
                manifest = json.loads(path.read_text())
                if any(manifest.get(k) != v for k, v in stable.items()):
                    raise ValueError("incompatible resume: configuration, schema, experiment code, or recorded setup changed")
            else:
                existing = [p for p in self.directory.iterdir() if p.name != ".writer.lock" and not p.name.startswith(".pending-")]
                if existing:
                    raise ValueError("run directory has existing data without a manifest")
                manifest = {**stable, "created_at_utc": datetime.now(timezone.utc).isoformat(),
                            "host": platform.node(),
                            "mask_format": "uint8 XOR bytes; cacheline/word/byte readback order; LSB is bit 0"}
                with atomic_file(path) as f:
                    f.write((json.dumps(manifest, indent=2, allow_nan=False) + "\n").encode())
                path.chmod(0o444)
            self.units_dir = self.directory / "units"
            self.units_dir.mkdir(exist_ok=True)
            self.rebuild_csv()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *exc):
        if self.lock is not None and not self.lock.closed:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()

    def read_archive(self, path):
        with np.load(path, allow_pickle=False) as archive:
            if set(archive.files) != {"metadata", "result_masks", "probe_masks"}:
                raise ValueError(f"invalid archive fields: {path}")
            metadata = json.loads(str(archive["metadata"].item()))
            masks, probe_masks = archive["result_masks"], archive["probe_masks"]
        key = path.stem
        if (metadata.get("schema_version") != SCHEMA_VERSION or metadata.get("unit_id") != key
                or metadata.get("manifest_id") != self.manifest_id
                or key not in self.expected or metadata.get("unit") != self.expected[key]):
            raise ValueError(f"archive unit mismatch: {path}")
        results = metadata["results"]
        for record in [metadata, *results]:
            if (record.get("Temperature") != self.config.temperature_label
                    or record.get("temperature_source") != self.config.temperature_source):
                raise ValueError(f"archive temperature mismatch: {path}")
        nbytes = self.config.cachelines_per_row * self.config.words_per_cacheline * 4
        nprobes = sum(len(r["probes"]) for r in results)
        if (len(results) != self.config.repetitions or masks.dtype != np.uint8 or probe_masks.dtype != np.uint8
                or masks.shape != (self.config.repetitions, nbytes) or probe_masks.shape != (nprobes, nbytes)):
            raise ValueError(f"archive mask shape or repetition mismatch: {path}")
        offset = 0
        for i, result in enumerate(results):
            if result["repetition"] != i or result["probe_start"] != offset or not result["probes"]:
                raise ValueError(f"archive probe indexing mismatch: {path}")
            selected = result["selected_probe_index"]
            if not 0 <= selected < len(result["probes"]) or not np.array_equal(masks[i], probe_masks[offset + selected]):
                raise ValueError(f"archive selected mask mismatch: {path}")
            for record, mask in [(result, masks[i]), *zip(result["probes"], probe_masks[offset:offset+len(result["probes"])])]:
                if (record["tested_bits"] != nbytes * 8 or record["flips"] != flip_count(mask)
                        or record["ber"] != record["flips"] / (nbytes * 8)):
                    raise ValueError(f"archive bit accounting mismatch: {path}")
            offset += len(result["probes"])
        return metadata

    def csv_rows(self, metadata):
        unit = metadata["unit"]
        for result in metadata["results"]:
            yield {**result, **unit, "unit_id": metadata["unit_id"], "experiment": self.config.experiment,
                   "module": self.config.module, "temperature_label": self.config.temperature_label,
                   "temperature_source": self.config.temperature_source, "pattern": f"{unit['pattern']:02x}",
                   "aggressor_pattern": f"{self.config.aggressor_byte(unit['pattern']):02x}",
                   "bank": self.config.bank, "rank": self.config.rank, "mapping": self.config.mapping,
                   "probe_count": len(result["probes"])}

    def rebuild_csv(self):
        self.completed.clear()
        # CSV is always reconstructed from authoritative units on resume.
        # Stream one unit at a time: full sweeps can contain millions of probes.
        with atomic_file(self.directory / "summary.csv", replace=True) as f:
            text = io.TextIOWrapper(f, encoding="utf-8", newline="")
            try:
                writer = csv.DictWriter(text, fieldnames=CSV_FIELDS, extrasaction="ignore")
                writer.writeheader()
                for path in sorted(self.units_dir.glob("*.npz")):
                    metadata = self.read_archive(path)
                    self.completed.add(path.stem)
                    writer.writerows(self.csv_rows(metadata))
                text.flush()
            finally:
                text.detach()

    def commit(self, unit, results, host_wall_seconds):
        key = unit_id(unit)
        if key not in self.expected or self.expected[key] != unit or len(results) != self.config.repetitions:
            raise ValueError("cannot commit an unknown or incomplete work unit")
        rows, probes, offset = [], [], 0
        for i, result in enumerate(results):
            rows.append({**result.metadata, "repetition": i, "probe_start": offset,
                         "Temperature": self.config.temperature_label,
                         "temperature_source": self.config.temperature_source,
                         "probes": [p.metadata() for p in result.probes]})
            probes.extend(p.mask for p in result.probes)
            offset += len(result.probes)
        metadata = {"schema_version": SCHEMA_VERSION, "manifest_id": self.manifest_id,
                    "unit_id": key, "unit": unit, "results": rows,
                    "Temperature": self.config.temperature_label,
                    "temperature_source": self.config.temperature_source,
                    "unit_host_wall_seconds": host_wall_seconds,
                    "committed_at_utc": datetime.now(timezone.utc).isoformat()}
        path = self.units_dir / f"{key}.npz"
        with atomic_file(path) as f:
            np.savez_compressed(f, metadata=np.asarray(canonical(metadata)),
                                result_masks=np.stack([r.mask for r in results]), probe_masks=np.stack(probes))
        # This ordering is deliberate: a crash here leaves a complete unit and
        # stale CSV, which is repaired before any hardware is opened on resume.
        self.completed.add(key)
        rows = list(self.csv_rows(metadata))
        with (self.directory / "summary.csv").open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
