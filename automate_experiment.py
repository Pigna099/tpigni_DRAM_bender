#!/usr/bin/env python3
"""Run one characterization command in a new timestamped results directory."""

import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import re
import signal
import subprocess
import sys

from characterization.temperature import LIQUID_OFF_TARGET, open_ft200_pair


def option_value(command_args, option, default=None):
    value = default
    for index, argument in enumerate(command_args):
        if argument == option:
            if index + 1 >= len(command_args):
                raise ValueError(f"{option} requires a value")
            value = command_args[index + 1]
        prefix = f"{option}="
        if argument.startswith(prefix):
            value = argument[len(prefix):]
    return value


def parse_byte(value):
    parsed = int(value.strip(), 16)
    if not 0 <= parsed <= 0xFF:
        raise ValueError
    return parsed


def pattern_folder_name(command_args):
    patterns = option_value(command_args, "--patterns", "00,ff,55,aa")
    aggressor = option_value(command_args, "--aggressor-pattern")
    try:
        victim_patterns = [parse_byte(value) for value in patterns.split(",")]
        aggressor_pattern = None if aggressor is None else parse_byte(aggressor)
    except ValueError as error:
        raise ValueError("patterns must be hexadecimal bytes") from error

    labels = []
    for victim_pattern in victim_patterns:
        used_aggressor = (
            victim_pattern ^ 0xFF
            if aggressor_pattern is None
            else aggressor_pattern
        )
        if victim_pattern in (0, 0xFF) and used_aggressor in (0, 0xFF):
            label = f"a{int(used_aggressor == 0xFF)}v{int(victim_pattern == 0xFF)}"
        else:
            label = f"v{victim_pattern:02x}a{used_aggressor:02x}"
        labels.append(label)
    return "-".join(dict.fromkeys(labels))


def temperature_folder_name(temperature, experiment, pattern, timestamp):
    label = temperature.strip().replace(" ", "").replace("/", "-")
    if label == "vanilla":
        label = "warm"
    else:
        target = float(label.replace("C", ""))
        if target == 50:
            label = "warm"
        elif target == 0:
            label = "0C"
        elif target in (-25, -50):
            label = f"{target:g}C"
        elif target < 0:
            label = f"coldest-{abs(target):g}C"
        else:
            label = f"{target:g}C"
    return f"{timestamp}_{experiment}_{pattern}_{label}"


def folder_suffixes(command_args, redo):
    taggon = option_value(command_args, "--taggon")
    suffixes = []
    if taggon is not None and taggon not in {"0", "0,0"}:
        suffixes.append(f"taggon{taggon.replace(',', '-')}")
    if redo:
        suffixes.append("redo")
    return "".join(f"_{suffix}" for suffix in suffixes)

