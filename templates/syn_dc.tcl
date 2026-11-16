# Synopsys Design Compiler front-end baseline template.
# Project-provided SDC and multi-corner library setups always take precedence.
#
# Required env:
#   DC_TOP          Top module name
#   DC_FILELIST     VCS-style RTL filelist (-f/+incdir+/+define+ supported)
#   DC_LIB_DB       Space-separated target .db library path(s)
#   DC_CLK_PERIOD   Clock period in library time units (typically ns)
#   DC_UNCERTAINTY  Setup/hold clock uncertainty
#   DC_OUT_LOAD     Output capacitive load
#
# Optional env:
#   DC_CLK_PORT          Clock port name (default: clk)
#   DC_IO_DELAY          Input/output external delay (default: 0.2 * DC_CLK_PERIOD)
#   DC_SDC_FILE          External SDC file (overrides default single-clock constraints)
#   DC_MACRO_DBS         Additional macro/SRAM .db files for link_library
#   DC_SYNC_RESET_PORTS  Synchronous reset ports (timed normally; NEVER false-pathed!)
#   DC_ASYNC_RESET_PORTS Asynchronous reset input ports (cuts external port async assertion path ONLY)
#   DC_GATE_CLOCK        1 to enable ICG insertion via compile_ultra -gate_clock (default: 1)

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

set TOP        [require_env DC_TOP]
set FILELIST   [require_env DC_FILELIST]
set LIB_DBS    [require_env DC_LIB_DB]
set MACRO_DBS  [env_or DC_MACRO_DBS ""]
set CLK_PORT   [env_or DC_CLK_PORT clk]
set CLK_PERIOD [require_env DC_CLK_PERIOD]
set UNCERT     [require_env DC_UNCERTAINTY]
set OUT_LOAD   [require_env DC_OUT_LOAD]
set IO_DELAY   [env_or DC_IO_DELAY [expr {0.2 * $CLK_PERIOD}]]
set SYNC_RSTS  [env_or DC_SYNC_RESET_PORTS ""]
set ASYNC_RSTS [env_or DC_ASYNC_RESET_PORTS ""]
set SDC_FILE   [env_or DC_SDC_FILE ""]
set GATE_CLK   [env_or DC_GATE_CLOCK 1]

file mkdir work reports out
define_design_lib WORK -path ./work

# Record guidance for Formality LEC (mandatory when compile_ultra merges/inverts/gates flops).
set_svf out/${TOP}.svf

set search_dirs [list .]
foreach db [concat $LIB_DBS $MACRO_DBS] {
  lappend search_dirs [file dirname $db]
}
set_app_var search_path [lsort -unique $search_dirs]
set_app_var target_library $LIB_DBS
set_app_var synthetic_library dw_foundation.sldb
set_app_var link_library [concat [list "*" dw_foundation.sldb] $LIB_DBS $MACRO_DBS]

if {![file isfile $FILELIST]} {
  puts stderr "ERROR: DC_FILELIST not found: $FILELIST"
  exit 2
}

# Use -vcs "-f ..." so +incdir+, +define+, -f/-F, -y, and package order work natively.
if {![analyze -format sverilog -vcs "-f $FILELIST"]} {
  puts stderr "ERROR: RTL analyze failed"
  exit 1
}
if {![elaborate $TOP]} {
  puts stderr "ERROR: elaborate $TOP failed"
  exit 1
}
current_design $TOP
if {![link]} {
  puts stderr "ERROR: unresolved references during link"
  exit 1
}
uniquify

redirect reports/check_design_pre.rpt { check_design }
redirect reports/references_pre.rpt   { report_reference -hierarchy }

