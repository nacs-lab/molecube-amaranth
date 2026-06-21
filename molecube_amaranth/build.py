#

from molecube_amaranth.toplevel import TopLevel
from amaranth_zynq.platform import ZC702Platform
from transactron import TransactronContextElaboratable

def build_zc702(config, do_build=True, build_dir="build"):
    top = TopLevel(config)
    core = TransactronContextElaboratable(top)
    plat = ZC702Platform()
    plan = plat.build(core, do_build=do_build, build_dir=build_dir, script_after_synth="""
foreach cell [get_cells -quiet -hier -filter {molecube.vivado.false_path_from == "TRUE"}] {
    puts "Set false path from $cell"
    set_false_path -from $cell
}
foreach cell [get_cells -quiet -hier -filter {molecube.vivado.false_path_to == "TRUE"}] {
    puts "Set false path to $cell"
    set_false_path -to $cell
}
""")
    if not do_build:
        plan.extract(build_dir)