def round_name(run_id):
    match = re.match(r"^(R[0-9]+|B|T|X)(?:-|$)", run_id, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def allocate_run_id(results_directory):
    used = []
    for path in results_directory.iterdir():
        match = re.match(r".*_ADHOC-(\d+)_", path.name)
        if match:
            used.append(int(match.group(1)))
    return f"ADHOC-{max(used, default=0) + 1:02d}"


def measured_temperatures(summary):
    values = []
    if summary.is_file():
        with summary.open(newline="") as stream:
            for row in csv.DictReader(stream):
                if row.get("dram_temperature", ""):
                    values.append(float(row["dram_temperature"]))
    if not values:
        return None, None, None
    return sum(values) / len(values), min(values), max(values)


def target_temperature(label):
    if label == "vanilla":
        return None
    return float(label.replace(" C", ""))


def ensure_runs_index(results_directory):
    index = results_directory / "runs_index.csv"
    fields = ["run_id", "folder", "test", "pattern", "target_temp_c",
              "measured_temp_mean_c", "temp_spread_c", "start_local", "status",
              "accepted", "notes"]
    existing = set()
    if index.is_file():
        with index.open(newline="") as stream:
            existing = {row["folder"] for row in csv.DictReader(stream)}
    legacy = sorted(path for path in results_directory.iterdir()
                    if path.is_dir() and path.name not in existing
                    and (path / "manifest.json").is_file()
                    and not re.match(r"^\d{8}-\d{6}_[^_]+_[^_]+_[^_]+_", path.name))
    if not legacy and index.is_file():
        return
    with index.open("a", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        if index.stat().st_size == 0:
            writer.writeheader()
        for number, path in enumerate(legacy, 1):
            manifest = json.loads((path / "manifest.json").read_text())
            config = manifest.get("configuration", {})
            target = manifest.get("Temperature", config.get("temperature_label", ""))
            mean, minimum, maximum = measured_temperatures(path / "summary.csv")
            pattern = pattern_folder_name([
                "--patterns", config.get("patterns", ["00"])[0],
                "--aggressor-pattern", config.get("aggressor_pattern", "ff"),
            ])
            writer.writerow({
                "run_id": f"LEGACY-{number}",
                "folder": path.name,
                "test": config.get("experiment", "unknown"),
                "pattern": pattern,
                "target_temp_c": target,
                "measured_temp_mean_c": mean,
                "temp_spread_c": None if mean is None else maximum - minimum,
                "start_local": path.name[:15],
                "status": "complete",
                "accepted": manifest.get("accepted"),
                "notes": manifest.get("notes", ""),
            })


def finalize_run(output_dir, run_id, script_answers, target_label, status):
    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    config = manifest["configuration"]
    mean, minimum, maximum = measured_temperatures(output_dir / "summary.csv")
    target = target_temperature(target_label)
    pattern_values = ",".join(f"{value:02x}" for value in config["patterns"])
    pattern = pattern_folder_name([
        "--patterns", pattern_values,
        *(["--aggressor-pattern", f"{config['aggressor_pattern']:02x}"]
          if config.get("aggressor_pattern") is not None else []),
    ])
    manifest.update({
        "run_id": run_id,
        "round": round_name(run_id),
        "test": config["experiment"],
        "pattern_name": pattern,
        "target_temp_c": target,
        "measured_temp_mean_c": mean,
        "measured_temp_min_c": minimum,
        "measured_temp_max_c": maximum,
        "script_answers": script_answers,
        "accepted": None,
        "notes": "",
    })
    manifest_path.chmod(0o644)
    manifest_path.write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    manifest_path.chmod(0o444)

    results_directory = output_dir.parent
    ensure_runs_index(results_directory)
    index = results_directory / "runs_index.csv"
    fields = ["run_id", "folder", "test", "pattern", "target_temp_c",
              "measured_temp_mean_c", "temp_spread_c", "start_local", "status",
              "accepted", "notes"]
    with index.open("a", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        if index.stat().st_size == 0:
            writer.writeheader()
        writer.writerow({
            "run_id": run_id,
            "folder": output_dir.name,
            "test": config["experiment"],
            "pattern": pattern,
            "target_temp_c": target,
            "measured_temp_mean_c": mean,
            "temp_spread_c": None if mean is None else maximum - minimum,
            "start_local": output_dir.name[:15],
            "status": status,
            "accepted": "",
            "notes": "",
        })
    spread = None if mean is None else maximum - minimum
    in_range = (
        mean is not None
        and (22 <= mean <= 30 if target is None else abs(mean - target) <= 1)
    )
    print(f"Folder: {output_dir.name}")
    print(f"Measured temperature: mean={mean} C, spread={spread} C")
    print(f"Temperature within target: {in_range}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run main.py for a fixed duration in a new results folder.")
    parser.add_argument("--duration-minutes", type=float, required=True,
                        help="maximum run time before graceful shutdown")
    temperature = parser.add_mutually_exclusive_group(required=True)
    temperature.add_argument("--temperature-config", type=Path,
                             help="fixed FT200 JSON configuration; also supplies the label")
    temperature.add_argument("--vanilla", action="store_true",
                             help="leave DRAM temperature unchanged and keep liquid circulation off")
    parser.add_argument("--output-root", type=Path, default=Path("results"),
                        help="parent directory for generated result folders")
    parser.add_argument("--run-id", help="run-sheet ID; defaults to the next ADHOC-NN ID")
    parser.add_argument("--redo", action="store_true",
                        help="mark this run as a repeat after a rejected run")
    parser.add_argument("--script-answer", action="append", default=[],
                        help="exact answer typed into the launcher; may be repeated")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="main.py command after '--', beginning with ber or hcfirst")
    return parser


def run_automated(args):
    if not args.command or args.command[0] != "--":
        raise ValueError("put the main.py command after '--', for example: -- ber ...")
    command_args = args.command[1:]
    if not command_args or command_args[0] not in {"ber", "hcfirst"}:
        raise ValueError("the experiment command must begin with 'ber' or 'hcfirst'")
    if args.duration_minutes <= 0:
        raise ValueError("duration must be positive")
    if "--output-dir" in command_args:
        raise ValueError("do not pass --output-dir; the wrapper creates it")
    if "--temperature" in command_args or "--temperature-label" in command_args:
        raise ValueError("do not pass --temperature; it is derived from the JSON")
    if "--temperature-config" in command_args:
        raise ValueError("do not pass --temperature-config; use the wrapper option")

    if args.vanilla:
        temperature_label = "vanilla"
    else:
        with args.temperature_config.open() as stream:
            temperature = json.load(stream)
        if "dram_target" not in temperature:
            raise ValueError("temperature configuration must contain dram_target")
        temperature_label = f"{float(temperature['dram_target']):g} C"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    experiment = command_args[0]
    pattern = pattern_folder_name(command_args)
    args.output_root.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or allocate_run_id(args.output_root)
    if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", run_id):
        raise ValueError("run ID must contain only letters, numbers, and hyphens")
    output_dir = args.output_root / temperature_folder_name(
        temperature_label, experiment, pattern, timestamp
    )
    output_dir = output_dir.with_name(output_dir.name.replace(
        f"_{experiment}_", f"_{run_id}_{experiment}_", 1
    ))
    output_dir = output_dir.with_name(output_dir.name + folder_suffixes(
        command_args, args.redo
    ))
    output_dir.mkdir(parents=True, exist_ok=False)

    command = [sys.executable, str(Path(__file__).with_name("main.py")), *command_args,
               "--output-dir", str(output_dir)]
    if args.vanilla:
        command.extend(["--temperature-label", "vanilla"])
    else:
        command.extend(["--temperature-config", str(args.temperature_config)])

    if args.vanilla:
        _, liquid_controller = open_ft200_pair()
        liquid_controller.changeTargetTemp(LIQUID_OFF_TARGET)
        print(f"Liquid cooling disabled at {LIQUID_OFF_TARGET:g} C.", flush=True)

    print(f"Output directory: {output_dir}", flush=True)
    print("Command: " + " ".join(command), flush=True)
    process = subprocess.Popen(command)
    try:
        process.wait(timeout=args.duration_minutes * 60)
    except subprocess.TimeoutExpired:
        print("Duration reached; requesting graceful shutdown.", flush=True)
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            print("Graceful shutdown exceeded 60 seconds; killing process.", flush=True)
            process.kill()
            process.wait()
    # plot_command = [sys.executable, str(Path(__file__).with_name("plot_results_max_2000.py")),
    #                 str(output_dir), "--output", str(output_dir / f"{output_dir.name}.png")]
    status = "complete" if process.returncode == 0 else "aborted"
    if status == "aborted":
        aborted_dir = output_dir.with_name(f"{output_dir.name}_aborted")
        output_dir.rename(aborted_dir)
        output_dir = aborted_dir
    plot_command = [sys.executable, str(Path(__file__).with_name("plot_results.py")),
                    str(output_dir), "--output", str(output_dir / f"{output_dir.name}.png")]
    try:
        plotted = subprocess.run(plot_command, check=False, text=True,
                                 capture_output=True)
    except OSError as error:
        print(f"Warning: could not start result plotting: {error}", file=sys.stderr)
    else:
        if plotted.stdout:
            print(plotted.stdout, end="")
        if plotted.returncode:
            detail = plotted.stderr.strip() or "unknown plotting error"
            print(f"Warning: result plotting failed: {detail}", file=sys.stderr)
    finalize_run(output_dir, run_id, args.script_answer, temperature_label, status)
    return process.returncode


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run_automated(args)
    except (OSError, ValueError) as error:
        print(f"automate_experiment.py: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
