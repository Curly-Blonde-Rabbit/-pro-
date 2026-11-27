#!/usr/bin/env python3
"""Windows host installer for the explicitly supported DLT-A0 128GB layout.
No device action without --install --erase-linux-targets. --verify is offline.
"""
import argparse,ctypes,getpass,hashlib,json,os,pathlib,shlex,shutil,subprocess,sys,time,uuid
from boot_image import personalize,split

ROOT=pathlib.Path(__file__).resolve().parents[1]


def sha(path):
    h=hashlib.sha256()
    with pathlib.Path(path).open('rb') as f:
        for b in iter(lambda:f.read(4<<20),b''):h.update(b)
    return h.hexdigest()


def run(args,timeout=60,check=True):
    p=subprocess.run([str(x) for x in args],stdin=subprocess.DEVNULL,capture_output=True,timeout=timeout)
    r={'exit':p.returncode,'stdout':p.stdout.decode(errors='replace'),'stderr':p.stderr.decode(errors='replace')}
    if check and p.returncode:raise RuntimeError([str(args[0]),r])
    return r


def tool(name):
    choices=[ROOT/'platform-tools'/(name+'.exe'),ROOT/'platform-tools'/name]
    p=next((str(p) for p in choices if p.is_file()),None) or shutil.which(name)
    if not p:raise FileNotFoundError(name+' must be installed/in PATH or platform-tools/')
    return p


