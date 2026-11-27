"""Bounded local image recovery. Never formats devices or touches active roots."""
import pathlib,json,hashlib,os,stat,subprocess,tempfile,shutil,contextlib,fcntl
SLOTS=('system-a','system-b')
SIZE=3221225472

def digest(path):
    h=hashlib.sha256()
    with pathlib.Path(path).open('rb') as f:
        for b in iter(lambda:f.read(4<<20),b''):h.update(b)
    return h.hexdigest()

def trusted(path,limit=None):
    path=pathlib.Path(path)
    for p in (path,*path.parents):
        if p.is_symlink():raise ValueError('恢复路径包含符号链接')
    st=path.stat()
    if st.st_uid!=0 or st.st_mode&0o022 or not stat.S_ISREG(st.st_mode) or (limit is not None and st.st_size>limit):raise ValueError('恢复文件身份或权限不符')
    return st

def read_json(path):
    trusted(path,65536)
    return json.loads(pathlib.Path(path).read_text())

def command(args,timeout=15):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError('恢复检查失败：'+pathlib.Path(args[0]).name)
    return p.stdout.strip()

def metadata(rec,target):
    if target not in SLOTS:raise ValueError('恢复目标不在白名单')
    rec=pathlib.Path(rec);layout=read_json(rec/'opt/dlt-recovery/production-layout.json');row=layout['slots'][target];store=rec/'mnt/dlt-store'
    marker=read_json(store/'.dlt-server-volume.json')
    mount=json.loads(command(['findmnt','--json','--mountpoint',str(store),'--output','SOURCE,FSTYPE,UUID,TARGET']))['filesystems'][0]
    source=pathlib.Path(mount['source'].split('[',1)[0]).resolve()
    if marker['uuid']!=layout['data_uuid']:raise ValueError('独立核心存储身份不符')
    if 'linear_pool' in layout:
        import importlib.util
        helper=rec/'usr/local/lib/dlt-recovery/dlt_main_pool.py';trusted(helper,65536)
        spec=importlib.util.spec_from_file_location('dlt_main_pool_recovery',helper)
        guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
        guard.validate_mount(layout,mount,str(store),rec)
    elif mount['uuid']!=layout['data_uuid'] or mount['fstype']!='ext4' or source!=pathlib.Path('/dev/sda17') or not source.is_block_device():raise ValueError('独立核心存储身份不符')
    if row['file']!='systems/'+target+'.ext4' or row['bytes']!=SIZE:raise ValueError('系统镜像路径或容量不符')
    image=store/row['file'];st=trusted(image)
    if st.st_size!=SIZE:raise ValueError('系统镜像长度不符')
    if any(p.read_text().strip().endswith('/systems/'+target+'.ext4') for p in pathlib.Path('/sys/class/block').glob('loop*/loop/backing_file')):raise ValueError('目标系统仍被使用')
    return layout,row,store,image,st

def plan(rec,decision,state,action,console):
    target=None
    try:
        if state.get('candidate') is not None or state.get('request')!='none' or not state.get('boot_healthy'):raise ValueError('已有启动任务或当前系统尚未通过健康检查')
        if action=='rollback':target=state.get('previous')
        elif action=='inactive-reset':
            active=decision['slot'] if decision['slot'] in SLOTS else state.get('good')
            target={'system-a':'system-b','system-b':'system-a'}.get(active)
        else:raise ValueError('未知镜像恢复操作')
        if target not in SLOTS or target==decision['slot']:raise ValueError('没有可操作的非活动系统')
        layout,row,store,image,st=metadata(rec,target)
        identity={'action':action,'target':target,'root_id':row['root_id'],'image_inode':st.st_ino,'image_mtime_ns':st.st_mtime_ns,'image_size':st.st_size}
        if action=='inactive-reset':
            bm=read_json(pathlib.Path(rec)/'opt/dlt-recovery/production-baselines/manifest.json')['slots'][target]
            if bm['file']!='production-baseline-'+target+'.ext4.gz' or bm['bytes']!=SIZE or bm['root_id']!=row['root_id']:raise ValueError('重装基线身份不符')
            provenance=bm.get('provenance',{})
            if provenance.get('clean_reinstall_boot_accepted') is not True:raise ValueError('该系统的新版基线尚未通过重装启动验收')
            if provenance.get('console_manifest_sha256')!=digest(pathlib.Path(console)/'manifest.json'):raise ValueError('重装基线尚未包含当前控制台版本')
            trusted(pathlib.Path(rec)/'opt/dlt-recovery/production-baselines'/bm['file'],SIZE)
            if shutil.disk_usage(store).free<SIZE+(128<<20):raise ValueError('重装暂存空间不足，原系统和数据未改动')
            identity['baseline']=bm
        fingerprint=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return {'target':target,'reason':'','operation_fingerprint':fingerprint}
    except (OSError,ValueError,KeyError,TypeError,RuntimeError) as exc:
        return {'target':target,'reason':str(exc)[:160],'operation_fingerprint':None}

