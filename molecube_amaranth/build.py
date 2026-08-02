#

from .toplevel import TopLevel

from amaranth_zynq.platform import ZC702Platform

from transactron import TransactronContextElaboratable
from transactron.utils.gen_hacks import fixup_vivado_transparent_memories

import importlib.util
from pathlib import Path
import shutil
import subprocess

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

boot_dir = Path(__file__).resolve().parent.parent / "boot"

def build_fsbl(config, *, build_dir='build_boot'):
    from xilinx_ps_config.zynq_config import ZynqConfig
    from xilinx_ps_config.zynq_fsbl import gen_board_files

    build_dir = Path(build_dir)

    zynq_config = ZynqConfig.from_preset("zc702")
    zynq_config.FCLK[0].enable(config.CLOCK_HZ / 1e6)

    build_fsbl_dir = build_dir / "fsbl"
    if build_fsbl_dir.exists() and build_fsbl_dir.is_dir():
        shutil.rmtree(build_fsbl_dir)

    proj_path = boot_dir / "embeddedsw"
    shutil.copytree(proj_path, build_fsbl_dir, ignore=shutil.ignore_patterns('.git*'))
    fsbl_dir = build_fsbl_dir / "lib" / "sw_apps" / "zynq_fsbl"
    gen_board_files(fsbl_dir / "misc" / "molecube", zynq_config)
    subprocess.run(["make", "-j", "1", "BOARD=molecube",
                    "-C", fsbl_dir / "src"])

    (fsbl_dir / "src" / "fsbl.elf").copy_into(build_dir)

def build_uboot(*, build_dir='build_boot'):
    build_dir = Path(build_dir)

    build_uboot_dir = build_dir / "u-boot"
    if build_uboot_dir.exists() and build_uboot_dir.is_dir():
        shutil.rmtree(build_uboot_dir)

    proj_path = boot_dir / "u-boot"
    shutil.copytree(proj_path, build_uboot_dir, ignore=shutil.ignore_patterns('.git*'))

    subprocess.run(["make", "xilinx_zynq_virt_defconfig", "DEVICE_TREE=zynq-zc702",
                    "ARCH=arm", "CROSS_COMPILE=armv7l-linux-gnueabihf-",
                    "-C", build_uboot_dir])

    with (build_uboot_dir / ".config").open('a') as io:
        print("\nCONFIG_ENV_OVERWRITE=y", file=io)

    subprocess.run(["make", "DEVICE_TREE=zynq-zc702",
                    "ARCH=arm", "CROSS_COMPILE=armv7l-linux-gnueabihf-",
                    "-C", build_uboot_dir])

    (build_uboot_dir / "u-boot.elf").copy_into(build_dir)
    subprocess.run(["armv7l-linux-gnueabihf-strip", build_dir / "u-boot.elf"])

    subprocess.run([build_uboot_dir / "tools" / "mkimage", "-A", "arm",
                    "-T", "script", "-d", boot_dir / "boot.cmd",
                    build_dir / "boot.scr"])

def build_boot(config, *, build_dir='build_boot'):
    build_dir = Path(build_dir)

    build_fsbl(config, build_dir=build_dir)
    build_uboot(build_dir=build_dir)
    (boot_dir / "boot.bif").copy_into(build_dir)
    subprocess.run(["bootgen", "-image", "boot.bif",
                    "-w", "-o", "boot.bin"], cwd=build_dir)

def load_var(path, var):
    spec = importlib.util.spec_from_file_location("tmp_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, var)
