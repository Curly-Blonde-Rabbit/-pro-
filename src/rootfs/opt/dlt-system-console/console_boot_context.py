"""Identify exact production/recovery roots before enabling local controls."""
import pathlib,json,stat,os
def authorize(decision,boot_id,root_id,layout,recovery_id):
    if not isinstance(decision,dict) or decision.get('boot_id')!=boot_id:raise RuntimeError('控制台启动身份不匹配')
    slot=decision.get('slot')
    if slot in ('system-a','system-b'):
        if not isinstance(layout,dict) or layout.get('slots',{}).get(slot,{}).get('root_id')!=root_id:raise RuntimeError('正式系统根身份不匹配')
        mode='production';rec='/run/dlt-local-recovery';network='/srv/dlt-state';wifi='dlt-wifi-managed.service'
    elif slot=='recovery':
        if not isinstance(recovery_id,str) or not recovery_id.startswith('dlt-linux-stage1-') or root_id!=recovery_id:raise RuntimeError('保底系统根身份不匹配')
        mode='recovery';rec='/';network='/var/lib/dlt-recovery/network';wifi='dlt-recovery-wifi.service'
    else:raise RuntimeError('不认可的本机启动槽位')
    return {'slot':slot,'mode':mode,'recovery_root':rec,'wifi_profile_root':network,'wifi_service':wifi,'root_id':root_id,'boot_id':boot_id}
def trusted_read(path):
    path=pathlib.Path(path)
    for p in (path,*path.parents):
        if p.is_symlink():raise RuntimeError('启动身份路径包含符号链接')
    st=path.stat()
    if st.st_uid!=0 or st.st_mode&0o022 or not stat.S_ISREG(st.st_mode) or st.st_size>65536:raise RuntimeError('启动身份文件校验失败')
    return path.read_text()
def inspect_context(root='/'):
    root=pathlib.Path(root)
    boot=trusted_read(root/'proc/sys/kernel/random/boot_id').strip()
    decision=json.loads(trusted_read(root/'run/dlt-local-boot-decision.json'))
    rec=root if decision.get('slot')=='recovery' else root/'run/dlt-local-recovery'
    layout=json.loads(trusted_read(rec/'opt/dlt-recovery/production-layout.json'))
    baseline=json.loads(trusted_read(rec/'opt/dlt-recovery/local-baseline.json'))
    root_id=trusted_read(root/'etc/dlt-server/native-root-id').strip()
    return authorize(decision,boot,root_id,layout,baseline.get('recovery_root_id'))
