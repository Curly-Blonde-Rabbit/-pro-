#!/usr/bin/python3
"""Strict, grow-only dm-linear assembly for the DLT-A0 main Linux filesystem.

This program creates kernel mappings only. It never formats, resizes, relocates,
wipes, edits GPT, changes networking or removes the production mapping.
Runtime partition identities are supplied by the private installer plan, not
embedded in the public source. Linux device-mapper table lengths use 512-byte
sectors, independently of the underlying 4096-byte logical block size.
"""
import argparse,fcntl,hashlib,json,os,pathlib,re,stat,struct,subprocess,uuid,zlib

COMPONENTS=(('ramdump','sda17',116733718528),('system_a','sda6',3221225472),
            ('vendor_a','sda8',1073741824),('vendor_b','sda9',1073741824))
TOTAL_BYTES=sum(x[2] for x in COMPONENTS)
MAIN_NAME='dlt-main'
PROBE_NAME='dlt-main-probe'
UUID_PREFIX='DLT-MAINPOOL-V1-'

class PoolError(RuntimeError):pass

def load_plan(path):
    path=pathlib.Path(path)
    for p in (path,*path.parents):
        if p.is_symlink():raise PoolError('Plan path contains a symlink')
    st=path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid or st.st_mode&0o022 or st.st_size>16384:
        raise PoolError('Untrusted pool plan')
    plan=json.loads(path.read_text())
    validate_plan(plan)
    return plan

