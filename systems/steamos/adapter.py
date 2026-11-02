#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse,subprocess,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--output',required=True,type=Path);p.add_argument('--source',required=True,type=Path);a=p.parse_args()
r=Path(__file__).resolve().parent
subprocess.run([sys.executable,str(r/'legacy/prepare.py'),'--profile',str(r/'legacy/profile.json'),'--archive',str(a.source.resolve()),'--output',str(a.output.resolve())],check=True)
