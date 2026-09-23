#!/usr/bin/env bash
# Deployment helper for safari-fpga62's single U200. Run via local/Makefile.
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pci_bdf=0000:01:00.0
pci_device="/sys/bus/pci/devices/${pci_bdf}"
bitfile="${repo_dir}/external/dram-bender/hw/prebuilt/XCU200/XCU200_C1_UDIMM_ANYRANK_x8.bit"
vivado_lab=/opt/Xilinx/Vivado_Lab/2022.1/bin/vivado_lab

[[ ${EUID} == 0 ]] || { echo 'Run with sudo make -f local/Makefile.' >&2; exit 1; }
[[ $(hostname -s) == safari-fpga62 ]] || { echo 'This helper is specific to safari-fpga62.' >&2; exit 1; }
[[ -f "${repo_dir}/external/dram-bender/xdma/xdma/drambender_xdma.ko" ]] || { echo 'Build the V2 driver first.' >&2; exit 1; }
[[ $(modinfo -F vermagic "${repo_dir}/external/dram-bender/xdma/xdma/drambender_xdma.ko") == "$(uname -r) "* ]] || {
    echo 'Rebuild the driver for the running kernel.' >&2; exit 1;
}

check_unused() {
    local nodes endpoints
    shopt -s nullglob
    nodes=(/dev/xdma*)
    endpoints=(/sys/bus/pci/drivers/xdma/????:??:??.?)
    shopt -u nullglob
    if (( ${#endpoints[@]} > 1 )); then
        echo 'Refusing a global driver change with multiple FPGA endpoints.' >&2
        exit 1
    fi
    if (( ${#nodes[@]} )) && fuser -v "${nodes[@]}"; then
        echo 'An application is using XDMA; close it before changing hardware.' >&2
        exit 1
    fi
}

unload_driver() {
    local owner
    check_unused
    if [[ -e /sys/bus/pci/drivers/xdma/module ]]; then
        owner="$(basename "$(readlink -f /sys/bus/pci/drivers/xdma/module)")"
        case "$owner" in
            xdma|drambender_xdma) rmmod "$owner" ;;
            *) echo "Unexpected PCI driver owner: $owner" >&2; exit 1 ;;
        esac
    fi
}

case "${1:-}" in
    program)
        [[ -x "$vivado_lab" && -f "$bitfile" && -e "$pci_device" ]]
        [[ -d "${repo_dir}/build/machine-check" ]] || {
            echo 'Create build/machine-check as your normal user first.' >&2; exit 1;
        }
        [[ $(cat "${pci_device}/vendor") == 0x10ee ]]
        [[ $(cat "${pci_device}/device") == 0x9038 ]]
        upstream_bridge="$(dirname "$(readlink -f "$pci_device")")"
        [[ -w "${upstream_bridge}/rescan" ]]
        unload_driver
        # Remove only the FPGA PCI function, then rediscover it after JTAG.
        trap 'echo 1 > "${upstream_bridge}/rescan"' EXIT
        echo 1 > "${pci_device}/remove"
        cd "${repo_dir}/build/machine-check"
        "$vivado_lab" -mode batch -nolog -nojournal \
            -source "${repo_dir}/local/program_hardware.tcl" -tclargs "$bitfile"
        ;;
    load-driver)
        [[ -e "$pci_device" ]]
        if [[ -e /sys/module/drambender_xdma/srcversion ]] &&
           [[ $(cat /sys/module/drambender_xdma/srcversion) == \
              "$(modinfo -F srcversion "${repo_dir}/external/dram-bender/xdma/xdma/drambender_xdma.ko")" ]]; then
            echo 'The matching V2 driver is already loaded.'
        else
            unload_driver
            "${repo_dir}/external/dram-bender/xdma/load_driver.sh"
        fi
        readlink -f /sys/bus/pci/drivers/xdma/module
        ls -l /dev/xdma*_h2c_* /dev/xdma*_c2h_*
        ;;
    *) echo 'Usage: manage_hardware.sh program|load-driver' >&2; exit 2 ;;
esac
