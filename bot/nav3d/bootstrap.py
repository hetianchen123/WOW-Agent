"""Explicit dependency bootstrap; sources/assets stay outside Git under data/."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import urllib.request
from .casc import CASC_COMMIT, EXPORT_COMMIT
from .backend import NavigationError

DBD_COMMIT='5ebf2124996b538d6784584e69db6fe8d6bd8d06'
DBD_SHA256='4e3513240065928a94d4d41be817bc54bb419fdf327891b904c2eab124db4d1d'
ROOT=Path(__file__).resolve().parents[2]


def run(*args):subprocess.run([str(a) for a in args],check=True)


def checkout(folder,url,revision):
    if not folder.exists():
        run('git','clone','--no-checkout',url,folder)
        run('git','-C',folder,'checkout','--detach',revision)
    current=subprocess.check_output(['git','-C',str(folder),'rev-parse','HEAD'],text=True).strip()
    if current!=revision:raise NavigationError(f'dependency_revision_mismatch:{folder}')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deps',type=Path,default=ROOT/'data/nav3d/deps')
    parser.add_argument('--cmake',default=shutil.which('cmake'))
    args=parser.parse_args()
    if not args.cmake:parser.error('CMake required; use uv tool install cmake==3.31.6 or pass --cmake')
    deps=args.deps.resolve();deps.mkdir(parents=True,exist_ok=True)
    checkout(deps/'CascLib','https://github.com/ladislav-zezula/CascLib.git',CASC_COMMIT)
    checkout(deps/'wow.export','https://github.com/Kruithne/wow.export.git',EXPORT_COMMIT)
    patch=(ROOT/'native/nav3d/casc/readonly.patch').read_bytes()
    diff=subprocess.check_output(['git','-C',str(deps/'CascLib'),'diff','HEAD'])
    if not diff:run('git','-C',deps/'CascLib','apply',ROOT/'native/nav3d/casc/readonly.patch')
    elif diff!=patch:raise NavigationError('casclib_patch_mismatch')
    dbd=deps/'Map.dbd'
    if not dbd.exists():
        url=f'https://raw.githubusercontent.com/wowdev/WoWDBDefs/{DBD_COMMIT}/definitions/Map.dbd'
        with urllib.request.urlopen(url,timeout=30) as response:data=response.read()
        if hashlib.sha256(data).hexdigest()!=DBD_SHA256:raise NavigationError('map_dbd_checksum_mismatch')
        dbd.write_bytes(data)
    if hashlib.sha256(dbd.read_bytes()).hexdigest()!=DBD_SHA256:raise NavigationError('map_dbd_checksum_mismatch')
    flags=[]
    if platform.system()=='Darwin':
        sdk=subprocess.check_output(['xcrun','--show-sdk-path'],text=True).strip()
        flags=[f'-DCMAKE_CXX_FLAGS=-isystem {sdk}/usr/include/c++/v1']
    run(args.cmake,'-S',deps/'CascLib','-B',deps/'CascLib-build','-DCMAKE_BUILD_TYPE=Release','-DCASC_BUILD_TESTS=OFF',*flags)
    run(args.cmake,'--build',deps/'CascLib-build','-j','4')
    lock=dict(casclib=CASC_COMMIT,wow_export=EXPORT_COMMIT,dbd_commit=DBD_COMMIT,dbd_sha256=DBD_SHA256,
        readonly_patch_sha256=hashlib.sha256(patch).hexdigest())
    (deps/'lock.json').write_text(json.dumps(lock,indent=2));print(json.dumps(lock))


if __name__=='__main__':main()
