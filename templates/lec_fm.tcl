# Synopsys Formality Logic Equivalence Checking (LEC) baseline template.
# Verifies Golden RTL against synthesized gate-level netlist using DC's .svf guidance.
#
# Required env:
#   FM_TOP          Top module name
#   FM_FILELIST     VCS-style RTL filelist (Golden reference)
#   FM_IMPL_VERILOG Synthesized netlist path (e.g., out/<TOP>_syn.v)
#   FM_LIB_DB       Space-separated standard cell .db library path(s)
#
# Optional env:
#   FM_SVF_FILE     SVF guidance file from DC (default: out/${FM_TOP}.svf)
#   FM_MACRO_DBS    Additional macro/SRAM .db files
#   FM_SCAN_CONSTS  Space-separated DFT ports forced to 0 in functional LEC (e.g., "scan_enable test_mode")

proc require_env {name} {
  if {![info exists ::env($name)] || [string trim $::env($name)] eq ""} {
    puts stderr "ERROR: $name is required"
    exit 2
  }
  return [string trim $::env($name)]
}

proc env_or {name default} {
  if {[info exists ::env($name)] && [string trim $::env($name)] ne ""} {
    return [string trim $::env($name)]
  }
  return $default
}

set TOP       [require_env FM_TOP]
set FILELIST  [require_env FM_FILELIST]
set IMPL_V    [require_env FM_IMPL_VERILOG]
set LIB_DBS   [require_env FM_LIB_DB]
set SVF_FILE  [env_or FM_SVF_FILE "out/${TOP}.svf"]
set MACRO_DBS [env_or FM_MACRO_DBS ""]
set DFT_PINS  [env_or FM_SCAN_CONSTS "scan_enable test_mode dft_cg_en"]

file mkdir reports

if {[file isfile $SVF_FILE]} {
  set_svf $SVF_FILE
} else {
  puts stderr "WARNING: SVF guidance not found ($SVF_FILE); retiming/merged flops may fail compare"
}

foreach db [concat $LIB_DBS $MACRO_DBS] {
  read_db $db
}

# Reference (Golden RTL)
read_sverilog -r -vcs "-f $FILELIST"
set_top r:/WORK/$TOP

# Implementation (Synthesized Netlist)
read_verilog -i $IMPL_V
set_top i:/WORK/$TOP

# Bind DFT / scan ports to functional inactive state on both sides if present
foreach pin $DFT_PINS {
  if {[sizeof_collection [get_ports -quiet r:/WORK/$TOP/$pin]] > 0} {
    set_constant r:/WORK/$TOP/$pin 0
  }
  if {[sizeof_collection [get_ports -quiet i:/WORK/$TOP/$pin]] > 0} {
    set_constant i:/WORK/$TOP/$pin 0
  }
}

match
redirect reports/lec_matched.rpt   { report_matched_points }
redirect reports/lec_unmatched.rpt { report_unmatched_points }

set lec_ok [verify]
redirect reports/lec_status.rpt  { report_status }

if {!$lec_ok} {
  redirect reports/lec_failing.rpt { report_failing_points }
  redirect reports/lec_analyze.rpt { analyze_points -failing }
  puts stderr "ERROR: Formality LEC FAILED! See reports/lec_failing.rpt and reports/lec_analyze.rpt"
  exit 1
}

puts "FORMALITY LEC PASSED: $TOP"
quit