@contextlib.contextmanager
def image_lock(rec):
    p=pathlib.Path(rec)/'var/lib/dlt-recovery/reset.lock';fd=os.open(p,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        st=os.fstat(fd)
        if st.st_uid!=0 or st.st_mode&0o077 or not stat.S_ISREG(st.st_mode):raise ValueError('恢复锁文件身份不符')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);yield
    finally:os.close(fd)

def stage_previous(rec,target,ctl,expected,atomic,verify_only=False):
    with image_lock(rec):
        layout,row,store,image,st=metadata(rec,target)
        command(['e2fsck','-fn',str(image)],120);raw=digest(image);loop=None;mounted=False
        root=pathlib.Path(tempfile.mkdtemp(prefix='dlt-check-',dir='/run'))
        try:
            loop=command(['losetup','--find','--show','--read-only',str(image)])
            if loop in ['/dev/loop'+str(i) for i in range(5)]:raise ValueError('系统专用 loop 分配异常')
            command(['mount','-t','ext4','-o','ro,noload,nodev,nosuid',loop,str(root)]);mounted=True
            if (root/'etc/dlt-server/native-root-id').read_text().strip()!=row['root_id']:raise ValueError('上一版本系统身份不符')
            manifest=read_json(root/'opt/dlt-system-console/manifest.json')
            for n,h in manifest['files'].items():
                p=root/'opt/dlt-system-console'/n
                if pathlib.PurePosixPath(n).is_absolute() or '..' in pathlib.PurePosixPath(n).parts or digest(p)!=h:raise ValueError('上一版本控制台校验失败')
            for n in ['etc/ssh/ssh_host_ed25519_key','home/ops/.ssh/authorized_keys']:
                if digest(root/n)!=digest('/'+n):raise ValueError('上一版本不匹配当前调试身份')
            for n in ['dlt-access-identity-restore.service','dlt-host-identity-restore.service']:
                if not (root/'etc/systemd/system'/n).is_file():raise ValueError('上一版本缺少持久身份恢复服务')
            if (root/'etc/dlt-server/E476-trial-arm.json').exists():raise ValueError('上一版本仍有一次性测试任务')
        finally:
            # An unmount failure must retain the mountpoint for inspection, not
            # recursively delete a still-mounted system or detach it blindly.
            cleanup_error=None
            if mounted or os.path.ismount(root):
                try:command(['umount',str(root)]);mounted=False
                except Exception as exc:mounted=True;cleanup_error=exc
            if loop and not mounted:
                try:command(['losetup','-d',loop]);loop=None
                except Exception as exc:cleanup_error=exc
            if not mounted and loop is None:root.rmdir()
            if cleanup_error is not None:raise RuntimeError('镜像卸载未完成，已保留只读挂载点供检查：'+str(root)) from cleanup_error
        if digest(image)!=raw:raise ValueError('验证期间系统镜像发生变化')
        if verify_only:
            if ctl.load()!=expected:raise ValueError('只读验证期间启动状态发生变化')
            return {'verified_image_sha256':raw,'target':target,'staged':False,'no_image_or_controller_writes':True}
        with ctl.locked():
            state=ctl.load()
            if state!=expected or state['candidate'] is not None or state['previous']!=target or target==state['good']:raise ValueError('回退验证期间启动状态发生变化')
            layout['slots'][target]['initial_sha256']=raw;atomic(pathlib.Path(rec)/'opt/dlt-recovery/production-layout.json',layout)
            state.update(candidate=target,attempts=0,trial_boot=None,request='none',reason='candidate-staged')
            transitioned=ctl.save(state)
        return {'transitioned':transitioned,'verified_image_sha256':raw,'target':target}

def reset_inactive(rec,target,ctl,expected):
    if ctl.load()!=expected:raise ValueError('重装前启动状态发生变化')
    tool=pathlib.Path(rec)/'usr/local/sbin/dlt-server-reset';trusted(tool,65536)
    receipt=json.loads(command([str(tool),'--slot',target],300))
    transitioned=ctl.load()
    if receipt.get('data_probe_retained') is not True or receipt.get('business_data_formatted') is not False or transitioned['candidate']!=target or transitioned['boot_id']!=expected['boot_id']:raise ValueError('重装回执或启动任务不符，请检查保留的备份')
    return {'transitioned':transitioned,'reset_receipt':receipt,'target':target}
