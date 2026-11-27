#!/usr/bin/env python3
"""Verify/restore installation workspace boot+recovery backups via Fastboot.
Does not reconstruct erased system_a/vendor_a/vendor_b/ramdump data.
"""
import argparse,hashlib,json,pathlib,shutil,subprocess


def digest(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for x in iter(lambda:f.read(4<<20),b''):h.update(x)
    return h.hexdigest()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',required=True);p.add_argument('--serial')
    p.add_argument('--apply',action='store_true');p.add_argument('--active-slot',choices=['a','b'],default='a')
    a=p.parse_args();w=pathlib.Path(a.workspace)
    meta=json.loads((w/'partition-backups.json').read_text(encoding='utf-8'))
    for label in ['boot_a','boot_b','system_b','vbmeta_a','vbmeta_b']:
        item=meta[label];f=w/'partition-backups'/item['file']
        if f.name!=label+'.img' or f.is_symlink() or f.stat().st_size!=item['bytes'] or digest(f)!=item['sha256']:
            raise ValueError('backup mismatch '+label)
    if not a.apply:
        print('Backup hashes verified. No device operation. Main-pool/business data is NOT restored by this tool.');return
    if not a.serial:p.error('--apply requires explicit --serial')
    fb=shutil.which('fastboot')
    if not fb:raise FileNotFoundError('fastboot must be in PATH')
    def run(*args):
        r=subprocess.run([fb,'-s',a.serial,*map(str,args)],capture_output=True,text=True,timeout=180)
        if r.returncode:raise RuntimeError(r.stdout+r.stderr)
        return r.stdout+r.stderr
    if 'product: Darklighter' not in run('getvar','product'):raise ValueError('wrong Fastboot board')
    for label in ['system_b','boot_b','boot_a']:
        run('flash',label,w/'partition-backups'/meta[label]['file'])
    # AVB was not changed by this installer; archived originals are not reflashed.
    run('--set-active='+a.active_slot)
    print('Boot/recovery originals restored; not rebooted. Erased data is NOT reconstructed.\n'
          'The old OS may require its separate complete restore image/data backup.')


if __name__=='__main__':main()
