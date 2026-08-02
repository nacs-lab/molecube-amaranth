#!/usr/bin/env python

from molecube_amaranth.build import build_zc702

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

config_path = args.config_file
config_var = args.var

config_spec = importlib.util.spec_from_file_location("config_module", config_path)
config_module = importlib.util.module_from_spec(config_spec)
config_spec.loader.exec_module(config_module)

build_zc702(getattr(config_module, config_var), do_build=args.build,
            build_dir=args.build_dir)