def verify():
    expected={}
    for line in (ROOT/'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        d,n=line.split('  ',1);p=pathlib.PurePosixPath(n)
        if p.is_absolute() or '..' in p.parts or n in expected:raise ValueError('unsafe manifest')
        expected[n]=d
    for n,d in expected.items():
        p=ROOT/n
        if p.is_symlink() or sha(p)!=d:raise ValueError('payload mismatch: '+n)
    release=json.loads((ROOT/'release.json').read_text(encoding='utf-8'))
    if release.get('board')!='DLT-A0' or release.get('generic_other_boards_supported') is not False:
        raise ValueError('wrong installation release')
    h,k,e=split((ROOT/'payload/boot-template.img').read_bytes())
    if 'root/.ssh/authorized_keys' in e or any(n.startswith('etc/dropbear/dropbear_') for n in e):
        raise ValueError('publisher identity in generic boot template')
    return {'files_verified':len(expected),'version':release['version'],
            'hardware_installation_tested':release['hardware_installation_tested'],
            'no_device_action':True}


class Installer:
    def __init__(self,args):
        self.a=args;self.work=pathlib.Path(args.workspace).absolute()
        if self.work.exists():raise ValueError('choose a NEW workspace; it will contain your private identity and backups')
        self.work.mkdir(parents=True);self.serial=args.serial
        self.fastboot=tool('fastboot');self.ssh=tool('ssh');self.keygen=tool('ssh-keygen')
        self.key=self.work/'owner_ed25519';self.bootstrap_known=self.work/'bootstrap_known_hosts'
        self.ops_known=self.work/'server_known_hosts'
        self.config=None;self.last_phase='prepared';self.log=[]

    def record(self,phase,details=None):
        self.last_phase=phase
        self.log.append({'phase':phase,'time':time.time(),'details':details})
        (self.work/'progress.json').write_text(json.dumps(self.log,indent=2),encoding='utf-8')
        print(phase,flush=True)

    def fb(self,*a,timeout=60,check=True):
        return run([self.fastboot,'-s',self.serial,*a],timeout,check)

    def wait_fastboot(self):
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            r=run([self.fastboot,'devices'],10)
            if any(x.split()[:1]==[self.serial] for x in r['stdout'].splitlines()):return
            time.sleep(2)
        raise TimeoutError('selected device did not enter Fastboot')

    def ssh_args(self,command,ops=False):
        return [self.ssh,'-T','-F','NUL','-i',self.key,'-o','IdentitiesOnly=yes','-o','BatchMode=yes',
          '-o','ConnectTimeout=5','-o','ServerAliveInterval=5','-o','ServerAliveCountMax=6',
          '-o','StrictHostKeyChecking='+('yes' if ops else 'accept-new'),
          '-o','UserKnownHostsFile='+str(self.ops_known if ops else self.bootstrap_known),
          ('ops' if ops else 'root')+'@172.31.255.1',command]

    def ssh_run(self,command,ops=False,timeout=60):
        return run(self.ssh_args(command,ops),timeout)

    def upload(self,local,remote,timeout=240):
        with pathlib.Path(local).open('rb') as f:
            p=subprocess.run([str(x) for x in self.ssh_args('umask 077; cat > '+shlex.quote(remote))],
                             stdin=f,capture_output=True,timeout=timeout)
        if p.returncode:raise RuntimeError('upload failed: '+pathlib.Path(local).name)
        actual=self.ssh_run('/dlt/bin/busybox sha256sum '+shlex.quote(remote))['stdout'].split()[0]
        if actual!=sha(local):raise ValueError('upload checksum mismatch')

    def download(self,remote,local,timeout=600):
        with pathlib.Path(local).open('xb') as f:
            p=subprocess.run([str(x) for x in self.ssh_args('cat '+shlex.quote(remote))],
                             stdin=subprocess.DEVNULL,stdout=f,stderr=subprocess.PIPE,timeout=timeout)
        if p.returncode:raise RuntimeError('download failed')

    def connect_usb(self,ops=False):
        deadline=time.monotonic()+150
        while time.monotonic()<deadline:
            try:
                r=run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',
                       ROOT/'tools/usb_link.ps1','-Workspace',self.work],20)
                self.ssh_run('id -u',ops,15);return
            except (RuntimeError,subprocess.TimeoutExpired):time.sleep(2)
        raise TimeoutError('USB SSH not ready; existing workspace preserves recovery materials')

    def chroot_command(self,cmd):
        return '/dlt/bin/busybox chroot /run/dlt-public-builder '+cmd

    def reboot_bootloader(self,ops=False):
        # RESTART2 magic string, executed by the selected owner's root context.
        code="import ctypes; l=ctypes.CDLL(None,use_errno=True); l.syscall(142,0xfee1dead,672274793,0xa1b2c3d4,ctypes.c_char_p(b'bootloader')); raise OSError(ctypes.get_errno())"
        inner=('/usr/bin/python3 -c '+shlex.quote(code))
        if not ops:inner=self.chroot_command(inner)
        else:inner='sudo -n '+inner
        detached="(sleep 2; "+inner+") >/dev/null 2>&1 </dev/null &"
        self.ssh_run('/bin/sh -c '+shlex.quote(detached),ops,15)
        self.wait_fastboot()

    def execute(self):
        verify()
        if os.name!='nt' or not ctypes.windll.shell32.IsUserAnAdmin():
            raise PermissionError('Run in an elevated Windows terminal; only the dedicated USB adapter receives a temporary IP')
        self.wait_fastboot()
        for name,value in [('product','Darklighter'),('unlocked','yes')]:
            r=self.fb('getvar',name)
            if name+': '+value not in r['stdout']+r['stderr']:raise ValueError('Fastboot '+name+' mismatch')
        slot=self.fb('getvar','current-slot')
        (self.work/'fastboot-before.json').write_text(json.dumps(slot,indent=2),encoding='utf-8')
        self.record('fastboot-product-and-unlocked-verified')
        run([self.keygen,'-q','-t','ed25519','-N','','-f',self.key])
        pub=self.key.with_suffix('.pub').read_text().strip()
        ssid=self.a.ssid or input('Initial Wi-Fi SSID: ').strip()
        password=getpass.getpass('Wi-Fi password (stored only in your private installation workspace): ')
        if not 1<=len(ssid.encode())<=32 or not (8<=len(password)<=63 and all(32<=ord(x)<=126 for x in password)):
            raise ValueError('invalid initial Wi-Fi configuration')
        c={'schema':1,'board':'DLT-A0','owner_public_key':pub,
           'wifi':{'schema':1,'networks':[{'ssid':ssid,'psk':password}]},
           **{k:str(uuid.uuid4()) for k in ['data_uuid','recovery_uuid','system_a_uuid','system_b_uuid']},
           'recovery_id':'dlt-linux-stage1-'+uuid.uuid4().hex,
           'system_a_id':'dlt-production-system-a-'+uuid.uuid4().hex,
           'system_b_id':'dlt-production-system-b-'+uuid.uuid4().hex}
        self.config=c
        cfg=self.work/'config.private.json';cfg.write_text(json.dumps(c),encoding='utf-8')
        ramboot=self.work/'installer-personalized.private.img'
        template=(ROOT/'payload/boot-template.img').read_bytes()
        ramboot.write_bytes(personalize(template,pub,{},install=True))
        self.fb('boot',ramboot,timeout=120);self.connect_usb()
        marker=self.ssh_run('cat /run/dlt-public-install-session')['stdout'].strip()
        if marker!='public-installer-session-v1':raise ValueError('wrong RAM boot session')
        self.record('personalized-RAM-installer-USB-ready')
        self.ssh_run('mkdir -m 700 -p /run/dlt-public-payload /run/dlt-public-builder /run/dlt-public-output')
        for name in ['system-rootfs.tar.gz','recovery-support.tar.gz']:
            self.upload(ROOT/'payload'/name,'/run/dlt-public-payload/'+name)
        self.ssh_run('/dlt/bin/busybox tar -xzf /run/dlt-public-payload/system-rootfs.tar.gz -C /run/dlt-public-builder && '
                     '/dlt/bin/busybox tar -xzf /run/dlt-public-payload/recovery-support.tar.gz -C /run/dlt-public-builder',timeout=240)
        # Small install-time Python/modules/units come from a separate checked tar.
        self.upload(ROOT/'payload/installer-support.tar.gz','/run/dlt-public-payload/installer-support.tar.gz')
        self.ssh_run('/dlt/bin/busybox tar -xzf /run/dlt-public-payload/installer-support.tar.gz -C /run/dlt-public-output')
        for name in ['dev','sys','proc']:
            self.ssh_run('/dlt/bin/busybox mount -o bind /'+name+' /run/dlt-public-builder/'+name)
        self.ssh_run('mkdir -p /run/dlt-public-builder/work && /dlt/bin/busybox mount -o bind /run/dlt-public-output /run/dlt-public-builder/work')
        self.ssh_run('mkdir -p /run/dlt-public-builder/run; cp /run/dlt-public-install-session /run/dlt-public-builder/run/')
        # Payload bytes remain in RAM outside the builder root; expose only that
        # dedicated directory, never an arbitrary Android/data filesystem.
        self.ssh_run('mkdir -p /run/dlt-public-builder/payload; /dlt/bin/busybox mount -o bind /run/dlt-public-payload /run/dlt-public-builder/payload')
        self.upload(cfg,'/run/dlt-public-output/config.private.json')
        base='/usr/bin/python3 -I /work/tools/target_install.py '
        common=' --work /work/images --payload /payload --config /work/config.private.json'
        # Copy the checked small payload children needed by target_install.
        self.ssh_run('cp -a /run/dlt-public-output/payload/python /run/dlt-public-payload/; cp -a /run/dlt-public-output/payload/units /run/dlt-public-payload/')
        pre=self.ssh_run(self.chroot_command(base+'preflight'+common))
        (self.work/'geometry-before.json').write_text(pre['stdout'],encoding='utf-8')
        self.record('RAM-session-and-GPT-geometry-checked-no-format-yet')
        if shutil.disk_usage(self.work).free<4*1024**3:raise OSError('need at least 4 GiB PC backup space')
        backups=self.work/'partition-backups';backups.mkdir()
        backup_manifest={}
        for label in ['boot_a','boot_b','system_b','vbmeta_a','vbmeta_b']:
            remote='/dev/disk/by-partlabel/'+label;dest=backups/(label+'.img')
            expected=self.ssh_run('/dlt/bin/busybox sha256sum '+remote,timeout=120)['stdout'].split()[0]
            self.download(remote,dest)
            if sha(dest)!=expected:raise ValueError('partition backup checksum mismatch: '+label)
            backup_manifest[label]={'file':dest.name,'bytes':dest.stat().st_size,'sha256':expected}
        (self.work/'partition-backups.json').write_text(json.dumps(backup_manifest,indent=2),encoding='utf-8')
        self.record('boot-and-recovery-partition-backups-verified',
                    {'main_pool_not_backed_up':'erased only because --erase-linux-targets explicitly selected'})
        self.ssh_run(self.chroot_command(base+'build'+common),timeout=1200)
        built=json.loads(self.ssh_run('cat /run/dlt-public-output/images/build-result.json')['stdout'])
        (self.work/'build-result.json').write_text(json.dumps(built,indent=2),encoding='utf-8')
        hostpub=self.ssh_run('cat /run/dlt-public-output/images/seed/etc/ssh/ssh_host_ed25519_key.pub')['stdout'].strip().split()
        if hostpub[:1]!=['ssh-ed25519'] or len(hostpub)<2:raise ValueError('new server host identity missing')
        self.ops_known.write_text('172.31.255.1 '+hostpub[0]+' '+hostpub[1]+'\n',encoding='utf-8')
        self.record('new-owner-A-B-rescue-file-images-built')
        self.ssh_run(self.chroot_command(base+'install'+common+' --erase-linux-targets'),timeout=1200)
        result=json.loads(self.ssh_run('cat /run/dlt-public-output/images/install-result.json')['stdout'])
        (self.work/'install-result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        prod=self.work/'server-personalized.private.img'
        prod.write_bytes(personalize(template,pub,result['layout']))
        self.record('new-systems-and-recovery-written-and-readback-verified-boot-uncommitted')
        self.reboot_bootloader()
        # Temporary native boot must pass before boot partitions are committed.
        self.fb('boot',prod,timeout=120);self.connect_usb(ops=True)
        self.wait_health()
        self.record('temporary-new-server-boot-health-passed')
        self.reboot_bootloader(ops=True)
        self.fb('flash','boot_b',prod,timeout=120)
        self.fb('--set-active=b');self.fb('reboot');self.connect_usb(ops=True)
        healthy=self.wait_health()
        self.record('installed-native-server-B-confirmed-A-still-original',json.loads(healthy['stdout']))
        self.reboot_bootloader(ops=True)
        self.fb('flash','boot_a',prod,timeout=120)
        self.fb('--set-active=b');self.fb('reboot');self.connect_usb(ops=True)
        healthy=self.wait_health()
        self.record('installed-native-server-both-boot-slots-confirmed',json.loads(healthy['stdout']))
        print('Owner key and partition backups are in '+str(self.work)+'; keep PRIVATE.')
        print('USB login: ssh -i "'+str(self.key)+'" -o UserKnownHostsFile="'+str(self.ops_known)+'" ops@172.31.255.1')

    def wait_health(self):
        end=time.monotonic()+180;last='health not ready'
        while time.monotonic()<end:
            try:return self.ssh_run('sudo -n /usr/local/sbin/dlt-health-confirm --confirm',True,timeout=25)
            except (RuntimeError,subprocess.TimeoutExpired) as exc:last=str(exc)[-1000:];time.sleep(5)
        raise TimeoutError(last)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--verify',action='store_true');p.add_argument('--install',action='store_true')
    p.add_argument('--erase-linux-targets',action='store_true')
    p.add_argument('--allow-unvalidated-install',action='store_true',
                   help='explicitly accept that this packaging build has not passed a complete device installation trial')
    p.add_argument('--serial');p.add_argument('--workspace');p.add_argument('--ssid')
    a=p.parse_args()
    if not a.install:print(json.dumps(verify(),indent=2));return
    if not a.erase_linux_targets or not a.serial or not a.workspace:
        p.error('--install requires --erase-linux-targets --serial and a NEW --workspace')
    release=json.loads((ROOT/'release.json').read_text(encoding='utf-8'))
    if not release.get('hardware_installation_tested') and not a.allow_unvalidated_install:
        p.error('candidate has no full-device install acceptance; default is verification only. Explicit --allow-unvalidated-install is required to run the candidate installer.')
    Installer(a).execute()


if __name__=='__main__':main()
