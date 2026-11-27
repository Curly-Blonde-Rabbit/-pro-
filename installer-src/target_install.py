#!/usr/bin/python3
"""DLT-A0 128GB installer target. Default audits/builds task files, never flashes.

Block-device operations require the personalized RAM install session AND the
explicit erase flag. Neither live Android nor an installed Linux root is accepted.
"""
import argparse,base64,fcntl,hashlib,json,os,pathlib,re,shutil,stat,struct,subprocess,sys,tarfile,time,uuid

SIZE=3221225472
COMPONENTS=(('ramdump','sda17',116733718528),('system_a','sda6',3221225472),
            ('vendor_a','sda8',1073741824),('vendor_b','sda9',1073741824))
TOTAL=sum(x[2] for x in COMPONENTS)


def run(a,t=180):
    a=[str(x) for x in a];env=None
    if a[0]=='dmsetup' and not shutil.which('dmsetup'):
        a[0]='/opt/dlt-main-pool-tools/sbin/dmsetup'
        env=dict(os.environ,LD_LIBRARY_PATH='/opt/dlt-main-pool-tools/lib/aarch64-linux-gnu:/opt/dlt-main-pool-tools/usr/lib/aarch64-linux-gnu')
    p=subprocess.run(a,env=env,capture_output=True,text=True,timeout=t)
    if p.returncode:raise RuntimeError([a,p.returncode,p.stderr[-1800:],p.stdout[-1800:]])
    return p.stdout.strip()


def sha(p):
    h=hashlib.sha256()
    with pathlib.Path(p).open('rb') as f:
        for b in iter(lambda:f.read(4<<20),b''):h.update(b)
    return h.hexdigest()


def write(root,rel,data,mode=0o600):
    p=root/rel
    for a in (p,*p.parents):
        if a==root.parent:break
        if a.is_symlink():raise ValueError('symlink target: '+rel)
    p.parent.mkdir(parents=True,exist_ok=True)
    q=p.with_name('.install-'+p.name)
    with q.open('xb') as f:
        f.write(data if isinstance(data,bytes) else data.encode());f.flush();os.fsync(f.fileno())
    q.chmod(mode);os.replace(q,p)


def jsonfile(root,rel,value,mode=0o600):
    write(root,rel,json.dumps(value,sort_keys=True,indent=2)+'\n',mode)


def safe_archive(archive):
    with tarfile.open(archive) as t:
        for m in t:
            p=pathlib.PurePosixPath(m.name)
            if p.is_absolute() or '..' in p.parts or m.ischr() or m.isblk() or m.isfifo():
                raise ValueError('unsafe archive member: '+m.name)
            if m.islnk():
                link=pathlib.PurePosixPath(m.linkname)
                if link.is_absolute() or '..' in link.parts:raise ValueError('unsafe archive hardlink')
            # Standard rootfs symlinks are retained, but never allow a file
            # below a previous symlink during archive extraction.
        members=t.getmembers();links={str(pathlib.PurePosixPath(m.name)) for m in members if m.issym()}
        for m in members:
            if any(str(p) in links for p in pathlib.PurePosixPath(m.name).parents):
                raise ValueError('archive traverses symlink: '+m.name)


def extract(archive,dest):
    safe_archive(archive)
    run(['tar','--numeric-owner','-xpf',archive,'-C',dest],240)


def validate_config(c):
    if c.get('schema')!=1 or c.get('board')!='DLT-A0':raise ValueError('configuration board/schema')
    a=c.get('owner_public_key','').strip().split()
    if len(a)<2 or a[0]!='ssh-ed25519':raise ValueError('new owner Ed25519 key required')
    wire=base64.b64decode(a[1],validate=True)
    if len(wire)!=51 or wire[:19]!=b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20':raise ValueError('key wire format')
    from dlt_module_state import validate
    validate('wifi',c.get('wifi'))
    for k in ['data_uuid','recovery_uuid','system_a_uuid','system_b_uuid']:uuid.UUID(c[k])
    for k in ['recovery_id','system_a_id','system_b_id']:
        if not re.fullmatch('[a-z0-9-]{20,96}',c[k]):raise ValueError('invalid root identity')
    if not c['recovery_id'].startswith('dlt-linux-stage1-'):raise ValueError('recovery ID prefix')
    return c


