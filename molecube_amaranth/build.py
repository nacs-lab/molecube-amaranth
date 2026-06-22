#

from molecube_amaranth.toplevel import TopLevel
from amaranth_zynq.platform import ZC702Platform
from transactron import TransactronContextElaboratable

def build_zc702(config, do_build=True, build_dir="build"):
    top = TopLevel(config)
    core = TransactronContextElaboratable(top)
    plat = ZC702Platform()
    plan = plat.build(core, do_build=do_build, build_dir=build_dir,
                      synth_design_opts="-directive PerformanceOptimized",
                      script_after_synth="""
foreach cell [get_cells -quiet -hier -filter {molecube.vivado.false_path_from == "TRUE"}] {
    puts "Set false path from $cell"
    set_false_path -from $cell
}
foreach cell [get_cells -quiet -hier -filter {molecube.vivado.false_path_to == "TRUE"}] {
    puts "Set false path to $cell"
    set_false_path -to $cell
}
""",
                      # Supposed to be more useful to do optimization
                      # before routing after placing
                      script_after_place="""
phys_opt_design -directive AggressiveExplore
phys_opt_design -directive AggressiveFanoutOpt
phys_opt_design -directive AlternateReplication
""",
                      # Run an extra physical optimization pass
                      # for fan-out and hold fixing
                      # before the phys_opt_design already present in the template
                      script_after_route="""
phys_opt_design -directive AggressiveExplore
phys_opt_design -directive AggressiveFanoutOpt
phys_opt_design -directive AlternateReplication
""")
    if not do_build:
        plan.extract(build_dir)
