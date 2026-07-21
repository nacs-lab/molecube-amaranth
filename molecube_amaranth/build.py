#

from molecube_amaranth.toplevel import TopLevel
from amaranth_zynq.platform import ZC702Platform
from transactron import TransactronContextElaboratable
from transactron.utils.gen_hacks import fixup_vivado_transparent_memories

class BuildPlatform(ZC702Platform):
    def __init__(self, *args, **kws):
        super().__init__(*args, **kws)
        self._molecube_vivado_fixedup = False

    def toolchain_prepare(self, design, *args, **kws):
        self._molecube_vivado_fixedup = True
        fixup_vivado_transparent_memories(design)
        return super().toolchain_prepare(design, *args, **kws)

def build_zc702(config, do_build=True, build_dir="build"):
    top = TopLevel(config)
    core = TransactronContextElaboratable(top)
    plat = BuildPlatform()
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
    assert plat._molecube_vivado_fixedup
    if not do_build:
        plan.extract(build_dir)