def activate_unit(root,name,target='multi-user.target'):
    p=root/'etc/systemd/system'/name
    if p.is_symlink():
        if os.readlink(p)!='/dev/null':raise ValueError('unexpected service symlink')
        p.unlink()
    if not p.exists():raise ValueError('missing unit '+name)
    link=root/'etc/systemd/system'/(target+'.wants')/name
    link.parent.mkdir(parents=True,exist_ok=True)
    if link.is_symlink():link.unlink()
    elif link.exists():raise ValueError('nonlink service enable collision')
    link.symlink_to('/etc/systemd/system/'+name)


def make_plan(config):
    return {'schema':1,'storage_mode':'dm-linear-v1','data_uuid':config['data_uuid'],
            'data_bytes':TOTAL,'physical_data_partition':'ramdump','recovery_partition':'system_b',
            'production_data_directory':'data','staging_capacity_only':False,
            'data_reset_policy':'Replace inactive image only; never format business data for reset.',
            'slots':{name:{'file':'systems/'+name+'.ext4','bytes':SIZE,'root_id':config[name.replace('-','_')+'_id'],
               'filesystem_uuid':config[name.replace('-','_')+'_uuid'],'initial_sha256':'',
               'baseline':'not-installed','mounted':False,'regular_file_on_physical_ext4':True}
               for name in ['system-a','system-b']}}


