"""State machine for real settings pages and 26-letter local keyboard."""
import time,queue,threading

class Settings:
    @property
    def page(self):return self._page
    @page.setter
    def page(self,value):
        if value!=getattr(self,'_page',None):
            self.page_generation=getattr(self,'page_generation',0)+1
            self.pointer_start=None
        self._page=value
    def __init__(self,backend):
        self.backend=backend;self.page='home';self.offset=0;self.wifi={};self.bt={};self.phone={}
        self.selected=None;self.password='';self.keyboard_mode='abc';self.shift=False
        self.busy=False;self.message='';self.results=queue.Queue();self.revision=0;self.saved_only=False
        self.pressed=None;self.pressed_until=0;self.keyboard_started=0
        self.admin={};self.admin_selected=None;self.admin_tab='pending';self.admin_check=0
        self.home_scroll=0;self.pointer_start=None;self.pointer_moved=False
        self.overlay={};self.overlay_check=0;self.keyboard_target='wifi';self.capture={};self.capture_check=0;self.server={};self.recovery={};self.recovery_plan={};self.recovery_receipt={};self.status_check=0
        self.bt_saved_only=False;self.bt_action=None;self.confirm_origin_page=None
        self.active_job_kind=None;self.pending_job=None
        self.service={};self.service_selected=None;self.service_tab='status';self.services_offset=0
        self.log_entry=None;self.log_offset=0
        self.screen_blank=False;self.power_action=None;self.power_return='home';self.power_pending=False
    def power_event(self,kind,origin):
        if origin!='physical-power-key' or kind not in ('short','long'):raise PermissionError('实体电源键事件校验失败')
        self.pointer_start=None;self.pressed=None;self.password=''
        if self.power_pending:
            self.screen_blank=(not self.screen_blank) if kind=='short' else False
            self.change();return
        if kind=='short':
            self.screen_blank=not self.screen_blank
            if 'confirm' in self.page or self.page=='power-menu':self.page='home';self.power_action=None
        else:
            self.screen_blank=False;self.power_action=None
            self.power_return=self.page if self.page not in ('power-menu','power-confirm') else 'home'
            self.page='power-menu';self.message=''
        self.change()
    def change(self):self.revision+=1
    def go(self,page):
        self.page=page;self.message='';self.change()
        if page in ('server','phone','recovery') or page.startswith(('server-','phone-')):self.status_check=time.monotonic()
        if page=='wifi':self.refresh_wifi(True)
        elif page=='bluetooth':self.refresh_bt(False)
        elif page=='phone' or page.startswith('phone-'):self.job('phone',self.backend.phone_info,queue_read=True)
        elif page=='server' or page.startswith('server-'):self.job('server',self.backend.server_info,queue_read=True)
        elif page=='recovery':self.job('recovery',self.backend.recovery_info,queue_read=True)
        elif page=='service-detail':self.refresh_service()
        elif page=='admin':self.refresh_admin()
        elif page=='overlay':self.status_check=time.monotonic();self.job('overlay',self.backend.fixed_network_info,queue_read=True)
        elif page=='capture':self.status_check=time.monotonic();self.job('capture',self.backend.capture_info,queue_read=True)
    def refresh_service(self):
        self.status_check=time.monotonic()
        if self.service_selected:
            name=self.service_selected;self.job('service',lambda:self.backend.service_info(name),queue_read=True)
    def refresh_status(self):
        self.status_check=time.monotonic()
        if self.page=='phone' or self.page.startswith('phone-'):self.job('phone',self.backend.phone_info)
        elif self.page=='server' or self.page.startswith('server-'):self.job('server',self.backend.server_info)
        elif self.page in ('recovery','recovery-result','recovery-history'):self.job('recovery',self.backend.recovery_info)
        elif self.page=='service-detail':self.refresh_service()
        elif self.page=='overlay':self.job('overlay',self.backend.fixed_network_info)
        elif self.page=='capture':self.job('capture',self.backend.capture_info)
    def refresh_admin(self):
        if hasattr(self.backend,'admin_info'):self.job('admin',self.backend.admin_info,queue_read=True)
    def home_tiles(self):
        return [(k,(40+(n%2)*512,40+(n//2)*236-self.home_scroll,488,216)) for n,k in enumerate(['server','phone','wifi','bluetooth','admin','recovery','capture','overlay'])]
    def home_max_scroll(self):return max(0,40+((len(self.home_tiles())-1)//2)*236+216-624)
    def swipe(self,direction):
        if self.screen_blank:return
        if self.page=='home':self.home_scroll=self.home_max_scroll() if direction=='up' else 0;self.change()
    def pointer(self,kind,x,y,origin='remote'):
        if self.screen_blank:self.pointer_start=None;return
        if kind=='down':self.pointer_start=(x,y,self.home_scroll,self.page,origin,self.safety_context());self.pointer_moved=False;return
        if self.pointer_start is None:return
        sx,sy,scroll,page,start_origin,context=self.pointer_start
        if origin!=start_origin or context!=self.safety_context():self.pointer_start=None;return
        if abs(y-sy)>24 or abs(x-sx)>24:self.pointer_moved=True
        if page=='home' and self.page=='home' and self.pointer_moved:
            self.home_scroll=max(0,min(self.home_max_scroll(),scroll+sy-y));self.change()
        if kind=='up':
            self.pointer_start=None
            if not self.pointer_moved and self.page==page:self.tap(x,y,origin=origin)
    def safety_context(self):
        admin=getattr(self,'admin_selected',None) or {};selected=getattr(self,'selected',None) or {};plan=getattr(self,'recovery_plan',{})
        return (self.page,self.page_generation,admin.get('request_id'),admin.get('id'),getattr(self,'admin_tab',None),
                plan.get('token'),selected.get('ssid'),selected.get('address'),getattr(self,'bt_action',None),self.screen_blank,self.power_action)
    def job(self,kind,function,queue_read=False):
        if self.busy:
            if queue_read or kind not in ('wifi','bt','phone','server','recovery','admin','service','capture','overlay'):
                if queue_read and self.pending_job and self.pending_job[0] not in ('wifi','bt','phone','server','recovery','admin','service','capture','overlay'):return
                self.pending_job=(kind,function,self.page,self.page_generation);self.message='等待当前检查完成';self.change()
            return
        self.job_origin=self.page
        self.job_generation=self.page_generation
        self.active_job_kind=kind
        self.busy=True;self.message='正在处理';self.change()
        def work():
            try:
                if kind=='wifi':self.results.put(('wifi-preview',self.backend.wifi_info(False),None))
                self.results.put((kind,function(),None))
            except Exception as exc:
                # Only fixed validation/backend errors, never raw config/credentials.
                msg=str(exc) if isinstance(exc,(ValueError,RuntimeError)) else '操作失败，请重试'
                self.results.put((kind,None,msg[:100]))
        threading.Thread(target=work,daemon=True).start()
    def refresh_wifi(self,scan=True):self.job('wifi',lambda:self.backend.wifi_info(scan),queue_read=True)
    def refresh_bt(self,scan=True):self.job('bt',lambda:self.backend.bluetooth_info(scan),queue_read=True)
    def poll(self):
        changed=False
        while True:
            try:kind,value,error=self.results.get_nowait()
            except queue.Empty:break
            if kind=='wifi-preview' and error is None:
                self.wifi=value;self.change();changed=True;continue
            may_navigate=(self.page==getattr(self,'job_origin',self.page) and self.page_generation==getattr(self,'job_generation',self.page_generation))
            self.busy=False
            if may_navigate:self.message=error or ''
            if error is None:
                if kind=='wifi':self.wifi=value
                elif kind=='bt':self.bt=value
                elif kind in ('overlay','overlay-action'):self.overlay=value;self.status_check=time.monotonic()
                elif kind in ('capture','capture-action'):self.capture=value;self.status_check=time.monotonic()
                elif kind=='phone':self.phone=value
                elif kind in ('server','ssh-action'):self.server=value;self.status_check=time.monotonic()
                elif kind=='service':
                    if value.get('name')==self.service_selected:
                        self.service=value
                        if self.page=='service-detail':self.offset=min(self.offset,max(0,((len(self.service_rows())-1)//4)*4))
                elif kind=='recovery':self.recovery=value
                elif kind=='recovery-plan':
                    self.recovery_plan=value
                    if value.get('available') and may_navigate:self.page='recovery-confirm';self.message=''
                    else:self.message=value.get('reason','当前状态下暂不可执行')
                elif kind=='recovery-execute':
                    self.recovery_receipt=value;self.message='任务已提交';self.status_check=0
                    if may_navigate:self.page='recovery-result'
                elif kind=='bt-action':
                    self.message='设置已提交，请读取设备状态';self.status_check=0
                    if may_navigate:self.page='bluetooth'
                elif kind=='admin':self.admin=value
                elif kind=='admin-action':
                    self.admin_check=0
                    if may_navigate:self.message='授权已更新';self.page='admin'
                elif kind=='connect':
                    if may_navigate:self.message='连接成功';self.page='wifi'
                    self.password=''
                    self.wifi.update(ssid=value['ssid'],connected=True,ip=value.get('ip',''))
                    for n in self.wifi.get('networks',[]):n['connected']=n['ssid']==value['ssid']
                elif kind=='bt-connect':self.message='设备已连接'
                elif kind=='power-execute':self.message='已提交有序关停';self.power_pending=True
            self.change();changed=True
        if self.pressed and time.monotonic()>=self.pressed_until:
            self.pressed=None;self.change();changed=True
        # A small processing pulse, rather than freezing during hardware work.
        if self.busy:
            pulse=int(time.monotonic()*3)
            if getattr(self,'pulse',None)!=pulse:self.pulse=pulse;self.change();changed=True
        if self.page=='keyboard' and time.monotonic()-self.keyboard_started<.15:
            self.change();changed=True
        if hasattr(self.backend,'admin_info') and not self.busy and time.monotonic()-self.admin_check>3:
            self.admin_check=time.monotonic()
            try:
                value=self.backend.admin_info()
                if value!=self.admin:
                    self.admin=value;self.change();changed=True
            except Exception:pass
        if self.page=='home' and hasattr(self.backend,'fixed_network_info') and not self.busy and time.monotonic()-self.overlay_check>10:
            self.overlay_check=time.monotonic();self.job('overlay',self.backend.fixed_network_info,queue_read=True)
        if self.page=='home' and hasattr(self.backend,'capture_info') and not self.busy and time.monotonic()-self.capture_check>5:
            self.capture_check=time.monotonic();self.job('capture',self.backend.capture_info,queue_read=True)
        if not self.busy and self.pending_job:
            kind,function,page,generation=self.pending_job;self.pending_job=None
            if self.page==page and self.page_generation==generation:self.job(kind,function)
        if not self.busy and time.monotonic()-self.status_check>4:
            if self.page in ('server','phone','recovery','recovery-result','recovery-history','capture','overlay') or self.page.startswith(('server-','phone-')):self.refresh_status()
            elif self.page=='bluetooth' and self.status_check==0:self.status_check=time.monotonic();self.refresh_bt(False)
            elif self.page=='service-detail' and time.monotonic()-self.status_check>8:self.refresh_service()
        return changed
    def wifi_items(self):
        items=[n for n in self.wifi.get('networks',[]) if not n.get('connected')]
        if not self.saved_only:return items
        saved=self.wifi.get('saved_ssids',[])
        items=[n for n in items if n['ssid'] in saved]
        detected={n['ssid'] for n in self.wifi.get('networks',[])}
        items += [{'ssid':s,'signal':None,'open':False,'saved':True,'detected':False} for s in saved if s not in detected and s!=self.wifi.get('ssid')]
        return items
    def visible_wifi(self):return self.wifi_items()[self.offset:self.offset+3]
    def bt_items(self):return [v for v in self.bt.get('devices',[]) if not self.bt_saved_only or v.get('paired')]
    def visible_bt(self):return self.bt_items()[self.offset:self.offset+3]
    def detail_rows(self):
        p=self.phone;s=self.server;page=self.page
        def value(v,suffix=''):return '未报告' if v is None else str(v)+suffix
        if page=='server-resources':
            seconds=s.get('uptime_seconds',0);days=seconds//86400;hours=(seconds%86400)//3600
            return [('CPU 使用率',value(s.get('cpu_percent'),'%') if s.get('cpu_percent') is not None else '采样中'),('内存使用',str(s.get('used_mib','—'))+' / '+str(s.get('total_mib','—'))+' MiB'),('可用内存',value(s.get('available_mib'),' MiB')),('交换区使用',str(s.get('swap_used_mib','—'))+' / '+str(s.get('swap_total_mib','—'))+' MiB'),('运行时间',str(days)+' 天 '+str(hours)+' 小时'),('系统负载',' / '.join(s.get('load',[])))]
        if page=='server-services':
            labels={'active':'运行中','inactive':'已停止','failed':'故障','activating':'启动中','deactivating':'停止中','reloading':'重新加载中','unknown':'读取失败'}
            return [(v['name'],labels.get(v['state'],'状态未知') if v['installed'] else '未部署') for v in s.get('services',[])]
        if page=='server-storage':
            return [(name,str(v.get(key,'—'))+' GiB') for v in s.get('disks',[])[:1] for name,key in [('总容量','total_gib'),('已使用','used_gib'),('剩余','free_gib')]]
        if page=='server-network':
            return [(v['name']+' · '+name,result) for v in s.get('interfaces',[]) for name,result in [('状态','已连接' if v['state']=='up' else v['state']),('IPv4',' / '.join(v['ipv4']) or '未分配'),('收 / 发',str(v['rx_mib'])+' / '+str(v['tx_mib'])+' MiB')]]
        if page=='phone-battery':return [('电量',str(p.get('battery','—'))+'%'),('电池温度',value(p.get('temperature_c'),'℃')),('电压',value(p.get('voltage_v'),' V')),('电流',value(p.get('current_ma'),' mA')),('健康状态',{'Good':'正常'}.get(p.get('health'),p.get('health','—'))),('充电状态',{'Charging':'充电中','Discharging':'放电中','Not charging':'未充电','Full':'已充满'}.get(p.get('charge'),p.get('charge','—')))]
        if page=='phone-power':
            return [(v['name'],{'1':'已接入','0':'未接入'}.get(v['online'],'未报告')+' · '+v['type']) for v in p.get('supplies',[])]+[('USB 角色', ' / '.join(v['role'] for v in p.get('usb_roles',[])) or '未报告')]
        if page=='phone-display':
            panel=p.get('panel',{});touch=p.get('touch_devices',[])
            return [('显示链路','已初始化' if panel.get('initialized')=='1' else '待检查'),('面板状态码',panel.get('response','—')),('触摸驱动','已接通' if touch else '未接通'),('输入节点',' / '.join(v['event'] for v in touch) or '无'),('有效区域','1032 × 600'),('亮度模式','已验证的固定亮度')]
        if page=='phone-hardware':return [('设备','BlackShark 2 Pro'),('架构',p.get('machine','—')),('内核',p.get('kernel','—')),('触摸设备',str(len(p.get('touch_devices',[])))),('蓝牙控制器',str(p.get('bluetooth_hci_count',0))+' 个')]
        if page in ('recovery-history','recovery-result'):
            records=self.recovery.get('receipts',[])
            if page=='recovery-result':records=[v for v in records if v.get('transaction')==self.recovery_receipt.get('transaction')] or ([self.recovery_receipt] if self.recovery_receipt else [])
            phases={'queued':'已排队','running':'执行中','completed':'已完成','failed':'失败','pending-network':'等待网络','reboot-scheduled':'已安排重启','interrupted':'已中断'}
            warnings=[('记录校验','已跳过 '+str(self.recovery['history_errors'])+' 条异常记录')] if page=='recovery-history' and self.recovery.get('history_errors') else []
            return warnings+[(v.get('title','恢复操作'),v.get('message') or phases.get(v.get('phase'),v.get('phase','未知'))) for v in records]
        return []
    def service_rows(self):
        if self.service_tab=='logs':return [(v['time'],v['message']) for v in reversed(self.service.get('logs',[]))]
        p=self.service.get('properties',{})
        if not self.service.get('installed',True):return [('部署状态','未部署'),('服务名称',self.service_selected or '—')]
        state={'active':'运行中','inactive':'已停止','failed':'故障','activating':'启动中','deactivating':'停止中'}.get(p.get('ActiveState'),p.get('ActiveState','读取中'))
        raw=p.get('MemoryCurrent','');memory=str(round(int(raw)/2**20,1))+' MiB' if raw.isdigit() and int(raw)<2**63 else '未报告'
        return [('运行状态',state),('进程 PID',p.get('MainPID','—')),('内存使用',memory),('重启次数',p.get('NRestarts','—')),('启动设置',p.get('UnitFileState','—')),('最后结果',p.get('Result','—')),('退出代码',p.get('ExecMainStatus','—')),('服务说明',p.get('Description','—'))]
    def rows_for_page(self):return self.service_rows() if self.page=='service-detail' else self.detail_rows()
    def keyboard_keys(self):
        if self.keyboard_mode=='abc':rows=['qwertyuiop','asdfghjkl','zxcvbnm']
        elif self.keyboard_mode=='123':rows=['1234567890','@#$%&-+()/','.,?!\'":;=']
        else:rows=['[]{}<>^*_|','\\~`£€¥•',':;!?"\'.,']
        # Only printable ASCII is accepted for Wi-Fi PSKs.
        if self.keyboard_mode=='more':rows=['[]{}<>^*_|','\\~`$%&+-=',':;!?"\'.,']
        keys=[]
        for row,letters in enumerate(rows):
            width=92;gap=8;count=len(letters)
            if row==2:
                x=190
                keys.append(('SHIFT',(40,434,140,74)))
                width=(690-(count-1)*8)//count
            else:x=(1080-(count*width+(count-1)*gap))//2
            for n,c in enumerate(letters):
                key=c.upper() if self.shift and self.keyboard_mode=='abc' else c
                keys.append((key,(x+n*(width+gap),250+row*92,width,74)))
            if row==2:keys.append(('BACKSPACE',(900,434,140,74)))
        keys += [('MODE',(40,534,170,70)),('SPACE',(222,534,568,70)),('DONE',(802,534,238,70))]
        return keys
    def key(self,key):
        if self.screen_blank:return
        if self.page!='keyboard':return
        if key=='BACKSPACE':self.password=self.password[:-1]
        elif key=='ENTER' or key=='DONE':self.page='overlay-name' if self.keyboard_target=='overlay' else 'wifi-detail'
        elif key=='ESCAPE':self.password='';self.page='overlay' if self.keyboard_target=='overlay' else 'wifi-detail'
        elif key=='SHIFT':
            if self.keyboard_mode=='abc':self.shift=not self.shift
            else:self.keyboard_mode='more' if self.keyboard_mode=='123' else '123'
        elif key=='MODE':self.keyboard_mode='123' if self.keyboard_mode=='abc' else 'abc'
        elif key=='SPACE':
            if len(self.password)<63:self.password+=' '
        elif len(key)==1 and 32<=ord(key)<=126 and len(self.password)<63:self.password+=key
        self.change()
    def tap(self,x,y,origin='remote'):
        if self.screen_blank or self.power_pending:return
        if not (24<=x<1056 and 24<=y<624):return
        self.pressed=(x,y);self.pressed_until=time.monotonic()+.12;self.change()
        if self.page in ('power-menu','power-confirm'):
            if 40<=x<122 and 36<=y<106:self.power_action=None;self.go(self.power_return);return
            if self.page=='power-menu' and 244<=y<400:
                if 40<=x<528:self.power_action='poweroff';self.page='power-confirm';self.message='';self.change()
                elif 552<=x<1040:self.power_action='reboot';self.page='power-confirm';self.message='';self.change()
            elif self.page=='power-confirm' and 432<=y<516:
                if 40<=x<528:self.power_action=None;self.go('power-menu');return
                if 552<=x<1040:
                    if origin!='physical-phone':self.message='请在手机实体屏幕确认';self.change();return
                    if self.busy:self.message='等待当前任务完成';self.change();return
                    if self.power_action in ('poweroff','reboot'):
                        self.job('power-execute',lambda action=self.power_action:self.backend.system_power(action,origin))
            return
        if self.page=='home':
            for key,(bx,by,w,h) in self.home_tiles():
                if 24<=y<624 and bx<=x<bx+w and by<=y<by+h:self.offset=0;self.go(key);return
            return
        if 40<=x<122 and 36<=y<106:
            if self.page=='keyboard':self.key('ESCAPE')
            elif self.page in ('wifi-detail','confirm-wifi'):self.password='';self.page='wifi';self.change()
            elif self.page=='admin-confirm':self.page='admin';self.change()
            elif self.page=='service-log-entry':self.page='service-detail';self.offset=self.log_offset;self.change()
            elif self.page=='service-detail':self.offset=self.services_offset;self.go('server-services')
            elif self.page.startswith('server-'):self.offset=0;self.go('server')
            elif self.page.startswith('phone-'):self.offset=0;self.go('phone')
            elif self.page.startswith('recovery-'):self.offset=0;self.go('recovery')
            elif self.page.startswith('overlay-'):self.password='';self.go('overlay')
            elif self.page in ('bt-detail','bt-confirm'):self.go('bluetooth')
            else:self.password='';self.go('home')
            return
        if self.page in ('overlay','overlay-link','overlay-admin','overlay-name'):
            if 824<=x<1040 and 36<=y<106:
                self.job('overlay',self.backend.fixed_network_info,queue_read=True);return
            if self.page=='overlay' and 422<=y<506:
                if origin!='physical-phone':self.message='请在手机实体屏幕设置';self.change();return
                if self.busy:return
                kind='disable' if x<528 else 'enable'
                if not (40<=x<528 or 552<=x<1040):return
                self.job('overlay-action',lambda kind=kind:self.backend.fixed_network_action(kind,origin));return
            if self.page=='overlay' and 528<=y<604:
                if 40<=x<356:
                    if origin!='physical-phone':self.message='请在手机实体屏幕授权';self.change();return
                    if self.busy:return
                    self.page='overlay-link';self.change();self.job('overlay-action',lambda:self.backend.fixed_network_action('login',origin));return
                if 374<=x<690:self.password=self.overlay.get('hostname','darklighter-server');self.page='overlay-name';self.message='';self.change();return
                if 708<=x<1040:self.page='overlay-admin';self.message='';self.change();return
            if self.page=='overlay-name':
                if 40<=x<1040 and 170<=y<280:self.keyboard_target='overlay';self.keyboard_started=time.monotonic();self.page='keyboard';self.change();return
                if 552<=x<1040 and 422<=y<506:
                    if origin!='physical-phone':self.message='请在手机实体屏幕保存';self.change();return
                    if self.busy:return
                    name=self.password;self.page='overlay';self.change();self.job('overlay-action',lambda name=name:self.backend.fixed_network_action('rename',origin,name));return
            return
        if self.page=='capture':
            if 824<=x<1040 and 36<=y<106:self.refresh_status();return
            if 432<=y<516 and (40<=x<528 or 552<=x<1040):
                if origin!='physical-phone':self.message='请在手机实体屏幕设置';self.change();return
                if not self.capture.get('installed'):self.message='抓包组件未安装';self.change();return
                if self.busy:return
                enabled=x>=552;self.job('capture-action',lambda:self.backend.capture_switch(enabled,origin))
            return
        if self.page in ('server','phone'):
            if self.page=='server' and 560<=x<804 and 36<=y<106:
                if origin!='physical-phone':self.message='请在手机实体屏幕设置 SSH';self.change();return
                if self.busy:return
                state=self.server.get('ssh_exposure',{})
                if not state.get('ready'):self.message='SSH 接口状态待检查';self.change();return
                enabled=not state['network_enabled'];self.job('ssh-action',lambda:self.backend.ssh_exposure_switch(enabled,origin));return
            if 824<=x<1040 and 36<=y<106:self.refresh_status();return
            tiles=['resources','services','storage','network'] if self.page=='server' else ['battery','power','display','hardware']
            for n,key in enumerate(tiles):
                bx=40+(n%2)*512;by=136+(n//2)*216
                if bx<=x<bx+488 and by<=y<by+196:self.offset=0;self.go(self.page+'-'+key);return
            return
        if self.page=='service-detail':
            if 824<=x<1040 and 36<=y<106:self.refresh_service();return
            if 608<=x<804 and 36<=y<106:self.service_tab='logs' if self.service_tab=='status' else 'status';self.offset=0;self.change();return
            if self.service_tab=='logs' and 40<=x<1040 and 132<=y<516:
                if (y-132)%100>=84:return
                index=self.offset+int((y-132)//100);items=list(reversed(self.service.get('logs',[])))
                if index<len(items):self.log_entry=dict(items[index]);self.log_offset=self.offset;self.page='service-log-entry';self.change()
                return
            if 544<=y<610:
                if x<220:self.offset=max(0,self.offset-4)
                elif x>850 and self.offset+4<len(self.service_rows()):self.offset+=4
                self.change()
            return
        if self.page=='service-log-entry':return
        if self.page.startswith(('server-','phone-')):
            if 824<=x<1040 and 36<=y<106:self.refresh_status();return
            if self.page=='server-services' and 40<=x<1040 and 132<=y<516:
                if (y-132)%100>=84:return
                index=self.offset+int((y-132)//100)
                items=self.server.get('services',[])
                if index<len(items):
                    self.services_offset=self.offset;self.service_selected=items[index]['name'];self.service={};self.service_tab='status';self.offset=0;self.go('service-detail')
                return
            if 544<=y<610:
                if x<220:self.offset=max(0,self.offset-4)
                elif x>850 and self.offset+4<len(self.detail_rows()):self.offset+=4
                self.change()
            return
        if self.page=='recovery':
            if 824<=x<1040 and 36<=y<106:self.page='recovery-history';self.offset=0;self.change();return
            items=self.recovery.get('actions',[])
            for n,item in enumerate(items[self.offset:self.offset+4]):
                bx=40+(n%2)*512;by=136+(n//2)*216
                if bx<=x<bx+488 and by<=y<by+196:
                    if not item.get('available'):self.message=item.get('reason','当前不可执行');self.change();return
                    self.job('recovery-plan',lambda action=item['action']:self.backend.recovery_prepare(action));return
            if 552<=y<614:
                if x<220:self.offset=max(0,self.offset-4)
                elif x>850 and self.offset+4<len(items):self.offset+=4
                self.change()
            return
        if self.page=='recovery-confirm':
            if 432<=y<516 and not self.busy:
                if 40<=x<528:self.go('recovery');return
                if 552<=x<1040:
                    if origin!='physical-phone':self.message='请在手机实体屏幕上确认';self.change();return
                    self.job('recovery-execute',lambda:self.backend.recovery_execute(self.recovery_plan['token'],origin))
            return
        if self.page in ('recovery-result','recovery-history'):
            if 824<=x<1040 and 36<=y<106:self.refresh_status()
            if 544<=y<610:
                if x<220:self.offset=max(0,self.offset-4)
                elif x>850 and self.offset+4<len(self.detail_rows()):self.offset+=4
                self.change()
            return
        if self.page=='keyboard':
            for key,(bx,by,w,h) in self.keyboard_keys():
                if bx<=x<bx+w and by<=y<by+h:self.key(key);return
        elif self.page in ('wifi','bluetooth'):
            if self.page=='wifi' and 608<=x<804 and 36<=y<106:
                self.saved_only=not self.saved_only;self.offset=0;self.change();return
            if self.page=='bluetooth' and 608<=x<804 and 36<=y<106:
                self.bt_saved_only=not self.bt_saved_only;self.offset=0;self.change();return
            if self.page=='bluetooth' and self.bt.get('ready') and 40<=x<1040 and 124<=y<216 and not self.busy:
                self.bt_action='power-off' if self.bt.get('powered') else 'power-on';self.selected=None;self.page='bt-confirm';self.change();return
            if 824<=x<1040 and 36<=y<106:
                if self.page=='wifi':self.refresh_wifi()
                else:self.refresh_bt()
            if self.page=='wifi' and 40<=x<1040 and 124<=y<216 and self.wifi.get('connected') and not self.busy:
                self.selected=next((n for n in self.wifi.get('networks',[]) if n.get('connected')),{'ssid':self.wifi['ssid'],'open':False})
                self.password='';self.page='wifi-detail';self.message='';self.change();return
            if 40<=x<1040 and 232<=y<516:
                if (y-232)%96>=82:return
                index=int((y-232)//96);items=self.visible_wifi() if self.page=='wifi' else self.visible_bt()
                if index<len(items) and not self.busy:
                    self.selected=items[index];self.password='';self.page='wifi-detail' if self.page=='wifi' else 'bt-detail';self.message='';self.change()
            if 544<=y<610:
                if x<220:self.offset=max(0,self.offset-3)
                elif x>850:
                    size=len(self.wifi_items()) if self.page=='wifi' else len(self.bt_items())
                    if self.offset+3<size:self.offset+=3
                self.change()
        elif self.page=='wifi-detail':
            if 40<=x<1040 and 250<=y<326 and not self.busy:
                self.keyboard_target='wifi';self.page='keyboard';self.keyboard_started=time.monotonic();self.change()
            elif 40<=x<1040 and 354<=y<432 and not self.busy:
                if self.selected.get('open'):self.message='暂未接入开放网络保存';self.change()
                else:self.page='confirm-wifi';self.change()
        elif self.page=='confirm-wifi' and not self.busy:
            if 552<=x<1040 and 432<=y<516:
                ssid=self.selected['ssid'];secret=self.password;self.password=''
                self.job('connect',lambda:self.backend.wifi_connect(ssid,secret))
            elif 40<=x<528 and 432<=y<516:self.page='wifi-detail';self.change()
        elif self.page=='bt-detail' and not self.busy:
            item=self.selected or {};actions=['disconnect' if item.get('connected') else 'connect','trust-off' if item.get('trusted') else 'trust-on','forget']
            for n,action in enumerate(actions):
                bx=40+(n%2)*512;by=250+(n//2)*104
                if bx<=x<bx+488 and by<=y<by+84:
                    if action=='connect' and not item.get('paired'):self.message='交互配对代理尚未接入';self.change();return
                    self.bt_action=action;self.page='bt-confirm';self.change();return
        elif self.page=='bt-confirm' and 432<=y<516 and not self.busy:
            if 40<=x<528:self.go('bluetooth')
            elif 552<=x<1040:
                if origin!='physical-phone':self.message='请在手机实体屏幕上确认';self.change();return
                action=self.bt_action;address=(self.selected or {}).get('address')
                self.job('bt-action',lambda:self.backend.bluetooth_action(action,address,origin))
        elif self.page=='admin':
            if 632<=x<1040 and 36<=y<106:
                self.admin_tab='devices' if self.admin_tab=='pending' else 'pending';self.offset=0;self.change();return
            items=self.admin.get(self.admin_tab,[])
            if 40<=x<1040 and 164<=y<516:
                if (y-164)%176>=160:return
                n=self.offset+int((y-164)//176)
                if n<len(items):self.admin_selected=items[n];self.page='admin-confirm';self.message='';self.change()
            if 544<=y<610:
                if x<220:self.offset=max(0,self.offset-2)
                elif x>850 and self.offset+2<len(items):self.offset+=2
                self.change()
        elif self.page=='admin-confirm' and not self.busy:
            if 432<=y<516 and 40<=x<1040:
                if origin!='physical-phone':self.message='请在手机实体屏幕上确认，远程点击无授权效力';self.change();return
                item=self.admin_selected or {}
                if self.admin_tab=='pending':
                    approve=x>=552
                    self.job('admin-action',lambda:self.backend.admin_decide(item['request_id'],approve,origin))
                elif x>=552:
                    self.job('admin-action',lambda:self.backend.admin_revoke(item['id'],origin))
    def public_state(self):
        return {'page':self.page,'busy':self.busy,'message':self.message,'screen_blank':self.screen_blank,'power_action':self.power_action,'power_pending':self.power_pending,
                'wifi':self.wifi,'bluetooth':self.bt,'phone':self.phone,
                'keyboard_visible':self.page=='keyboard','password_length':len(self.password),
                'keyboard_layout':'QWERTY-26','settings_actions_connected':True,
                'wifi_saved_filter':self.saved_only,'auto_connect_saved':self.wifi.get('auto_connect_enabled',False),'preserve_connected':True,
                'home_scroll':self.home_scroll,'administrator':self.admin,'new_device_default_role':'operator','grant_confirmation':'physical-phone-only',
                'fixed_network':{k:v for k,v in self.overlay.items() if k not in ('login_url',)},'capture':self.capture,'server':self.server,'recovery':self.recovery,'recovery_confirmation':{k:v for k,v in self.recovery_plan.items() if k!='token'},
                'bluetooth_saved_filter':self.bt_saved_only,'detail_offset':self.offset,'service_detail':self.service,'service_tab':self.service_tab,'runtime_mode':getattr(self,'runtime_mode','production')}
