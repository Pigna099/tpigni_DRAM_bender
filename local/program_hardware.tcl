# Volatile JTAG programming of this host's U200, not flash programming.
if {$argc != 1} { error "Expected the bitstream path." }
set bitfile [file normalize [lindex $argv 0]]
if {![file isfile $bitfile]} { error "Missing bitstream: $bitfile" }

if {[catch {
    open_hw_manager
    connect_hw_server -url localhost:3121
    set targets [get_hw_targets */Xilinx/21291036400NA]
    if {[llength $targets] != 1} { error "Expected U200 JTAG serial 21291036400NA." }
    current_hw_target [lindex $targets 0]
    set_property PARAM.FREQUENCY 15000000 [current_hw_target]
    open_hw_target
    set devices [get_hw_devices xcu200_0]
    if {[llength $devices] != 1} { error "Expected one xcu200_0 device." }
    set device [lindex $devices 0]
    set_property PROGRAM.FILE $bitfile $device
    set_property PROBES.FILE {} $device
    program_hw_devices $device
    refresh_hw_device -update_hw_probes false $device
    puts "PROGRAMMED_BITSTREAM: $bitfile"
    set migs [get_hw_migs -quiet]
    if {[llength $migs] != 1} { error "Expected one DDR4 MIG core." }
    foreach mig $migs {
        for {set attempt 0} {$attempt < 20} {incr attempt} {
            refresh_hw_mig $mig
            if {[get_property CALIBRATION_FAIL.STATUS $mig] eq "TRUE"} {
                error "DDR4 calibration failed."
            }
            if {[get_property CAL_STATUS.RANK0.14_WRITE_READ_SANITY_CHECK $mig] eq "PASS"} {
                break
            }
            after 500
        }
        if {[get_property CAL_STATUS.RANK0.14_WRITE_READ_SANITY_CHECK $mig] ne "PASS"} {
            error "DDR4 calibration did not complete."
        }
        report_property $mig
        report_hw_mig $mig
    }
    close_hw_target
    disconnect_hw_server
    close_hw_manager
} detail]} {
    puts stderr "PROGRAMMING_FAILED: $detail"
    exit 1
}
exit 0
