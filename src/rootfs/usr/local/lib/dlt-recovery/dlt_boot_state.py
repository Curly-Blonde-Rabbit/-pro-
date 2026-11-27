#!/usr/bin/python3
"""Local boot transactions. A new version is never good merely because it exists.

Two checksummed state copies, monotonic generation, write/fsync/rename/fsync,
serialized changes, finite trials, boot-ID-bound confirmation. This module does
not flash, reboot, format, or execute commands from the state file.
"""
import argparse
import contextlib
import hashlib
import json
import os
import pathlib
import re
import stat
import tempfile
try:
    import fcntl
except ImportError:
    fcntl = None

SLOTS = ('recovery', 'system-a', 'system-b')
MAX_BYTES = 16384


def encode(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()


def fsync_dir(path):
    if os.name == 'nt':
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, obj):
    fd, name = tempfile.mkstemp(prefix='.boot-txn-', dir=path.parent)
    try:
        if hasattr(os, 'fchmod'):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(encode(obj) + b'\n'); f.flush(); os.fsync(f.fileno())
        os.replace(name, path); fsync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            raise ValueError('Invalid state file type/size')
        obj = json.loads(os.read(fd, MAX_BYTES+1))
    finally:
        os.close(fd)
    p = obj['payload']
    if hashlib.sha256(encode(p)).hexdigest() != obj['sha256']:
        raise ValueError('State checksum mismatch')
    # Verify the old payload checksum before migrating it in memory.
    if p.get('schema') == 1:
        if set(p) != set(initial()) - {'boot_slot','boot_id','boot_healthy'}:
            raise ValueError('Legacy schema mismatch')
        p=dict(p,schema=2,boot_slot=None,boot_id=None,boot_healthy=True)
    validate(p)
    return p


def initial():
    return {'schema':2, 'seq':0, 'good':'recovery', 'previous':None,
            'candidate':None, 'attempts':0, 'trial_boot':None,
            'request':'none', 'rejected':[], 'reason':'initial-recovery-only',
            'boot_slot':None,'boot_id':None,'boot_healthy':True}


def validate(s):
    if set(s) != set(initial()) or type(s['schema']) is not int or s['schema'] != 2:
        raise ValueError('State schema mismatch')
    if type(s['seq']) is not int or not 0 <= s['seq'] < 2**53:
        raise ValueError('State generation invalid')
    for k in ('good','previous','candidate'):
        if s[k] not in SLOTS + ((None,) if k != 'good' else ()):
            raise ValueError('Invalid slot')
    if type(s['attempts']) is not int or not 0 <= s['attempts'] <= 2:
        raise ValueError('Invalid trial count')
    if s['trial_boot'] is not None and not re.fullmatch('[a-zA-Z0-9-]{1,64}', s['trial_boot']):
        raise ValueError('Invalid boot identity')
    if s['request'] not in ('none','recovery') or not isinstance(s['rejected'],list) or len(s['rejected']) > 32:
        raise ValueError('Invalid request/rejection history')
    if any(x not in SLOTS for x in s['rejected']) or not re.fullmatch('[a-z0-9-]{1,96}',s['reason']):
        raise ValueError('Invalid state details')
    if s['boot_slot'] not in (*SLOTS,None) or type(s['boot_healthy']) is not bool:
        raise ValueError('Invalid boot lease')
    if (s['boot_slot'] is None) != (s['boot_id'] is None):
        raise ValueError('Incomplete boot lease')
    if s['boot_id'] is not None and not re.fullmatch('[a-zA-Z0-9-]{1,64}',s['boot_id']):
        raise ValueError('Invalid lease identity')


