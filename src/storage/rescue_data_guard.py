#!/usr/bin/python3 -I
"""Check already mounted owned business volume before a rescue-only bind mount."""
import pathlib,json,subprocess,os
def validate(layout,marker,mount,device,size):
    if marker.get('uuid')!=layout.get('data_uuid') or mount.get('uuid')!=layout.get('data_uuid') or mount.get('fstype')!='ext4':raise ValueError('recovery data identity mismatch')
    if mount.get('target')!='/mnt/dlt-store' or device!='/dev/sda17' or (size not in (10737418240,116733718528) or size not in (layout.get('data_bytes'),layout.get('pending_data_bytes'))):raise ValueError('unexpected recovery data mount')
    return True
def main():
    if os.geteuid()!=0:raise PermissionError('root required')
    layout=json.loads(pathlib.Path('/opt/dlt-recovery/production-layout.json').read_text());base=pathlib.Path('/mnt/dlt-store')
    for p in (base,base/'data',base/'.dlt-server-volume.json'):
        if p.is_symlink():raise ValueError('symlink data path')
    marker=json.loads((base/'.dlt-server-volume.json').read_text())
    text=subprocess.check_output(['findmnt','--json','--output','SOURCE,FSTYPE,UUID,TARGET','--mountpoint',str(base)],text=True,timeout=5)
    mount=json.loads(text)['filesystems'][0];device=str(pathlib.Path(mount['source'].split('[',1)[0]).resolve())
    size=int(subprocess.check_output(['blockdev','--getsize64',device],text=True,timeout=5))
    if 'linear_pool' in layout:
        import sys
        sys.path.insert(0,'/usr/local/lib/dlt-recovery')
        from dlt_main_pool import validate_mount
        if marker.get('uuid')!=layout.get('data_uuid'):raise ValueError('recovery marker mismatch')
        validate_mount(layout,mount,'/mnt/dlt-store','/')
    else:validate(layout,marker,mount,device,size)
    if not (base/'data').is_dir():raise ValueError('business data directory missing')
    print(json.dumps({'owned_data_mount_verified':True,'format_requested':False}))
if __name__=='__main__':main()
