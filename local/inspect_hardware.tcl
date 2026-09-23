# Read-only JTAG inspection; never programs or resets the FPGA.
open_hw_manager
connect_hw_server -url localhost:3121
set targets [get_hw_targets]
puts "JTAG_TARGETS: $targets"
if {[llength $targets] != 1} {
    error "Expected exactly one JTAG target; select the intended board explicitly."
}
current_hw_target [lindex $targets 0]
open_hw_target
foreach device [get_hw_devices] {
    puts "JTAG_DEVICE: $device"
    refresh_hw_device -update_hw_probes false $device
    report_property $device
}
foreach mig [get_hw_migs -quiet] {
    refresh_hw_mig $mig
    report_hw_mig $mig
}
close_hw_target
disconnect_hw_server
close_hw_manager
exit
