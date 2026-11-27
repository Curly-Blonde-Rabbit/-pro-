"""Phone UI capture module; viewing this module never starts a capture."""
import pathlib,importlib.util,json,sys
ROOT=pathlib.Path('/opt/dlt-netcapture')
def policy():
 p=ROOT/'capture_policy.py'
 if not p.is_file() or p.is_symlink() or p.stat().st_uid or p.stat().st_mode&0o022:raise RuntimeError('抓包策略组件未安装或校验失败')
 spec=importlib.util.spec_from_file_location('dlt_capture_module_policy',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def info():
 try:
  state=policy().read();sessions=[]
  for p in pathlib.Path('/run/dlt-netcapture').glob('*.json'):
   if p.name=='module.json' or p.is_symlink() or p.stat().st_size>32768:continue
   try:
    s=json.loads(p.read_text())
    if not s.get('session') or not s.get('running') or s.get('boot_id')!=state['boot_id']:continue
    proc=pathlib.Path('/proc')/str(s['pid'])
    if proc.exists() and (proc/'stat').read_text().split()[21]==s['pid_start']:sessions.append(s)
   except (OSError,ValueError,KeyError):continue
  return {'installed':True,'enabled':state['enabled'],'active_sessions':len(sessions),'packets':sum(s.get('seen_packets',0) for s in sessions),'drops':sum(s.get('kernel_drops',0)+s.get('queue_drops',0) for s in sessions),'queue_bytes':sum(s.get('queue_bytes',0) for s in sessions),'label':('采集中' if sessions else '已开启 · 等待电脑') if state['enabled'] else ('正在停止' if sessions else '已关闭'),'default_enabled':False,'autostart':False}
 except (OSError,ValueError,RuntimeError):return {'installed':False,'enabled':False,'active_sessions':0,'label':'未安装','default_enabled':False,'autostart':False}

def switch(enabled,origin):
 if origin!='physical-phone':raise PermissionError('请在手机实体屏幕设置抓包开关')
 policy().set_enabled(enabled,origin)
 return info()
