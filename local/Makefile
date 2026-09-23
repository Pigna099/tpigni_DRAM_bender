# Host administration uses the existing sudo permission for /usr/bin/make.
# Application tests run as the normal user, never through this Makefile.
SHELL := /bin/bash
REPO := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
VIVADO_LAB := /opt/Xilinx/Vivado_Lab/2022.1/bin/vivado_lab

.PHONY: inspect-hardware program-hardware load-driver
inspect-hardware:
	cd $(REPO)/build/machine-check && $(VIVADO_LAB) -mode batch -nolog -nojournal -source $(REPO)/local/inspect_hardware.tcl

program-hardware:
	bash $(REPO)/local/manage_hardware.sh program

load-driver:
	bash $(REPO)/local/manage_hardware.sh load-driver
