"""Local physical-confirmation recovery dispatcher. Never resets an active root."""
import base64,hashlib,importlib.util,json,os,pathlib,re,secrets,shutil,subprocess,sys,time,contextlib,fcntl,stat,math
RUN=pathlib.Path('/run/dlt-system-console')
HISTORY=pathlib.Path('/srv/dlt-state/console-actions')
REC=pathlib.Path('/run/dlt-local-recovery') if pathlib.Path('/run/dlt-local-recovery/opt/dlt-recovery/production-layout.json').is_file() else pathlib.Path('/')
ACTIONS={'wifi-restart':'恢复 Wi-Fi','time-sync':'同步时间','recovery-boot':'进入保底系统','normal-boot':'返回正式系统','rollback':'版本回退','inactive-reset':'重装备用系统'}
def run(argv,timeout=15):
    p=subprocess.run(argv,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError('操作未完成：'+pathlib.Path(argv[0]).name)
    return p.stdout.strip()
def atomic(path,value):
    from device_access import atomic as write
    path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    write(path,(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode())
def boot():return pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
@contextlib.contextmanager
def operation_lock(wait=False):
    RUN.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(RUN/'recovery-operation.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        deadline=time.monotonic()+5
        while True:
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if not wait or time.monotonic()>=deadline:raise RuntimeError('已有恢复任务正在执行，请查看操作记录')
                time.sleep(.05)
        yield
    finally:os.close(fd)
def active_operation():
    path=RUN/'recovery-active.json'
    if not path.exists():return None
    obj=read_private_json(path)
    if not isinstance(obj,dict) or not isinstance(obj.get('boot_id'),str):raise RuntimeError('恢复任务索引损坏，请检查后再操作')
    deadline=obj.get('deadline_monotonic')
    if type(deadline) not in (int,float) or not math.isfinite(deadline):raise RuntimeError('恢复任务租约格式异常')
    if obj['boot_id']!=boot() or deadline<=time.monotonic():return None
    if not isinstance(obj.get('transaction'),str) or not re.fullmatch('[a-f0-9]{32}',obj['transaction']):raise RuntimeError('恢复任务身份格式异常')
    return obj
def read_private_json(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        st=os.fstat(fd)
        if st.st_uid!=0 or st.st_mode&0o077 or not stat.S_ISREG(st.st_mode) or st.st_size>65536:raise PermissionError('恢复记录文件校验失败')
        try:return json.loads(os.read(fd,65537))
        except (ValueError,UnicodeError):raise RuntimeError('恢复记录内容损坏，请检查后再操作')
    finally:os.close(fd)
def controller():
    p=REC/'usr/local/lib/dlt-recovery/dlt_boot_state.py'
    if p.is_symlink() or p.stat().st_uid!=0 or p.stat().st_mode&0o022:raise RuntimeError('保底控制器校验失败')
    spec=importlib.util.spec_from_file_location('dlt_console_boot_state',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m.Store(REC/'var/lib/dlt-recovery/boot-state')
def inspect():
    current=json.loads(pathlib.Path('/run/dlt-local-boot-decision.json').read_text())
    if current['boot_id']!=boot():raise RuntimeError('启动状态不匹配')
    store=controller();state=store.load()
    if state.get('boot_id')!=current['boot_id'] or state.get('boot_slot')!=current.get('slot'):
        raise RuntimeError('恢复控制器与当前启动租约不一致')
    return current,state
def preflight(action):
    if action not in ACTIONS:raise ValueError('未知恢复操作')
    decision,state=inspect();reason='';target=None
    operation_fingerprint=None
    if action in ('inactive-reset','rollback'):
        target=state.get('previous') if action=='rollback' else None
        if action=='rollback' and target=='recovery':
            if decision['slot']=='recovery':reason='当前已在保底系统'
        else:
            from console_image_recovery import plan as image_plan
            image=image_plan(REC,decision,state,action,pathlib.Path(__file__).parent)
            target=image['target'];reason=image['reason'];operation_fingerprint=image['operation_fingerprint']
    elif action=='normal-boot':
        target=state.get('good')
        if decision['slot']!='recovery':reason='当前已在正式系统'
        elif target not in ('system-a','system-b') or state.get('candidate') is not None:reason='没有已确认可返回的正式版本'
        elif state.get('request') not in (None,'none'):reason='存在尚未完成的启动请求'
    elif action=='recovery-boot' and decision['slot']=='recovery':reason='当前已在保底系统'
    elif action=='wifi-restart':
        wifi_unit='dlt-recovery-wifi.service' if decision['slot']=='recovery' else 'dlt-wifi-managed.service'
        p=subprocess.run(['systemctl','show',wifi_unit,'-p','LoadState','--value'],capture_output=True,text=True,timeout=3)
        if p.stdout.strip()!='loaded':reason='Wi-Fi 管理服务未安装'
    elif action=='time-sync':
        if not shutil.which('timedatectl'):reason='时间同步工具未安装'
    return {'action':action,'title':ACTIONS[action],'available':not reason,'reason':reason,'current_slot':decision['slot'],'target':target,'operation_fingerprint':operation_fingerprint,'wifi_service':'dlt-recovery-wifi.service' if decision['slot']=='recovery' else 'dlt-wifi-managed.service',
            'boot_id':boot(),'boot_state_seq':state['seq'],'data_preserved':True,'requires_physical_phone':True,
            'warning':'',
            'effect':{'wifi-restart':'Wi-Fi 会短暂断开；保留已保存网络，不重启手机','time-sync':'重新启动校时，不重启手机','recovery-boot':'重启进入机内保底 Linux，SSH 会暂时断开','normal-boot':'重新启动到已确认的正式版本，SSH 会暂时断开','rollback':'回到已记录的上一版本，SSH 会暂时断开','inactive-reset':'重装非活动系统并重启试运行，保留业务数据'}[action]}
def info():
    try:
        decision,state=inspect();actions=[preflight(k) for k in ACTIONS]
        error=''
    except Exception:
        decision={};state={};actions=[{'action':k,'title':v,'available':False,'reason':'保底系统状态待检查'} for k,v in ACTIONS.items()];error='保底系统状态待检查'
    receipts=[];history_errors=0
    if HISTORY.exists():
        candidates=[]
        for index,p in enumerate(HISTORY.glob('*.json')):
            if index>=512:history_errors+=1;break
            try:candidates.append((p.lstat().st_mtime,p))
            except OSError:history_errors+=1
        for _,p in sorted(candidates,reverse=True)[:16]:
            try:
                item=read_private_json(p)
                if not isinstance(item,dict) or not isinstance(item.get('transaction'),str) or not re.fullmatch('[a-f0-9]{32}',item['transaction']):raise ValueError('invalid receipt')
                if item.get('phase') not in ('queued','running','completed','failed','pending-network','reboot-scheduled','interrupted'):raise ValueError('invalid receipt phase')
                if any(k in item and not isinstance(item[k],str) for k in ('message','title','action','boot_id')):raise ValueError('invalid receipt value')
                receipts.append(item)
                if len(receipts)>=8:break
            except (ValueError,OSError,RuntimeError):history_errors+=1
    try:active=active_operation()
    except (ValueError,OSError,RuntimeError,TypeError):
        active=None;error='恢复任务索引损坏，执行入口已暂停'
        for item in actions:item.update(available=False,reason=error)
    for item in receipts:
        if item.get('phase') in ('queued','running','reboot-scheduled') and (item.get('boot_id')!=boot() or not active or item.get('transaction')!=active.get('transaction')):
            item.update(phase='interrupted',message='任务会话已结束；请核对实际状态，未自动重试')
    return {'current_slot':decision.get('slot','unknown'),'good_slot':state.get('good'),'previous':state.get('previous'),'boot_healthy':state.get('boot_healthy',False),'actions':actions,'receipts':receipts,'error':error,'active_task':active,'history_errors':history_errors}
def prepare(action):
    plan=preflight(action)
    active=active_operation()
    if active:plan.update(available=False,reason='已有恢复任务正在执行，请查看操作记录')
    if not plan['available']:return plan
    for p in RUN.glob('recovery-plan-*.json'):
        if p.is_symlink() or p.stat().st_uid!=0:raise PermissionError('无效恢复计划文件')
        try:
            old=read_private_json(p)
            if not isinstance(old,dict):continue
            if old.get('boot_id')!=boot() or (type(old.get('expires')) in (int,float) and old['expires']<time.time()):p.unlink()
        except (ValueError,OSError,RuntimeError):continue
    token=secrets.token_hex(16);plan.update(token=token,expires=time.time()+90,expires_monotonic=time.monotonic()+90)
    atomic(RUN/('recovery-plan-'+token+'.json'),plan)
    return plan
def execute(token,origin):
    if origin!='physical-phone':raise PermissionError('请在手机实体屏幕确认恢复操作')
    with operation_lock():return execute_locked(token,origin)
def execute_locked(token,origin):
    if origin!='physical-phone':raise PermissionError('请在手机实体屏幕确认恢复操作')
    if os.geteuid()!=0 or not isinstance(token,str) or not re.fullmatch('[a-f0-9]{32}',token):raise PermissionError('无效操作凭证')
    path=RUN/('recovery-plan-'+token+'.json')
    plan=read_private_json(path);validate_plan(plan,token)
    if plan['expires']<time.time() or plan['expires_monotonic']<time.monotonic() or plan['boot_id']!=boot():raise RuntimeError('确认已过期，请重新检查')
    fresh=preflight(plan['action'])
    if not fresh['available'] or fresh['boot_state_seq']!=plan['boot_state_seq'] or fresh.get('target')!=plan.get('target') or fresh.get('operation_fingerprint')!=plan.get('operation_fingerprint'):raise RuntimeError('系统状态已变化，请重新检查')
    if active_operation():raise RuntimeError('已有恢复任务正在执行，请查看操作记录')
    consumed=RUN/('recovery-consumed-'+token+'.json');os.rename(path,consumed)
    receipt={'transaction':token,'action':plan['action'],'title':plan['title'],'phase':'queued','time':time.time(),'boot_id':boot(),'data_preserved':True}
    atomic(HISTORY/(token+'.json'),receipt)
    atomic(RUN/'recovery-active.json',{'transaction':token,'action':plan['action'],'boot_id':boot(),'deadline_monotonic':time.monotonic()+480})
    worker=pathlib.Path(__file__).resolve()
    try:run(['systemd-run','--quiet','--collect','--property=RuntimeMaxSec=420','--property=TimeoutStopSec=20','--property=KillMode=control-group','--unit=dlt-console-action-'+token[:12],'/usr/bin/python3',str(worker),'--worker',str(consumed)],8)
    except BaseException:
        receipt.update(phase='failed',message='任务未能启动');atomic(HISTORY/(token+'.json'),receipt)
        (RUN/'recovery-active.json').unlink(missing_ok=True);raise
    return receipt
def validate_plan(plan,token):
    if not isinstance(plan,dict) or plan.get('token')!=token or plan.get('action') not in ACTIONS:raise RuntimeError('恢复计划内容校验失败')
    if not isinstance(plan.get('boot_id'),str) or type(plan.get('boot_state_seq')) is not int or plan['boot_state_seq']<0:raise RuntimeError('恢复计划启动身份无效')
    for key in ('expires','expires_monotonic'):
        if type(plan.get(key)) not in (int,float) or not math.isfinite(plan[key]):raise RuntimeError('恢复计划时限无效')
    if plan.get('available') is not True or not isinstance(plan.get('title'),str):raise RuntimeError('恢复计划尚未就绪')
def cancel_transition(ctl,expected,kind):
    # Compare-and-swap under the same controller lock. Never restore raw copies.
    with ctl.locked():
        latest=ctl.load()
        if latest!=expected:return False
        if kind=='recovery':
            if latest['request']!='recovery':return False
            latest.update(request='none',reason='console-reboot-cancelled')
        elif kind=='candidate':
            if latest['candidate'] is None or latest['attempts'] or latest['trial_boot'] is not None:return False
            latest.update(candidate=None,attempts=0,trial_boot=None,reason='console-trial-cancelled')
        else:raise ValueError('unknown transition')
        ctl.save(latest);return True

def schedule_transition(ctl,expected,kind,receipt,message):
    try:run(['systemd-run','--quiet','--collect','--unit=dlt-console-reboot-'+receipt['transaction'][:12],'--on-active=5s','/bin/systemctl','reboot'])
    except BaseException:
        receipt['boot_request_cancelled']=cancel_transition(ctl,expected,kind)
        receipt['boot_state_after_schedule_failure']=ctl.load()
        raise
    return {'phase':'reboot-scheduled','message':message}

def apply(plan,receipt):
    action=plan['action']
    if action=='wifi-restart':
        wifi_unit=plan.get('wifi_service','dlt-wifi-managed.service')
        if wifi_unit not in ('dlt-wifi-managed.service','dlt-recovery-wifi.service'):raise RuntimeError('未知网络恢复服务')
        run(['systemctl','restart',wifi_unit],60)
        end=time.monotonic()+45
        while time.monotonic()<end:
            text=run(['/sbin/wpa_cli','-i','wlan0','status'],4)
            if 'wpa_state=COMPLETED' in text and 'ip_address=' in text:break
            time.sleep(1)
        else:raise RuntimeError('Wi-Fi 尚未恢复连接；原配置未删除，USB 调试仍独立保留')
        return {'phase':'completed','message':'Wi-Fi 已重新连接，配置未删除'}
    if action=='time-sync':
        run(['timedatectl','set-ntp','true']);run(['systemctl','restart','systemd-timesyncd.service'])
        end=time.monotonic()+35
        while time.monotonic()<end:
            if run(['timedatectl','show','-p','NTPSynchronized','--value'])=='yes':return {'phase':'completed','message':'系统时间已同步'}
            time.sleep(1)
        return {'phase':'pending-network','message':'校时已启动，等待时间服务器响应'}
    if action=='inactive-reset' or (action=='rollback' and plan.get('target')!='recovery'):
        from console_image_recovery import stage_previous,reset_inactive
        ctl=controller();expected=ctl.load()
        if expected['seq']!=plan['boot_state_seq']:raise RuntimeError('启动状态已变化')
        if action=='rollback':result=stage_previous(REC,plan['target'],ctl,expected,atomic)
        else:result=reset_inactive(REC,plan['target'],ctl,expected)
        receipt.update({k:v for k,v in result.items() if k!='transitioned'})
        return schedule_transition(ctl,result['transitioned'],'candidate',receipt,'已安排试运行目标系统，健康检查失败时保留原版本')
    if action=='recovery-boot' or (action=='rollback' and plan.get('target')=='recovery'):
        ctl=controller();transitioned=ctl.recovery()
        return schedule_transition(ctl,transitioned,'recovery',receipt,'已安排进入机内保底系统')
    if action=='normal-boot':
        decision,state=inspect()
        if decision['slot']!='recovery' or state.get('candidate') is not None or state.get('good')!=plan.get('target') or plan.get('target') not in ('system-a','system-b'):raise RuntimeError('正式系统返回条件已变化')
        run(['systemd-run','--quiet','--collect','--unit=dlt-console-reboot-'+receipt['transaction'][:12],'--on-active=5s','/bin/systemctl','reboot'])
        return {'phase':'reboot-scheduled','message':'已安排返回已确认的正式系统'}
    raise RuntimeError('该操作尚未通过执行门禁')
def worker(plan_path):
    with operation_lock(wait=True):worker_locked(plan_path)
def worker_locked(plan_path):
    if os.geteuid()!=0:raise PermissionError('root required')
    p=pathlib.Path(plan_path)
    if p.parent!=RUN or not re.fullmatch(r'recovery-consumed-[a-f0-9]{32}\.json',p.name) or p.is_symlink():raise ValueError('invalid worker target')
    token=p.name[len('recovery-consumed-'):-len('.json')]
    plan=read_private_json(p);validate_plan(plan,token);path=HISTORY/(token+'.json');receipt=read_private_json(path)
    if not isinstance(receipt,dict) or receipt.get('transaction')!=token:raise RuntimeError('恢复任务回执无效')
    try:
        active=active_operation()
        if not active or active['transaction']!=token:raise RuntimeError('恢复任务租约无效')
        if plan['boot_id']!=boot() or plan['expires']<time.time() or plan['expires_monotonic']<time.monotonic():raise RuntimeError('操作计划已失效')
        fresh=preflight(plan['action'])
        if not fresh['available'] or fresh['boot_state_seq']!=plan['boot_state_seq'] or fresh.get('target')!=plan.get('target') or fresh.get('operation_fingerprint')!=plan.get('operation_fingerprint'):raise RuntimeError('执行前状态检查未通过')
        receipt.update(phase='running');atomic(path,receipt)
        receipt.update(apply(plan,receipt))
    except Exception as exc:receipt.update(phase='failed',message=str(exc)[:120])
    finally:
        atomic(path,receipt);p.unlink(missing_ok=True)
        if receipt.get('phase')!='reboot-scheduled':
            active=active_operation()
            if active and active['transaction']==token:(RUN/'recovery-active.json').unlink(missing_ok=True)
if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--worker':worker(sys.argv[2])
    else:raise SystemExit(2)