if {$SDC_FILE ne ""} {
  if {![file isfile $SDC_FILE]} {
    puts stderr "ERROR: DC_SDC_FILE not found: $SDC_FILE"
    exit 2
  }
  source -echo -verbose $SDC_FILE
} else {
  create_clock -name $CLK_PORT -period $CLK_PERIOD [get_ports $CLK_PORT]
  set_clock_uncertainty $UNCERT [get_clocks $CLK_PORT]

  set non_data_ports [get_ports -quiet [concat [list $CLK_PORT] $SYNC_RSTS $ASYNC_RSTS]]
  set data_inputs [remove_from_collection [all_inputs] $non_data_ports]
  if {[sizeof_collection $data_inputs] > 0} {
    set_input_delay $IO_DELAY -clock $CLK_PORT $data_inputs
  }
  # Synchronous resets ARE timed against CLK_PORT; never slap set_false_path on sync resets.
  if {$SYNC_RSTS ne ""} {
    set sync_rst_col [get_ports -quiet $SYNC_RSTS]
    if {[sizeof_collection $sync_rst_col] > 0} {
      set_input_delay $IO_DELAY -clock $CLK_PORT $sync_rst_col
    }
  }
  # Only external raw asynchronous reset pad assertion may be cut if synchronized internally.
  if {$ASYNC_RSTS ne ""} {
    set async_rst_col [get_ports -quiet $ASYNC_RSTS]
    if {[sizeof_collection $async_rst_col] > 0} {
      set_false_path -from $async_rst_col
    }
  }
  if {[sizeof_collection [all_outputs]] > 0} {
    set_output_delay $IO_DELAY -clock $CLK_PORT [all_outputs]
    set_load $OUT_LOAD [all_outputs]
  }
}

# Separate I/O paths from internal REG2REG paths so estimated IO budgets do not mask core timing.
set core_regs [all_registers -edge_triggered]
if {[sizeof_collection $core_regs] > 0} {
  group_path -name REG2REG -from $core_regs -to $core_regs -weight 2.0 -critical_range [expr {0.1 * $CLK_PERIOD}]
}
if {[sizeof_collection [all_inputs]] > 0 && [sizeof_collection $core_regs] > 0} {
  group_path -name IN2REG -from [all_inputs] -to $core_regs
}
if {[sizeof_collection $core_regs] > 0 && [sizeof_collection [all_outputs]] > 0} {
  group_path -name REG2OUT -from $core_regs -to [all_outputs]
}
if {[sizeof_collection [all_inputs]] > 0 && [sizeof_collection [all_outputs]] > 0} {
  group_path -name IN2OUT -from [all_inputs] -to [all_outputs]
}

redirect reports/check_timing_pre.rpt {
  check_timing -include {no_clock unconstrained_endpoints loops generated_clocks}
}

set compile_cmd [list compile_ultra -no_autoungroup]
if {$GATE_CLK eq "1"} {
  lappend compile_cmd -gate_clock
}
eval $compile_cmd

redirect reports/check_design_post.rpt { check_design }
redirect reports/check_timing_post.rpt {
  check_timing -include {no_clock unconstrained_endpoints loops generated_clocks}
}
redirect reports/qor.rpt               { report_qor }
redirect reports/area.rpt              { report_area -hierarchy }
redirect reports/power.rpt             { report_power -hierarchy }
redirect reports/timing_setup.rpt      { report_timing -delay_type max -max_paths 20 -nets -transition_time -capacitance }
redirect reports/timing_hold.rpt       { report_timing -delay_type min -max_paths 20 -nets -transition_time -capacitance }
redirect reports/constraints.rpt       { report_constraint -all_violators }
redirect reports/references.rpt        { report_reference -hierarchy }
if {$GATE_CLK eq "1"} {
  redirect reports/clock_gating.rpt    { report_clock_gating -gating_elements }
}
redirect reports/removed_flops.rpt     { report_compile_options; report_transformed_registers }
redirect reports/latches.rpt {
  foreach_in_collection latch [all_registers -level_sensitive] {
    puts [get_object_name $latch]
  }
}

write -format verilog -hierarchy -output out/${TOP}_syn.v
write -format ddc     -hierarchy -output out/${TOP}_syn.ddc
write_sdc out/${TOP}_syn.sdc
set_svf -off

quit