def validate_plan(plan):
    if plan.get('schema')!=1 or plan.get('board')!='DLT-A0' or plan.get('name')!=MAIN_NAME:
        raise PoolError('Pool plan schema or board mismatch')
    try:uuid.UUID(plan['filesystem_uuid'])
    except (ValueError,KeyError,TypeError):raise PoolError('Invalid filesystem UUID')
    segments=plan.get('segments',[])
    if len(segments)!=len(COMPONENTS):raise PoolError('Unexpected pool component count')
    for row,(label,dev,size) in zip(segments,COMPONENTS):
        if (row.get('label'),row.get('device'),row.get('bytes'))!=(label,dev,size):
            raise PoolError('Unexpected pool component or size')
        if type(row.get('start_sector512'))is not int or row['start_sector512']<0 or row['start_sector512']%8:
            raise PoolError('Unaligned component start')
        try:uuid.UUID(row['partuuid'])
        except (ValueError,KeyError,TypeError):raise PoolError('Invalid component partition UUID')
    extents=sorted((r['start_sector512'],r['start_sector512']+r['bytes']//512) for r in segments)
    if any(a[1]>b[0] for a,b in zip(extents,extents[1:])):raise PoolError('Overlapping component extents')
    return True

def device_size(path):
    with open(path,'rb',buffering=0) as f:
        return struct.unpack('Q',fcntl.ioctl(f.fileno(),0x80081272,struct.pack('Q',0)))[0]

def gpt_entries(path='/dev/sda'):
    with open(path,'rb',buffering=0) as f:
        sector=struct.unpack('I',fcntl.ioctl(f.fileno(),0x1268,struct.pack('I',0)))[0]
        if sector!=4096:raise PoolError('Unexpected disk logical sector size')
        def header(lba):
            f.seek(lba*sector);blob=f.read(sector)
            if len(blob)!=sector:raise PoolError('Short GPT header')
            v=struct.unpack_from('<8sIIIIQQQQ16sQIII',blob)
            if v[0]!=b'EFI PART' or not 92<=v[2]<=sector or v[11]!=32 or v[12]!=128:
                raise PoolError('Unexpected GPT header structure')
            copy=bytearray(blob[:v[2]]);struct.pack_into('<I',copy,16,0)
            if zlib.crc32(copy)!=v[3]:raise PoolError('GPT header checksum mismatch')
            f.seek(v[10]*sector);entries=f.read(v[11]*v[12])
            if zlib.crc32(entries)!=v[13]:raise PoolError('GPT entries checksum mismatch')
            return v,entries
        primary,entries=header(1);backup,other=header(primary[6])
        if entries!=other or backup[6]!=1 or backup[9]!=primary[9]:raise PoolError('GPT copies disagree')
    result={}
    for i in range(primary[11]):
        row=entries[i*128:(i+1)*128]
        if row[:16]==bytes(16):continue
        start,end=struct.unpack_from('<QQ',row,32)
        label=row[56:128].decode('utf-16le').rstrip(chr(0))
        result['sda'+str(i+1)]={'label':label,'partuuid':str(uuid.UUID(bytes_le=row[16:32])),
                              'start_sector512':start*8,'bytes':(end-start+1)*4096}
    return result

def owned_components(plan):
    validate_plan(plan);disk=gpt_entries();rows=[]
    for expected in plan['segments']:
        name=expected['device'];actual=disk.get(name)
        if not actual or any(actual[k]!=expected[k] for k in ('label','partuuid','start_sector512','bytes')):
            raise PoolError('GPT component identity mismatch: '+name)
        dev=pathlib.Path('/dev')/name;st=dev.stat()
        if not stat.S_ISBLK(st.st_mode) or dev.is_symlink() or device_size(dev)!=expected['bytes']:
            raise PoolError('Invalid pool block device: '+name)
        sys=pathlib.Path('/sys/class/block')/name
        if int((sys/'start').read_text())!=expected['start_sector512'] or int((sys/'size').read_text())*512!=expected['bytes']:
            raise PoolError('Kernel component geometry mismatch: '+name)
        number=(sys/'dev').read_text().strip()
        if number!=str(os.major(st.st_rdev))+':'+str(os.minor(st.st_rdev)):
            raise PoolError('Kernel device number mismatch')
        rows.append({**expected,'major_minor':number})
    with open('/dev/'+rows[0]['device'],'rb',buffering=0) as f:
        f.seek(1024);superblock=f.read(1024)
    if superblock[56:58]!=b'\x53\xef' or str(uuid.UUID(bytes=superblock[104:120]))!=plan['filesystem_uuid']:
        raise PoolError('Owned filesystem prefix identity mismatch')
    return rows

def make_table(rows):
    offset=0;lines=[]
    for row in rows:
        n=row['bytes']//512;number=row['major_minor']
        if not re.fullmatch(r'[0-9]+:[0-9]+',number):raise PoolError('Invalid block device number')
        lines.append(f'{offset} {n} linear {number} 0');offset+=n
    if offset*512!=TOTAL_BYTES:raise PoolError('Pool table capacity mismatch')
    return '\n'.join(lines)

def normalize_table(text):
    return '\n'.join(' '.join(line.split()) for line in text.splitlines() if line.strip())

class Mapper:
    def __init__(self,plan,tool_root='/opt/dlt-main-pool-tools'):
        if os.geteuid():raise PermissionError('Root is required for mapping control')
        self.plan=plan;self.rows=owned_components(plan);self.table=make_table(self.rows)
        self.root=pathlib.Path(tool_root);self.tool=self.root/'sbin/dmsetup'
        manifest=self.root/'manifest.json'
        if not manifest.is_file() or manifest.is_symlink():raise PoolError('Mapping tool manifest missing')
        meta=json.loads(manifest.read_text());expected=meta['files'].get('sbin/dmsetup',{}).get('sha256')
        if not expected or self.tool.is_symlink() or self.tool.stat().st_uid or self.tool.stat().st_mode&0o022:
            raise PoolError('Untrusted mapping tool')
        if hashlib.sha256(self.tool.read_bytes()).hexdigest()!=expected:raise PoolError('Mapping tool checksum mismatch')
        self.env=dict(os.environ,LD_LIBRARY_PATH=str(self.root/'lib/aarch64-linux-gnu')+':'+str(self.root/'usr/lib/aarch64-linux-gnu'))
    def run(self,args):
        p=subprocess.run([str(self.tool),'--noudevsync','--noudevrules',*args],env=self.env,capture_output=True,text=True,timeout=15)
        if p.returncode:raise PoolError('Device-mapper command failed: '+args[0]+' '+p.stderr[-600:].strip())
        return p.stdout.strip()
    def existing_node(self,name):
        if name not in (MAIN_NAME,PROBE_NAME):raise PoolError('Mapping name outside allowlist')
        nodes=[]
        for p in pathlib.Path('/sys/class/block').glob('dm-*'):
            try:
                if (p/'dm/name').read_text().strip()==name:nodes.append(p.name)
            except OSError:continue
        if len(nodes)>1:raise PoolError('Duplicate mapping name')
        return pathlib.Path('/dev')/nodes[0] if nodes else None
    def inspect(self,probe=False):
        name=PROBE_NAME if probe else MAIN_NAME;node=self.existing_node(name)
        if node is None:return None
        expected_uuid=UUID_PREFIX+self.plan['filesystem_uuid']+('-PROBE' if probe else '')
        info=self.run(['info','-c','--noheadings','-o','uuid',name]).strip()
        if info!=expected_uuid or normalize_table(self.run(['table',name]))!=self.table:
            raise PoolError('Existing mapping table or UUID is not owned')
        if not stat.S_ISBLK(node.stat().st_mode) or device_size(node)!=TOTAL_BYTES:
            raise PoolError('Mapped capacity mismatch')
        with node.open('rb',buffering=0) as f:
            readonly=struct.unpack('I',fcntl.ioctl(f.fileno(),0x125e,struct.pack('I',0)))[0]
            f.seek(1128);fs_uuid=str(uuid.UUID(bytes=f.read(16)))
        if readonly!=int(probe) or fs_uuid!=self.plan['filesystem_uuid']:raise PoolError('Mapping mode or filesystem identity mismatch')
        return {'name':name,'device':str(node),'bytes':TOTAL_BYTES,'readonly':bool(readonly),
                'table':self.table,'uuid':expected_uuid,'filesystem_uuid':fs_uuid}
    def ensure(self,probe=False):
        current=self.inspect(probe)
        if current:return {**current,'created':False}
        name=PROBE_NAME if probe else MAIN_NAME
        args=['create',name,'--uuid',UUID_PREFIX+self.plan['filesystem_uuid']+('-PROBE' if probe else ''),'--table',self.table]
        if probe:args.append('--readonly')
        self.run(args);result=self.inspect(probe)
        if not result:raise PoolError('New mapping did not appear')
        return {**result,'created':True}
    def remove_probe(self):
        current=self.inspect(True)
        if current:self.run(['remove',PROBE_NAME])
        if self.existing_node(PROBE_NAME):raise PoolError('Probe removal failed')
        return {'probe_removed':True,'production_mapping_removed':False}


def validate_mount(layout,mount,expected_target,recovery_root='/'):
    """Validate one mounted main filesystem, including every linear component.

    A raw prefix is accepted only during the declared pre-growth transition.
    Once storage_mode is dm-linear-v1, raw-prefix mounts are always rejected.
    """
    if mount.get('target')!=expected_target or mount.get('fstype')!='ext4' or mount.get('uuid')!=layout.get('data_uuid'):
        raise PoolError('Mounted pool identity mismatch')
    source=pathlib.Path(mount['source'].split('[',1)[0]).resolve()
    if not stat.S_ISBLK(source.stat().st_mode):raise PoolError('Main pool source is not a block device')
    size=device_size(source);transition=layout.get('linear_pool',{})
    if str(source)=='/dev/sda17':
        if layout.get('storage_mode')=='dm-linear-v1' or transition.get('pending')is not True:
            raise PoolError('Raw prefix mount is not allowed after linear activation')
        if size!=COMPONENTS[0][2] or size!=layout.get('data_bytes'):
            raise PoolError('Unexpected pre-growth raw prefix geometry')
        return {'mode':'declared-linear-transition-raw-prefix','device':str(source),'bytes':size}
    if not re.fullmatch(r'/dev/dm-[0-9]+',str(source)):
        raise PoolError('Unexpected mapped source')
    if size!=TOTAL_BYTES or size not in (layout.get('data_bytes'),layout.get('pending_data_bytes')):
        raise PoolError('Mapped pool is outside declared geometry')
    rec=pathlib.Path(recovery_root);path=rec/'etc/dlt-server/main-pool-layout.json'
    plan=load_plan(path)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=transition.get('plan_sha256'):
        raise PoolError('Mapping plan differs from boot layout')
    if plan['filesystem_uuid']!=layout.get('data_uuid'):raise PoolError('Pool UUID differs from boot layout')
    mapping=Mapper(plan,str(rec/'opt/dlt-main-pool-tools')).inspect(False)
    if not mapping or pathlib.Path(mapping['device']).stat().st_rdev!=source.stat().st_rdev:
        raise PoolError('Mounted device is not the validated production mapper')
    return {'mode':'dm-linear-v1','device':str(source),'bytes':size,
            'table_sha256':hashlib.sha256(mapping['table'].encode()).hexdigest()}

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['status','assemble','probe','remove-probe']);p.add_argument('--plan',default='/etc/dlt-server/main-pool-layout.json');p.add_argument('--tool-root',default='/opt/dlt-main-pool-tools');p.add_argument('--device-only',action='store_true');a=p.parse_args()
    mapper=Mapper(load_plan(a.plan),a.tool_root)
    if a.action=='status':result=mapper.inspect(False)or{'assembled':False,'bytes':TOTAL_BYTES}
    elif a.action=='assemble':result=mapper.ensure(False)
    elif a.action=='probe':result=mapper.ensure(True)
    else:result=mapper.remove_probe()
    print(result['device'] if a.device_only else json.dumps(result))
if __name__=='__main__':main()