def configure_seed(seed,c):
    # The clean template has no host key or owner. One new host identity is
    # shared by this installation's A/B/rescue, never shared across installations.
    keyfile=seed/'etc/dlt-public/install-owner.pub'
    write(seed,'etc/dlt-public/install-owner.pub',c['owner_public_key'].strip()+'\n')
    from personalize_offline_root import personalize
    personalize(seed,keyfile);keyfile.unlink()
    # Password locks must not disable public-key-only ops on this Ubuntu/PAM setup.
    # Root/password login remain disabled in the SSH daemon configuration.
    seedkey=seed/'home/ops/.ssh/authorized_keys';assert seedkey.stat().st_uid==1000
    # A last-change date of zero means "force password change" even for a
    # valid SSH key under PAM. Keep password locked, but do not expire the owner.
    shadow=(seed/'etc/shadow').read_text().splitlines();out=[]
    for line in shadow:
        a=line.split(':')
        if a[0]=='ops':a[1]='!';a[2]=str(int(time.time()//86400));a[4]='99999';a[7]=''
        out.append(':'.join(a))
    write(seed,'etc/shadow','\n'.join(out)+'\n',0o640)
    for name in ['dlt-system-console.service','dlt-wifi-managed.service','dlt-connection-auto.service',
                 'dlt-auto-health.service','dlt-boot-deadline.service','dlt-boot-deadline.timer',
                 'dlt-ssh-exposure.service','dlt-fixed-network.service',
                 'dlt-fixed-network-owner-guard.service','dlt-fixed-network-owner-guard.timer']:
        p=seed/'etc/systemd/system'/name
        if p.is_symlink() and os.readlink(p)=='/dev/null':p.unlink()
    # Restore only the audited unit definitions, not identity restoration files.
    for name in ['dlt-system-console.service','dlt-wifi-managed.service','dlt-connection-auto.service',
                 'dlt-auto-health.service','dlt-boot-deadline.service','dlt-boot-deadline.timer',
                 'dlt-ssh-exposure.service','dlt-fixed-network.service',
                 'dlt-fixed-network-owner-guard.service','dlt-fixed-network-owner-guard.timer']:
        source=PAYLOAD/'units'/name
        write(seed,'etc/systemd/system/'+name,source.read_bytes(),0o644)
    for name in ['dlt-system-console.service','dlt-connection-auto.service','dlt-oem-wlan.service','dlt-ssh-exposure.service']:
        activate_unit(seed,name)
    # The baseline requires Wi-Fi to confirm production health. Requiring its
    # credentials in the local install config avoids a misleading offline pass.
    if not c['wifi']['networks']:raise ValueError('initial Wi-Fi required by baseline boot health')
    write(seed,'etc/dlt-server/wifi.conf','ctrl_interface=/run/wpa_supplicant\nupdate_config=0\n')
    jsonfile(seed,'etc/dlt-fixed-network/daemon.json',{'version':'alpha0','locked':False,'enabled':True,
        'acceptDNS':False,'acceptRoutes':False,'netfilterMode':'off','shieldsUp':True,
        'runSSHServer':False,'runWebClient':False,'advertiseRoutes':[],
        'advertiseExitNode':False,'exitNode':'','postureChecking':False,'remoteConfig':False,
        'autoUpdate':{'check':False,'apply':False}})
    jsonfile(seed,'etc/dlt-public/layout-accepted.json',{'schema':1,'prepared_by':'public-installer','cold_boot_tested':False})
    for rel in ['var/lib/dlt-recovery','var/lib/dlt-server','var/lib/dlt-clock','mnt/dlt-store','srv']:
        (seed/rel).mkdir(parents=True,exist_ok=True)


def build_images(work,config,payload):
    global PAYLOAD
    PAYLOAD=pathlib.Path(payload).resolve();work=pathlib.Path(work)
    if not work.is_absolute() or work.is_symlink() or work.exists():raise ValueError('new absolute work directory required')
    work.mkdir(mode=0o700);(work/'.public-installer-task').write_text('file-images-only\n')
    seed=work/'seed';seed.mkdir();extract(PAYLOAD/'system-rootfs.tar.gz',seed)
    extract(PAYLOAD/'recovery-support.tar.gz',seed)
    sys.path.insert(0,str(seed/'usr/local/lib/dlt-modules'))
    c=validate_config(config);plan=make_plan(c)
    configure_seed(seed,c)
    units=seed/'etc/systemd/system'
    manifests=[]
    for role in ['system-a','system-b','recovery']:
        identifier=c['recovery_id'] if role=='recovery' else c[role.replace('-','_')+'_id']
        fs_uuid=c['recovery_uuid'] if role=='recovery' else c[role.replace('-','_')+'_uuid']
        write(seed,'etc/dlt-server/native-root-id',identifier+'\n')
        write(seed,'etc/fstab','UUID='+fs_uuid+' / ext4 defaults,noatime,nodev 0 0\n',0o644)
        prole=seed/'etc/dlt-server/production-role.json'
        if role=='recovery':
            if prole.exists():prole.unlink()
            prod=units/'multi-user.target.wants/dlt-wifi-managed.service'
            if prod.is_symlink():prod.unlink()
            activate_unit(seed,'dlt-recovery-wifi.service')
            # Production auto-connect refers to /srv; recovery has a separate store.
            l=units/'multi-user.target.wants/dlt-connection-auto.service'
            if l.is_symlink():l.unlink()
            profile=seed/'var/lib/dlt-recovery/network'
            profile.mkdir(mode=0o700,exist_ok=True)
            init_wifi(profile,c['wifi'],fs_uuid)
            jsonfile(seed,'opt/dlt-recovery/local-baseline.json',{'schema':1,'recovery_root_id':identifier,
                     'factory_reset_available':False,'reason':'fresh deployment has no historical private factory image'})
            factory=(json.dumps(c['wifi'],sort_keys=True,separators=(',',':'))+'\n').encode()
            write(seed,'opt/dlt-recovery/network-factory.json',factory)
            write(seed,'opt/dlt-recovery/network-factory.backup.json',factory)
            jsonfile(seed,'opt/dlt-recovery/network-factory-manifest.json',
                     {'schema':1,'sha256':hashlib.sha256(factory).hexdigest()})
            jsonfile(seed,'opt/dlt-recovery/production-layout.json',plan)
            sys.path.insert(0,str(seed/'usr/local/lib/dlt-recovery'))
            from dlt_boot_state import Store
            Store(seed/'var/lib/dlt-recovery/boot-state').stage('system-a',{'system-a','system-b'})
        else:
            jsonfile(seed,'etc/dlt-server/production-role.json',{'slot':role,'root_id':identifier,'data_uuid':c['data_uuid']})
            activate_unit(seed,'dlt-wifi-managed.service')
        img=work/(role+'.ext4')
        with img.open('xb') as f:f.truncate(SIZE)
        # mke2fs is only ever called on this newly created task file here.
        run(['mke2fs','-q','-t','ext4','-F','-b','4096','-m','0','-U',fs_uuid,
             '-E','lazy_itable_init=0,lazy_journal_init=0','-d',seed,img],300)
        run(['e2fsck','-fn',img],180)
        digest=sha(img)
        manifests.append({'role':role,'bytes':SIZE,'sha256':digest,'filesystem_uuid':fs_uuid})
        if role!='recovery':plan['slots'][role]['initial_sha256']=digest
    write(work,'production-layout.json',json.dumps(plan,sort_keys=True,indent=2)+'\n')
    public={k:config[k] for k in ['data_uuid','recovery_uuid','system_a_uuid','system_b_uuid','recovery_id','system_a_id','system_b_id']}
    write(work,'boot-layout.json',json.dumps(public,indent=2)+'\n')
    report={'schema':1,'images':manifests,'layout':plan,'boot_layout':public,
            'new_owner_identity_created':True,'private_keys_returned_to_host':False,'block_devices_formatted':False,
            'configuration_sha256':hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()}
    write(work,'build-result.json',json.dumps(report,indent=2)+'\n')
    return report


def init_wifi(root,config,fs_uuid):
    from dlt_module_state import Store
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    jsonfile(root,'.dlt-state-volume.json',{'schema':1,'filesystem_uuid':fs_uuid})
    s=Store(root,'wifi',True);g=s.stage(config)
    # A candidate is confirmed by the real WPA service on its first association.
    return g


def geometry():
    from dlt_main_pool import gpt_entries,device_size
    disk=gpt_entries();expected={dev:(label,size) for label,dev,size in COMPONENTS}
    expected['sda7']=('system_b',SIZE)
    for dev,(label,size) in expected.items():
        if disk.get(dev,{}).get('label')!=label or disk[dev]['bytes']!=size:
            raise ValueError('unsupported partition geometry/labels: '+dev)
        p=pathlib.Path('/dev')/dev
        if p.is_symlink() or not p.is_block_device() or device_size(p)!=size:raise ValueError('block node mismatch')
    return disk


def require_ram_installer():
    p=pathlib.Path('/run/dlt-public-install-session')
    if not p.is_file() or p.read_text()!='public-installer-session-v1\n':raise ValueError('not personalized RAM install session')
    mounts=pathlib.Path('/proc/mounts').read_text().splitlines()
    if any(line.split()[0] in ['/dev/'+x[1] for x in COMPONENTS] or line.split()[0]=='/dev/sda7' for line in mounts):
        raise ValueError('target partitions still mounted')
    for dm in pathlib.Path('/sys/class/block').glob('dm-*'):
        if list((dm/'slaves').glob('sda*')):raise ValueError('target disk has an active mapping')
    # This package does not change AVB, unlock or relock a bootloader. Persisted
    # Linux boot requires both existing vbmeta images to have verification off.
    for label in ['vbmeta_a','vbmeta_b']:
        with open('/dev/disk/by-partlabel/'+label,'rb') as f:header=f.read(256)
        if len(header)!=256 or header[:4]!=b'AVB0' or not (struct.unpack_from('>I',header,120)[0]&2):
            raise ValueError('existing AVB verification-off prerequisite not met: '+label)


def install(work,config):
    require_ram_installer();disk=geometry();work=pathlib.Path(work)
    r=json.loads((work/'build-result.json').read_text());plan=r['layout']
    if r['configuration_sha256']!=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest():
        raise ValueError('configuration changed after image construction')
    for item in r['images']:
        if sha(work/(item['role']+'.ext4'))!=item['sha256']:raise ValueError('built image changed')
    # No GPT/vbmeta/boot/NV writes here. Host commits boot B only after a
    # successful readback; the prior boot A is retained for emergency maintenance.
    table=[];offset=0;segments=[]
    for label,dev,size in COMPONENTS:
        number=(pathlib.Path('/sys/class/block')/dev/'dev').read_text().strip()
        if not re.fullmatch('[0-9]+:[0-9]+',number):raise ValueError('bad device number')
        table.append(f'{offset} {size//512} linear {number} 0');offset+=size//512
        segments.append({'label':label,'device':dev,'bytes':size,'start_sector512':disk[dev]['start_sector512'],
                         'partuuid':disk[dev]['partuuid']})
    # This name is independent from production; creation is preceded by all
    # identity/mount checks and explicit host --erase-linux-targets confirmation.
    run(['dmsetup','--noudevsync','--noudevrules','create','dlt-public-install','--table','\n'.join(table)])
    node=pathlib.Path('/dev/mapper/dlt-public-install')
    if not node.exists():
        names=[p for p in pathlib.Path('/sys/class/block').glob('dm-*') if (p/'dm/name').read_text().strip()=='dlt-public-install']
        if len(names)!=1:raise ValueError('new installer mapper not found')
        node=pathlib.Path('/dev')/names[0].name
    if node.stat().st_rdev==0:raise ValueError('invalid mapper node')
    mount=work/'pool';mount.mkdir()
    mounted=False
    try:
        # Explicit destructive phase: only the verified target's agreed main pool.
        run(['mke2fs','-q','-t','ext4','-F','-b','4096','-m','0','-U',config['data_uuid'],node],300)
        run(['mount','-o','rw,nodev,nosuid',node,mount]);mounted=True
        (mount/'systems').mkdir();(mount/'data').mkdir()
        jsonfile(mount,'.dlt-server-volume.json',{'schema':1,'uuid':config['data_uuid']})
        state=mount/'data/dlt-state';state.mkdir(mode=0o700)
        init_wifi(state,config['wifi'],config['data_uuid'])
        jsonfile(mount,'data/dlt-state/connection-policy.json',{'schema':1,'wifi_autoconnect':True,'bluetooth_autoconnect':True,'preserve_connected':True})
        jsonfile(mount,'data/dlt-state/ssh-exposure/policy.json',{'schema':1,'network_enabled':False,'usb_maintenance_exempt':True})
        for role in ['system-a','system-b']:
            src=work/(role+'.ext4');dest=mount/'systems'/(role+'.ext4')
            shutil.copyfile(src,dest);dest.chmod(0o600)
            if sha(dest)!=sha(src):raise ValueError('installed system readback mismatch')
        # Add machine-local GPT identity to the NEW recovery image only.
        rec=work/'recovery-root';rec.mkdir();rec_image=work/'recovery.ext4'
        loop=run(['losetup','--find','--show',rec_image]);rec_mounted=False
        try:
            if run(['losetup','-n','-O','BACK-FILE',loop])!=str(rec_image):raise ValueError('recovery loop backing mismatch')
            run(['mount','-o','rw,nodev,nosuid',loop,rec]);rec_mounted=True
            pool_plan={'schema':1,'board':'DLT-A0','name':'dlt-main','filesystem_uuid':config['data_uuid'],'segments':segments}
            jsonfile(rec,'etc/dlt-server/main-pool-layout.json',pool_plan)
            plan['linear_pool']={'pending':False,'plan_sha256':sha(rec/'etc/dlt-server/main-pool-layout.json')}
            jsonfile(rec,'opt/dlt-recovery/production-layout.json',plan)
            run(['umount',rec]);rec_mounted=False
        finally:
            if rec_mounted:run(['umount',rec])
            run(['losetup','-d',loop])
        run(['e2fsck','-fn',rec_image],180)
        # Copy recovery last, after the primary images/data readbacks succeeded.
        with rec_image.open('rb') as src,open('/dev/sda7','r+b',buffering=0) as dst:
            shutil.copyfileobj(src,dst,4<<20);os.fsync(dst.fileno())
        if sha(pathlib.Path('/dev/sda7'))!=sha(rec_image):raise ValueError('recovery partition readback mismatch')
        run(['sync'])
    finally:
        if mounted:run(['umount',mount])
        run(['dmsetup','--noudevsync','--noudevrules','remove','dlt-public-install'])
    result={'installed_main_and_recovery':True,'boot_not_written_by_target':True,
            'GPT_and_NV_unchanged':True,'layout':r['boot_layout'],'recovery_sha256':sha(rec_image)}
    write(work,'install-result.json',json.dumps(result,indent=2)+'\n')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['build','preflight','install'])
    p.add_argument('--work',required=True);p.add_argument('--payload',required=True)
    p.add_argument('--config',required=True);p.add_argument('--erase-linux-targets',action='store_true')
    a=p.parse_args();payload=pathlib.Path(a.payload)
    sys.path.insert(0,str(payload/'python'))
    c=json.loads(pathlib.Path(a.config).read_text())
    if a.action=='build':result=build_images(a.work,c,payload)
    elif a.action=='preflight':require_ram_installer();result={'geometry':geometry(),'no_writes':True}
    else:
        if not a.erase_linux_targets:p.error('install requires --erase-linux-targets')
        result=install(a.work,c)
    print(json.dumps(result,sort_keys=True))
