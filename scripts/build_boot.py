#!/usr/bin/env python

from molecube_amaranth.build import load_var, build_fsbl, build_uboot, build_boot

import argparse

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

do_build_boot = not (args.build_fsbl or args.build_uboot)
do_build_fsbl = args.build_fsbl
do_build_uboot = args.build_uboot

if do_build_fsbl:
    build_fsbl(config, build_dir=args.build_dir)

if do_build_uboot:
    build_uboot(build_dir=args.build_dir)

if do_build_boot:
    build_boot(config, build_dir=args.build_dir)
