"""Native dynamic settings renderer, retaining the accepted rounded palette."""
import pathlib,time,math
import itertools,unicodedata
from PIL import Image,ImageDraw,ImageFont
from system_console_renderer import SystemConsole

class View:
    def __init__(self,root):
        self.root=pathlib.Path(root);self.home=SystemConsole(self.root/'system-console-assets.json')
        self.fonts={};self.fontpath=str(self.root/'ui-font.ttc');self.emoji_fonts={};self.ink_bounds={}
        self.badge_cache={};self.badge_masks={}
        self.home_cache_key=None;self.home_body=None
    def font(self,n):
        if n not in self.fonts:self.fonts[n]=ImageFont.truetype(self.fontpath,n)
        return self.fonts[n]
    def runs(self,value,size):
        # Network/device names are untrusted display data, not layout controls.
        value=''.join(c if unicodedata.category(c) not in ('Cc','Cf') else '�' for c in str(value))
        result=[]
        for emoji,chars in itertools.groupby(value,key=lambda c:ord(c)>=0x1f000 or 0x2600<=ord(c)<=0x27bf):
            part=''.join(chars);f=self.font(size)
            if emoji:
                if size not in self.emoji_fonts:self.emoji_fonts[size]=ImageFont.truetype(str(self.root/'emoji-font.ttf'),size)
                f=self.emoji_fonts[size]
            result.append((part,f))
        return result
    def ink_box(self,value,size):
        key=(value,size)
        if key not in self.ink_bounds:
            f=self.font(size);b=f.getbbox(value,anchor='lt')
            mask=Image.new('L',(max(1,b[2]-b[0])+8,max(1,b[3]-b[1])+8))
            ImageDraw.Draw(mask).text((4-b[0],4-b[1]),value,font=f,fill=255,anchor='lt')
            visible=mask.getbbox() or (4,4,4,4)
            self.ink_bounds[key]=(visible[0]-4+b[0],visible[1]-4+b[1],visible[2]-4+b[0],visible[3]-4+b[1])
        return self.ink_bounds[key]
    def badge_image(self,count):
        if count not in self.badge_cache:
            # One shared, supersampled coordinate system for ring and numeral.
            # Center the visible ink's weight, not the font's asymmetric bearings.
            scale=8;side=44;edge=side*scale;center=edge/2
            ring=Image.new('L',(edge,edge))
            ImageDraw.Draw(ring).ellipse((2*scale,2*scale,edge-2*scale-1,edge-2*scale-1),outline=255,width=2*scale)
            source=Image.new('L',(80*scale,44*scale))
            ImageDraw.Draw(source).text((0,0),str(count),font=self.font(24*scale),fill=255,anchor='lt')
            glyph=source.crop(source.getbbox());pixels=list(glyph.getdata());w,h= glyph.size;weight=sum(pixels)
            cx=sum((i%w+.5)*v for i,v in enumerate(pixels))/weight
            cy=h/2
            digit=Image.new('L',(edge,edge));digit.paste(glyph,(round(center-cx),round(center-cy)))
            ring=ring.resize((side,side),Image.Resampling.LANCZOS)
            digit=digit.resize((side,side),Image.Resampling.LANCZOS)
            from PIL import ImageChops
            alpha=ImageChops.lighter(ring,digit)
            sprite=Image.new('RGBA',(side,side),(172,210,188,0));sprite.putalpha(alpha)
            self.badge_cache[count]=sprite;self.badge_masks[count]=(ring,digit)
        return self.badge_cache[count]
    def draw(self,mem,pitch,app,snapshot):
        if getattr(app,'screen_blank',False):
            zero=b'\0'*(pitch*666);mem[:len(zero)]=zero;return
        im=Image.new('RGB',(1080,666),'black');d=ImageDraw.Draw(im)
        fg='#f1f0eb';muted='#abbab7';accent='#acd2bc';surface='#223630';ink='#17231f'
        def text(x,y,value,size=30,color=fg,maxwidth=950):
            value=str(value)
            def width(v):return sum(d.textlength(part,font=f) for part,f in self.runs(v,size))
            while value and width(value)>maxwidth:value=value[:-2]+'…' if len(value)>2 else ''
            for part,f in self.runs(value,size):
                d.text((x,y),part,font=f,fill=color,anchor='lt');x+=d.textlength(part,font=f)
        def box(rect,color=surface,r=24):
            x,y,w,h=rect;d.rounded_rectangle((x,y,x+w-1,y+h-1),radius=r,fill=color)
        def button(rect,title,color=surface,size=28):
            x,y,w,h=rect
            active=app.pressed and app.pressed_until>time.monotonic() and x<=app.pressed[0]<x+w and y<=app.pressed[1]<y+h
            box(rect,'#425c50' if active else color)
            f=self.font(size);tw=d.textlength(title,font=f);text(x+(w-tw)/2,y+(h-size)/2-1,title,size,maxwidth=w-12)
        def badge(x,y,count):
            if count<=0:return
            sprite=self.badge_image(count)
            target=body if app.page=='home' else im
            target.paste(sprite,(round(x-22),round(y-22)),sprite)
        def title_badge(x,y,title,size,count):
            b=self.ink_box(title,size)
            badge(x+b[2]+37,y+(b[1]+b[3])/2,count)
        titles={'wifi':'Wi-Fi','wifi-detail':'连接 Wi-Fi','confirm-wifi':'确认切换','bluetooth':'蓝牙','bt-detail':'蓝牙设备','bt-confirm':'确认蓝牙设置','phone':'手机状态','server':'服务器状态','keyboard':'Wi-Fi 密码','admin':'超级管理员','admin-confirm':'设备授权',
                'server-resources':'资源占用','server-services':'系统与业务服务','server-storage':'硬盘','server-network':'网络接口',
                'phone-battery':'电池状态','phone-power':'供电状态','phone-display':'显示与触摸','phone-hardware':'硬件信息',
                'recovery':'系统恢复','recovery-confirm':'确认恢复操作','recovery-result':'执行结果','recovery-history':'操作记录'}
        titles.update({'overlay':'固定 IP','overlay-link':'登录授权','overlay-name':'设备名称','overlay-admin':'地址管理'})
        if app.page=='keyboard' and app.keyboard_target=='overlay':titles['keyboard']='设备名称'
        titles['capture']='抓包'
        titles['service-detail']='服务详情'
        titles.update({'power-menu':'电源','power-confirm':'确认关机' if app.power_action=='poweroff' else '确认重启'})
        titles['service-log-entry']='日志内容'
        if app.page=='home':
            pending=len(app.admin.get('pending',[]))
            keys=('server','phone','wifi','bluetooth','admin','recovery','capture','overlay')
            cache_key=(pending,app.overlay.get('label','已关闭'),tuple(app.overlay.get('ips',[])),app.capture.get('label','已关闭'),tuple(tuple(snapshot.get(k,{}).get('lines',[snapshot.get(k,{}).get('label','')])) for k in keys))
            if self.home_body is None or cache_key!=self.home_cache_key:
                # Cache the complete content; drag frames only crop/blit.
                body=Image.new('RGB',(1080,1102),'black');original=d;d=ImageDraw.Draw(body)
                labels={'server':('服务器','#2c3530'),'phone':('手机','#302f39'),'wifi':('Wi-Fi','#223630'),'bluetooth':('蓝牙','#302b3c'),'admin':('超级管理员','#36312b'),'recovery':('系统恢复','#29333b'),'capture':('抓包','#283b3c'),'overlay':('固定 IP','#2d343f')}
                for key,rect in app.home_tiles():
                    x,y,w,h=rect;y+=app.home_scroll;rect=(x,y,w,h)
                    title,color=labels[key];box(rect,color,32)
                    text(x+30,y+30,title,37,maxwidth=430)
                    if key=='admin':title_badge(x+30,y+30,title,37,pending)
                    label=snapshot.get(key,{}).get('label','')
                    if key=='admin':label='设备授权'
                    elif key=='recovery':label='保底操作'
                    elif key=='overlay':label=next((x for x in app.overlay.get('ips',[]) if ':' not in x),app.overlay.get('label','已关闭'))
                    elif key=='capture':label=app.capture.get('label','已关闭')
                    if key in ('server','phone','wifi','bluetooth'):
                        lines=snapshot.get(key,{}).get('lines',[label])
                        for row,line in enumerate(lines[:2]):text(x+30,y+108+row*42,line,26,muted,maxwidth=430)
                    else:text(x+30,y+146,label,27,muted,maxwidth=430)
                self.home_body=body;self.home_cache_key=cache_key;d=original
            body=self.home_body;scroll=app.home_scroll
            im.paste(body.crop((24,24+scroll,1056,624+scroll)),(24,24))
        else:
            button((40,36,82,70),'',size=44)
            d.line([(88,56),(73,71),(88,86)],fill=fg,width=4,joint='curve')
            text(146,48,titles.get(app.page,app.page),38,maxwidth=440 if app.page in ('admin','service-detail') else 640)
        if app.page in ('overlay','overlay-link','overlay-admin','overlay-name'):
            o=app.overlay
            if app.page=='overlay':
                button((824,36,216,70),'读取中' if app.busy else '刷新',size=27)
                box((40,132,1000,118),'#2d343f');text(66,151,o.get('label','读取中'),28,accent,maxwidth=920)
                text(66,195,next((ip for ip in o.get('ips',[]) if ':' not in ip),'尚未分配虚拟 IP'),33,maxwidth=920)
                box((40,268,1000,126),'#262d36');text(66,287,'设备名称',24,muted);text(66,335,o.get('hostname','darklighter-server'),31,maxwidth=920)
                button((40,422,488,84),'断开','#3d302d',32);button((552,422,488,84),'连接','#293b35',32)
                button((40,528,316,76),'登录授权',size=28);button((374,528,316,76),'设备名称',size=28);button((708,528,332,76),'地址管理',size=28)
            elif app.page=='overlay-name':
                box((40,170,1000,110));text(65,205,app.password or '点此输入名称',32,maxwidth=945)
                text(40,321,'小写字母、数字、短横线',28,muted,maxwidth=1000)
                button((552,422,488,84),'保存名称',size=32)
            else:
                value=o.get('login_url','') if app.page=='overlay-link' else o.get('admin_url','https://login.tailscale.com/admin/machines')
                text(40,160,'在电脑打开授权链接' if app.page=='overlay-link' else '在私网后台修改虚拟 IP',32,maxwidth=1000)
                if not value:text(40,248,'正在准备链接' if app.busy else '尚无授权链接，请返回重试',28,muted,maxwidth=1000)
                for n in range(0,min(len(value),240),46):text(40,243+(n//46)*48,value[n:n+46],28,accent,maxwidth=1000)
                if app.page=='overlay-admin':text(40,438,'不是修改 Wi-Fi 地址',28,muted,maxwidth=1000)
            if app.message:text(40,112,app.message,21,'#e5b293',maxwidth=1000)
        if app.page=='capture':
            c=app.capture;button((824,36,216,70),'读取中' if app.busy else '刷新',size=27)
            box((40,136,1000,112));text(66,158,'模块状态',25,muted,maxwidth=350);text(450,157,c.get('label','已关闭'),32,accent,maxwidth=550)
            box((40,268,488,134));text(65,287,'正在采集',25,muted,maxwidth=430);text(65,334,str(c.get('active_sessions',0))+' 个会话',31,maxwidth=430)
            box((552,268,488,134));text(577,287,'抓包 / 丢包',25,muted,maxwidth=430);text(577,334,str(c.get('packets',0))+' / '+str(c.get('drops',0)),31,maxwidth=430)
            button((40,432,488,84),'关闭','#3d302d',32);button((552,432,488,84),'开启','#293b35',32)
            if app.message:text(40,553,app.message,25,'#e5b293',maxwidth=1000)
        elif app.page=='power-menu':
            button((40,244,488,156),'关机','#3d302d',36);button((552,244,488,156),'重启','#293b35',36)
        elif app.page=='power-confirm':
            text(40,172,'关停服务器并关闭手机' if app.power_action=='poweroff' else '关停服务后重新启动',34,maxwidth=1000)
            button((40,432,488,84),'正在关停' if app.power_pending else '取消');button((552,432,488,84),'已提交' if app.power_pending else ('确认关机' if app.power_action=='poweroff' else '确认重启'))
            if app.message:text(40,552,app.message,26,'#e5b293',maxwidth=1000)
        if app.page=='admin':
            title_badge(146,48,'超级管理员',38,len(app.admin.get('pending',[])))
            button((632,36,408,70),'已授权设备' if app.admin_tab=='pending' else '待授权申请',size=27)
            text(40,122,'批准默认普通管理员 · 仅手机可确认',24,accent,maxwidth=1000)
            items=app.admin.get(app.admin_tab,[])
            for n,item in enumerate(items[app.offset:app.offset+2]):
                y=164+n*176;box((40,y,1000,160),'#36312b')
                text(64,y+16,item['name'],30,maxwidth=700)
                text(64,y+62,item['fingerprint'],23,muted,maxwidth=945)
                label=('维护权限保留 · 防锁出保护' if item.get('protected') else '普通终端 · '+item.get('username','')) if app.admin_tab=='devices' else '来源 '+item.get('source','')+' · 等待批准'
                text(64,y+108,label,23,accent,maxwidth=945)
            if not items:text(64,228,'暂无待授权申请' if app.admin_tab=='pending' else '暂无已授权设备',31,muted)
            button((40,544,180,66),'上一页',size=25);button((860,544,180,66),'下一页',size=25)
            if app.message:text(240,563,app.message,23,'#e5b293',maxwidth=595)
        elif app.page=='admin-confirm':
            item=app.admin_selected or {};text(40,143,item.get('name',''),34,maxwidth=1000)
            text(40,208,item.get('fingerprint',''),25,accent,maxwidth=1000)
            if app.admin_tab=='pending':
                text(40,272,'普通管理员：SSH 终端、文件与程序调试',29,muted,maxwidth=1000)
                text(40,322,'独立账号 · 无 root、无限 sudo 和转授权',27,muted,maxwidth=1000)
                button((40,432,488,84),'拒绝');button((552,432,488,84),'允许 · 普通管理员')
            else:
                text(40,272,'当前电脑维护权限暂时保留' if item.get('protected') else '撤销后，新连接及后续操作将被拒绝',29,muted,maxwidth=1000)
                button((552,432,488,84),'受防锁出保护' if item.get('protected') else '撤销授权')
            if app.message:text(40,548,app.message,25,'#e5b293',maxwidth=1000)
        if app.page in ('server','phone'):
            button((824,36,216,70),'读取中' if app.busy else '刷新',size=27)
            if app.page=='server':
                state=app.server.get('ssh_exposure',{});button((560,36,244,70),state.get('label','SSH 读取中'),'#293b35' if state.get('network_enabled') is True else '#3d302d',size=26)
                s=app.server;cpu='采样中' if s.get('cpu_percent') is None else str(s['cpu_percent'])+'%'
                disks=s.get('disks',[]);data=next((v for v in disks if v['path']=='/srv'),{})
                cards=[('资源占用','CPU '+cpu,str(s.get('used_mib','—'))+' MiB 已使用'),('服务状态',str(sum(v['state']=='active' for v in s.get('services',[])))+' 项运行中','系统 / Docker / 1Panel'),('硬盘','已使用 '+('—' if data.get('percent') is None else '%g%%'%data['percent']),'剩余 '+str(data.get('free_gib','—'))+' GiB'),('网络接口',str(len(s.get('interfaces',[])))+' 个接口','地址 / 接收 / 发送')]
            else:
                p=app.phone;cards=[('电池状态',str(p.get('battery','—'))+'%',str(p.get('temperature_c','—'))+'℃'),('供电状态',{'Charging':'充电中','Discharging':'放电中','Not charging':'未充电','Full':'已充满'}.get(p.get('charge'),p.get('charge','读取中')),'USB / 供电来源'),('显示与触摸','触摸已接通' if p.get('touch_devices') else '触摸待检查','显示链路 / 有效区域'),('硬件信息',p.get('machine','—'),'设备 / 内核 / 模块')]
            for n,(title,first,second) in enumerate(cards):
                x=40+(n%2)*512;y=136+(n//2)*216;box((x,y,488,196),'#2d3530' if n%2==0 else '#302f39',30)
                text(x+26,y+24,title,34,maxwidth=430);text(x+26,y+93,first,28,accent,maxwidth=430);text(x+26,y+147,second,22,muted,maxwidth=430)
            if app.message:text(40,568,app.message,25,'#e5b293',maxwidth=1000)
        elif app.page=='service-log-entry':
            entry=app.log_entry or {};text(40,136,app.service_selected or '',25,accent,maxwidth=1000)
            text(40,190,entry.get('time','—'),24,muted,maxwidth=1000)
            value=entry.get('message','');lines=[];current=''
            for c in value:
                if sum(d.textlength(part,font=f) for part,f in self.runs(current+c,29))>970:
                    lines.append(current);current=c
                else:current+=c
            if current:lines.append(current)
            for n,line in enumerate(lines[:6]):text(40,252+n*48,line,29,maxwidth=1000)
        elif app.page.startswith(('server-','phone-')) or app.page in ('recovery-result','recovery-history','service-detail'):
            button((824,36,216,70),'读取中' if app.busy else '刷新',size=27)
            if app.page=='service-detail':button((608,36,196,70),'日志' if app.service_tab=='status' else '状态',size=27)
            rows=app.rows_for_page()
            if app.page=='service-detail':text(40,109,app.service_selected or '',18,muted,maxwidth=1000)
            for n,(name,value) in enumerate(rows[app.offset:app.offset+4]):
                y=132+n*100;box((40,y,1000,84),'#2d3530')
                if app.page=='service-detail' and app.service_tab=='logs':
                    text(62,y+8,name,19,muted,maxwidth=950)
                    text(62,y+38,value,24,maxwidth=950)
                else:
                    text(62,y+27,name,25,muted,maxwidth=410 if app.page=='server-services' else 440);text(510,y+26,value,27,maxwidth=460 if app.page=='server-services' else 505)
                    if app.page=='server-services':text(997,y+25,'›',30,accent,maxwidth=24)
            if not rows:text(64,186,'正在读取' if app.busy else '暂无记录',30,muted)
            button((40,552,180,58),'上一页',size=25);button((860,552,180,58),'下一页',size=25)
            if app.message and not app.busy:text(240,566,app.message,22,'#e5b293',maxwidth=600)
            else:text(467,567,str(app.offset//4+1)+' / '+str(max(1,math.ceil(len(rows)/4))),25,muted,maxwidth=200)
        elif app.page=='recovery':
            button((824,36,216,70),'操作记录',size=27)
            actions=app.recovery.get('actions',[])
            for n,item in enumerate(actions[app.offset:app.offset+4]):
                x=40+(n%2)*512;y=136+(n//2)*216;enabled=item.get('available');box((x,y,488,196),'#293a32' if enabled else '#272929',30)
                text(x+26,y+26,item['title'],33,fg if enabled else muted,maxwidth=435)
                subtitle='已检查 · 可操作' if enabled else item.get('reason','暂不可用')
                if item['action']=='rollback' and item.get('target')=='recovery' and enabled:subtitle='上一版本为机内保底系统'
                text(x+26,y+133,subtitle,23,accent if enabled else muted,maxwidth=435)
            if not actions:text(64,186,'正在检查机内恢复状态',30,muted)
            button((40,552,180,58),'上一页',size=25);button((860,552,180,58),'下一页',size=25)
            if app.message:text(240,568,app.message,22,'#e5b293',maxwidth=595)
            else:text(458,567,str(app.offset//4+1)+' / '+str(max(1,math.ceil(len(actions)/4))),25,muted,maxwidth=200)
        elif app.page=='recovery-confirm':
            plan=app.recovery_plan;text(40,145,plan.get('title',''),36,maxwidth=1000)
            text(40,225,plan.get('effect',''),28,muted,maxwidth=1000)
            text(40,290,'当前系统：'+plan.get('current_slot','—'),28,accent,maxwidth=1000)
            text(40,344,'保留业务数据 · 仅手机实体屏幕可确认',26,muted,maxwidth=1000)
            if plan.get('warning'):text(40,390,plan['warning'],24,'#e5b293',maxwidth=1000)
            button((40,432,488,84),'取消');button((552,432,488,84),'执行中' if app.busy else '确认执行')
            if app.message:text(40,548,app.message,26,'#e5b293',maxwidth=1000)
        if app.page in ('wifi','bluetooth'):
            not_ready=app.page=='bluetooth' and not app.bt.get('ready')
            label=('读取中' if not_ready else '扫描中')+'.'*(getattr(app,'pulse',0)%3+1) if app.busy else ('重新检测' if not_ready else '重新扫描')
            button((824,36,216,70),label)
        if app.page=='wifi':button((608,36,196,70),'附近网络' if app.saved_only else '已保存',size=27)
        if app.page=='bluetooth' and app.bt.get('ready'):button((608,36,196,70),'全部设备' if app.bt_saved_only else '已配对',size=27)
        if app.page=='wifi':
            label=app.wifi.get('ssid') or ('正在读取连接' if 'state' not in app.wifi else '未连接')
            box((40,124,1000,92));text(64,140,label,32,maxwidth=760)
            text(64,181,app.wifi.get('ip') or app.message or '正在读取网络',23,accent,maxwidth=600)
            text(690,182,'自动连接 · 不抢连接' if app.wifi.get('auto_connect_enabled') else '已保存 '+str(len(app.wifi.get('saved_ssids',[])))+' 个网络',21,accent,maxwidth=330)
            connected=next((n for n in app.wifi.get('networks',[]) if n.get('connected')),None)
            if connected:text(878,160,str(connected['signal'])+' dBm',23,accent,maxwidth=140)
            for n,item in enumerate(app.visible_wifi()):
                y=232+n*96;box((40,y,1000,82),surface if item.get('connected') else '#222a2a')
                text(64,y+12,item['ssid'],30,maxwidth=760)
                label='未检测到信号' if item.get('detected') is False else (('已保存 · 自动连接' if app.wifi.get('auto_connect_enabled') else '已保存') if item.get('saved') else ('开放网络' if item.get('open') else '需要密码'))
                text(64,y+51,label,21,accent)
                if item.get('signal') is not None:text(878,y+24,str(item['signal'])+' dBm',23,muted,maxwidth=140)
            if not app.visible_wifi() and not app.busy:text(64,272,app.message or ('其他已保存网络为空' if app.saved_only else '未发现可用网络'),30)
        elif app.page=='bluetooth':
            box((40,124,1000,92),'#302b3c');text(64,140,'蓝牙已开启' if app.bt.get('powered') else app.bt.get('message','正在读取'),32)
            text(64,181,app.bt.get('detail') or app.message or '选择附近设备进行配对',23,'#c5b5de')
            for n,item in enumerate(app.visible_bt()):
                y=232+n*96;box((40,y,1000,82),'#302b3c');text(64,y+12,item['name'],30,maxwidth=940)
                state='已连接' if item.get('connected') else ('已配对' if item.get('paired') else '未配对')
                text(64,y+51,state+' · '+item['address'],21,'#c5b5de')
            if app.bt.get('ready') and not app.visible_bt():text(64,272,app.message or '点击重新扫描查找设备',30)
            if not app.bt.get('ready'):
                diag=app.bt.get('diagnostics',{})
                text(64,258,'UART：'+('已发现' if diag.get('uart') else '未报告'),28,muted)
                text(64,320,'HCI：'+str(diag.get('hci_count',0))+' 个控制器',28,muted)
                text(64,382,'BlueZ：'+('工具已部署' if diag.get('bluez_tools') else '管理工具尚未部署'),28,muted)
                text(64,462,'当前只重新检测，不触发固件或整机重启',24,muted,maxwidth=950)
        elif app.page=='wifi-detail':
            item=app.selected or {};box((40,128,1000,92));text(64,148,item.get('ssid') or item.get('name',''),34,maxwidth=950)
            if app.page=='wifi-detail':
                box((40,250,1000,76),'#252c2c');text(64,271,'●'*min(len(app.password),32) if app.password else '输入密码 · 已保存的网络可留空',29,fg if app.password else muted)
            else:text(64,264,item.get('address',''),28,muted)
            button((40,354,1000,78),'正在连接' if app.busy else '连接',size=32)
            if app.message:text(64,464,app.message,27,'#e5b293',maxwidth=940)
        elif app.page=='bt-detail':
            item=app.selected or {};box((40,128,1000,92),'#302b3c');text(64,143,item.get('name',''),32,maxwidth=940);text(64,186,item.get('address',''),22,'#c5b5de')
            labels=['断开连接' if item.get('connected') else ('连接设备' if item.get('paired') else '配对待适配'),'关闭信任' if item.get('trusted') else '设为可信设备','取消配对']
            for n,title in enumerate(labels):button((40+(n%2)*512,250+(n//2)*104,488,84),title,'#302b3c',29)
            text(40,486,'已配对' if item.get('paired') else '新设备配对代理尚未验收',26,muted,maxwidth=1000)
            if app.message:text(40,548,app.message,25,'#e5b293',maxwidth=1000)
        elif app.page=='bt-confirm':
            labels={'power-on':'开启蓝牙','power-off':'关闭蓝牙','connect':'连接设备','disconnect':'断开设备','trust-on':'设为可信设备','trust-off':'关闭设备信任','forget':'删除配对记录'}
            text(40,145,labels.get(app.bt_action,'蓝牙设置'),36,maxwidth=1000)
            text(40,225,(app.selected or {}).get('name','蓝牙控制器'),32,accent,maxwidth=1000)
            text(40,300,'仅在手机实体屏幕确认后执行',28,muted,maxwidth=1000)
            button((40,432,488,84),'取消');button((552,432,488,84),'确认')
            if app.message:text(40,548,app.message,26,'#e5b293',maxwidth=1000)
        elif app.page=='confirm-wifi':
            text(40,150,(app.selected or {}).get('ssid',''),36,maxwidth=1000)
            text(40,234,'切换网络时，Wi-Fi SSH 会短暂断开。',30,muted)
            text(40,286,'连接失败会尝试恢复原网络。',30,muted)
            if app.message:text(40,354,app.message,26,'#e5b293')
            button((40,432,488,84),'返回修改');button((552,432,488,84),'正在连接' if app.busy else '确认连接')
        elif app.page=='keyboard':
            text(146,96,'固定 IP' if app.keyboard_target=='overlay' else (app.selected or {}).get('ssid','未选择网络'),21,accent,maxwidth=880)
            box((40,128,1000,80),'#252c2c');text(64,151,(app.password if app.keyboard_target=='overlay' else '●'*min(len(app.password),35)) or ('请输入设备名称' if app.keyboard_target=='overlay' else '请输入 Wi-Fi 密码'),30,fg if app.password else muted,maxwidth=940)
            progress=max(0,1-(time.monotonic()-app.keyboard_started)/.15)
            offset=round(12*progress*progress)
            for key,rect in app.keyboard_keys():
                x,y,w,h=rect;rect=(x,y+offset,w,h)
                title={'SHIFT':(('小写' if app.shift else '大写') if app.keyboard_mode=='abc' else '#+='),'BACKSPACE':'删除','MODE':('123' if app.keyboard_mode=='abc' else 'ABC'),'SPACE':'空格','DONE':'完成'}.get(key,key)
                button(rect,title,'#34443d' if key=='DONE' else '#252c2c',30)
        if app.page in ('wifi','bluetooth') and (app.page=='wifi' or app.bt.get('ready')):
            total=len(app.wifi_items()) if app.page=='wifi' else len(app.bt_items())
            button((40,544,180,66),'上一页',size=25);button((860,544,180,66),'下一页',size=25)
            if app.message and not app.busy:text(240,568,app.message,22,'#e5b293',maxwidth=595)
            else:text(458,563,str(app.offset//3+1)+' / '+str(max(1,math.ceil(total/3))),25,muted,maxwidth=200)
        # All pixels outside the previously accepted good screen rectangle stay black.
        d.rectangle((0,624,1079,665),fill='black')
        raw=im.tobytes('raw','BGRX')
        for y in range(666):mem[y*pitch:y*pitch+4320]=raw[y*4320:(y+1)*4320]
