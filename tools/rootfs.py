#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Extensible system adapter, model overlay and exact-component composition."""
import argparse,hashlib,io,json,shutil,subprocess,sys,tarfile
from pathlib import Path,PurePosixPath
r=Path(__file__).resolve().parents[1]
def load(p):return json.loads(p.read_text())
def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for chunk in iter(lambda:f.read(4*1024*1024),b''):h.update(chunk)
 return h.hexdigest()
def key(s):
 if not s or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in s):raise ValueError('Invalid adapter/model/profile identifier')
 return s
p=argparse.ArgumentParser();p.add_argument('command',choices=['plan','prepare','assemble']);p.add_argument('--system',required=True,type=key);p.add_argument('--device',default='a081',type=key);p.add_argument('--profile',default='vr',type=key);p.add_argument('--source',help='prepare: mirror URL or existing source archive');p.add_argument('--base-archive',type=Path);p.add_argument('--artifacts',type=Path,help='JSON list of {source, destination, sha256} local artifacts');p.add_argument('--output',type=Path);a=p.parse_args()
adapter_path=r/'systems'/a.system;adapter=load(adapter_path/'adapter.json');model=load(r/'devices'/a.device/'model.json');profile=load(r/'profiles'/(a.profile+'.json'));locks=load(r/'components.lock.json')
if a.system not in model['systems']:p.error('Model does not list this system adapter')
selected={n:locks['components'][n] for n in profile['components']}
manifest={'schema':1,'system':adapter,'device':model,'profile':profile,'components':selected,'hardware_validated':False,'device_write_performed':False}
if a.command=='plan':print(json.dumps(manifest,indent=2,ensure_ascii=False));sys.exit(0)
if a.output is None or a.output.exists() or a.output.is_symlink():p.error('Choose a new output directory')
if a.command=='prepare':
 if not a.source:p.error('prepare requires --source')
 substitutions={'output':str(a.output.resolve()),'source':a.source}
 spec=adapter['prepare'];cmd=[sys.executable,str(adapter_path/spec['program'])]+[x.format(**substitutions) for x in spec['arguments']];subprocess.run(cmd,check=True);(a.output/'composition-plan.json').write_text(json.dumps(manifest,indent=2)+'\n');sys.exit(0)
if a.base_archive is None or not a.base_archive.is_file():p.error('assemble requires an existing base archive')
for name,lock in selected.items():
 if len(lock.get('commit',''))!=40:p.error('Need exact component commits: '+name)
items=load(a.artifacts) if a.artifacts else [];validated=[]
for item in items:
 src=Path(item['source']);dest=PurePosixPath(item['destination'])
 if dest.is_absolute() or '..' in dest.parts or not dest.parts or dest.parts[0] not in ['opt','usr','etc','lib']:p.error('Invalid root-relative artifact destination')
 if not src.is_file() or src.is_symlink() or digest(src)!=item['sha256']:p.error('Artifact missing, symlink, or checksum mismatch')
 validated.append((src,str(dest),item['sha256']))
a.output.mkdir(parents=True);base=a.output/('base'+''.join(a.base_archive.suffixes));shutil.copyfile(a.base_archive,base);members={}
overlay_root=r/'devices'/a.device/model['overlay']
for q in overlay_root.rglob('*'):
 if q.is_file() and not q.is_symlink():members[str(q.relative_to(overlay_root))]=q
for q,dest,_ in validated:
 if dest in members:p.error('Duplicate overlay destination '+dest)
 members[dest]=q
with tarfile.open(a.output/'overlay.tar','w',format=tarfile.PAX_FORMAT) as tf:
 for dest,q in sorted(members.items()):
  data=q.read_bytes();ti=tarfile.TarInfo(dest);ti.mode=q.stat().st_mode&0o777;ti.size=len(data);ti.uid=ti.gid=0;ti.mtime=0;tf.addfile(ti,io.BytesIO(data))
manifest.update(base_archive={'name':base.name,'sha256':digest(base),'metadata_preservation':'input archive copied byte-for-byte'},overlay={'name':'overlay.tar','sha256':digest(a.output/'overlay.tar')},artifacts=[{'destination':d,'sha256':h} for _,d,h in validated],status='composition bundle; native Debian second-stage, vendor assets, kernel image and device acceptance are separate gates')
(a.output/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n');print(json.dumps({'bundle':str(a.output),'hardware_validated':False}))