class Store:
    def __init__(self, path):
        self.path = pathlib.Path(path).absolute()
        for p in (self.path, *self.path.parents):
            if p.is_symlink():
                raise ValueError('Symlink state path')
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != 'nt':
            st=self.path.stat()
            if st.st_uid != os.geteuid() or st.st_mode & 0o077:
                raise ValueError('State owner/mode rejected')

    @contextlib.contextmanager
    def locked(self):
        fd=os.open(self.path/'.lock',os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            if fcntl:fcntl.flock(fd,fcntl.LOCK_EX)
            yield
        finally:os.close(fd)

    def load(self):
        valid=[]
        for name in ('state.0.json','state.1.json'):
            try:valid.append(read(self.path/name))
            except (OSError,ValueError,TypeError,KeyError):pass
        if not valid:
            s=initial();s['reason']='state-unavailable-use-recovery';return s
        top=max(x['seq'] for x in valid)
        newest=[x for x in valid if x['seq']==top]
        if len(newest)==2 and newest[0] != newest[1]:
            s=initial();s['reason']='state-conflict-use-recovery';return s
        return newest[0]

    def save(self,s):
        validate(s);s=dict(s);s['seq']+=1
        validate(s)
        obj={'payload':s,'sha256':hashlib.sha256(encode(s)).hexdigest()}
        # Two complete copies of each committed generation. A cut between these
        # writes leaves one older generation and one newer valid generation.
        first=s['seq']%2
        atomic(self.path/('state.'+str(first)+'.json'),obj)
        atomic(self.path/('state.'+str(1-first)+'.json'),obj)
        return s

    def stage(self, slot, installed_slots):
        if slot not in ('system-a','system-b') or slot not in installed_slots:
            raise ValueError('Production slot is not installed/verified')
        with self.locked():
            s=self.load()
            if s['candidate'] or slot==s['good']:
                raise ValueError('Pending trial exists or slot is already good')
            s.update(candidate=slot,attempts=0,trial_boot=None,request='none',reason='candidate-staged')
            return self.save(s)

    def select(self, boot, healthy_slots):
        if not re.fullmatch('[a-zA-Z0-9-]{1,64}',boot):raise ValueError('Bad boot identity')
        with self.locked():
            s=self.load();chosen='recovery'
            failed_good=False
            # A known-good image can become broken later. Require a fresh
            # boot-ID-bound health receipt on EVERY boot, not just upgrades.
            if (s['boot_id'] not in (None,boot) and not s['boot_healthy'] and
                s['boot_slot']==s['good'] and s['good']!='recovery'):
                s['rejected']=(s['rejected']+[s['good']])[-32:]
                s.update(good=s['previous'] or 'recovery',previous=None,
                         reason='known-good-health-lease-expired')
                failed_good=True
            if s['request']=='recovery':
                s.update(request='none',reason='operator-requested-recovery')
            elif s['candidate']:
                c=s['candidate']
                if c not in healthy_slots:
                    s['reason']='candidate-image-rejected'
                elif s['trial_boot'] and s['trial_boot'] != boot:
                    s['reason']='unconfirmed-boot-rolled-back'
                elif s['attempts'] >= 2:
                    s['reason']='trial-budget-exhausted'
                else:
                    s.update(attempts=s['attempts']+1,trial_boot=boot,reason='candidate-trial')
                    chosen=c
                if chosen != c:
                    s['rejected']=(s['rejected']+[c])[-32:]
                    s.update(candidate=None,attempts=0,trial_boot=None)
                    chosen=s['good'] if s['good'] in healthy_slots else 'recovery'
            else:
                chosen=s['good'] if s['good'] in healthy_slots else 'recovery'
                if not failed_good:
                    s['reason']='known-good-selected' if chosen!='recovery' else 'recovery-selected'
            # Repeated selection within the same initramfs boot remains bounded
            # by the candidate budget and explicit mount failure handling.
            s.update(boot_slot=chosen,boot_id=boot,boot_healthy=False)
            s=self.save(s)
            out={'slot':chosen,'reason':s['reason'],'seq':s['seq'],'boot_id':boot}
            atomic(self.path/'last-selection.json',out)
            return out

    def confirm(self,slot,boot):
        with self.locked():
            s=self.load()
            if (s['candidate']!=slot or s['trial_boot']!=boot or not s['attempts'] or
                s['boot_slot']!=slot or s['boot_id']!=boot):
                raise ValueError('No matching live trial')
            s.update(previous=s['good'],good=slot,candidate=None,attempts=0,trial_boot=None,reason='health-confirmed',boot_healthy=True)
            return self.save(s)

    def healthy(self,slot,boot):
        if slot not in SLOTS:raise ValueError('Invalid healthy slot')
        with self.locked():
            s=self.load()
            if s['boot_slot']!=slot or s['boot_id']!=boot:
                raise ValueError('No matching current boot lease')
            if s['candidate']==slot:
                if s['trial_boot']!=boot or not s['attempts']:
                    raise ValueError('No matching live candidate')
                s.update(previous=s['good'],good=slot,candidate=None,attempts=0,trial_boot=None)
            elif slot!='recovery' and s['good']!=slot:
                raise ValueError('Unexpected production health receipt')
            s.update(boot_healthy=True,reason='boot-health-lease-confirmed')
            return self.save(s)

    def recovery(self):
        with self.locked():
            s=self.load();s.update(request='recovery',reason='recovery-request-recorded');return self.save(s)

    def fail(self,slot):
        if slot not in ('system-a','system-b'):raise ValueError('Invalid failed slot')
        with self.locked():
            s=self.load();s['rejected']=(s['rejected']+[slot])[-32:]
            if s['candidate']==slot:s.update(candidate=None,attempts=0,trial_boot=None)
            if s['good']==slot:s.update(good=s['previous'] or 'recovery',previous=None)
            s['reason']='root-mount-or-identity-rejected';return self.save(s)


def main():
    a=argparse.ArgumentParser();a.add_argument('action',choices=('status','select','request-recovery','stage','confirm','healthy','fail'))
    a.add_argument('--state',default='/var/lib/dlt-recovery/boot-state')
    a.add_argument('--boot-id');a.add_argument('--token-only',action='store_true')
    a.add_argument('--slot',choices=SLOTS)
    a.add_argument('--available-slot',choices=('system-a','system-b'),action='append',default=[])
    a.add_argument('--manifest',default='/opt/dlt-recovery/production-layout.json')
    a.add_argument('--store',default='/mnt/dlt-store');args=a.parse_args()
    s=Store(args.state)
    if args.action=='status':out=s.load()
    elif args.action=='request-recovery':out=s.recovery()
    elif args.action=='stage':
        if args.slot is None:a.error('stage requires --slot')
        manifest=json.loads(pathlib.Path(args.manifest).read_text())
        slot=manifest['slots'][args.slot];root=pathlib.Path(args.store)
        marker=json.loads((root/'.dlt-server-volume.json').read_text())
        if marker['uuid']!=manifest['data_uuid']:raise ValueError('Data volume identity mismatch')
        if slot['file']!='systems/'+args.slot+'.ext4':raise ValueError('Unexpected system image path')
        path=root/slot['file'];st=path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size!=slot['bytes']:raise ValueError('Image type/size mismatch')
        h=hashlib.sha256()
        with path.open('rb') as f:
            for b in iter(lambda:f.read(8<<20),b''):h.update(b)
        if h.hexdigest()!=slot['initial_sha256']:raise ValueError('Unaccepted image hash')
        out=s.stage(args.slot,set(manifest['slots']))
    elif args.action in ('confirm','healthy'):
        if not args.slot or not args.boot_id:a.error('confirm requires slot and boot identity')
        out=getattr(s,args.action)(args.slot,args.boot_id)
    elif args.action=='fail':
        if not args.slot:a.error('fail requires slot')
        out=s.fail(args.slot)
    else:
        boot=args.boot_id or pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        # Only initramfs-probed slot availability is eligible. Default remains
        # recovery when no physical production store has been accepted.
        out=s.select(boot,{'recovery',*args.available_slot})
    print(out['slot'] if args.token_only and args.action=='select' else json.dumps(out,sort_keys=True))


if __name__=='__main__':main()
