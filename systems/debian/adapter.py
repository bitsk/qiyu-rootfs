#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse,json,subprocess
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--output',required=True,type=Path);p.add_argument('--source',required=True,help='Debian mirror URL');p.add_argument('--suite',default='trixie');a=p.parse_args()
if a.output.exists():p.error('Choose a new output directory')
a.output.mkdir(parents=True);root=a.output/'foreign-stage'
subprocess.run(['debootstrap','--foreign','--arch=arm64','--variant=minbase',a.suite,str(root),a.source],check=True)
subprocess.run(['tar','--numeric-owner','--xattrs','--acls','-cpf',str(a.output/'rootfs.tar'),'-C',str(root),'.'],check=True)
(a.output/'prepare.json').write_text(json.dumps({'system':'debian','suite':a.suite,'mirror':a.source,'stage':'foreign; requires native second-stage before boot','device_tested':False},indent=2)+'\n')
