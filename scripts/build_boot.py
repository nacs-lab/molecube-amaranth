#!/usr/bin/env python

from molecube_amaranth.build import load_var

from xilinx_ps_config.zynq_config import ZynqConfig
from xilinx_ps_config.zynq_fsbl import gen_board_files

import argparse
from pathlib import Path
import shutil
import subprocess

parser = argparse.ArgumentParser(
    prog='build_boot',
    description='Compiling molecube boot binary')
parser.add_argument('config_file')
parser.add_argument('--var', help="Config variable name within config file",
                    default="config")

group = parser.add_mutually_exclusive_group()
group.add_argument("--build_fsbl", action="store_true", help="Build fsbl.elf")
group.add_argument("--build_uboot", action="store_true", help="Build uboot.elf")
group.add_argument("--build_boot", action="store_true", help="Build boot.bin")

parser.add_argument('--build_dir', help="Build directory", default="build_boot")
args = parser.parse_args()

config = load_var(args.config_file, args.var)

build_dir = Path(args.build_dir)
boot_dir = Path(__file__).resolve().parent.parent / "boot"

def do_fsbl_build():
    zynq_config = ZynqConfig.from_preset("zc702")
    zynq_config.FCLK[0].enable(config.CLOCK_HZ / 1e6)

    build_fsbl_dir = build_dir / "fsbl"
    if build_fsbl_dir.exists() and build_fsbl_dir.is_dir():
        shutil.rmtree(build_fsbl_dir)

    proj_path = boot_dir / "embeddedsw"
    shutil.copytree(proj_path, build_fsbl_dir, ignore=shutil.ignore_patterns('.git*'))
    fsbl_dir = build_fsbl_dir / "lib" / "sw_apps" / "zynq_fsbl"
    gen_board_files(fsbl_dir / "misc" / "molecube",
                    zynq_config)
    subprocess.run(["make", "-j", "1", "BOARD=molecube",
                    "-C", fsbl_dir / "src"])

    (fsbl_dir / "src" / "fsbl.elf").copy_into(build_dir)

def do_uboot_build():
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

def do_boot_build():
    (boot_dir / "boot.bif").copy_into(build_dir)
    subprocess.run(["bootgen", "-image", "boot.bif",
                    "-w", "-o", "boot.bin"], cwd=build_dir)

build_boot = not (args.build_fsbl or args.build_uboot)
build_fsbl = build_boot or args.build_fsbl
build_uboot = build_boot or args.build_uboot

if build_fsbl:
    do_fsbl_build()

if build_uboot:
    do_uboot_build()

if build_boot:
    do_boot_build()
