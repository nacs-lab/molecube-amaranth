#!/usr/bin/env python

from molecube_amaranth.build import build_zc702, load_var

import importlib.util
import argparse

parser = argparse.ArgumentParser(
    prog='build_plan',
    description='Compiling molecube hardware code')
parser.add_argument('config_file')
parser.add_argument('--var', help="Config variable name within config file",
                    default="config")
parser.add_argument('--build', help="Do building", action="store_true")
parser.add_argument('--build_dir', help="Build directory", default="build")
args = parser.parse_args()

build_zc702(load_var(args.config_file, args.var), do_build=args.build,
            build_dir=args.build_dir)
