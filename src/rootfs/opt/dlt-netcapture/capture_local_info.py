"""Read-only host capture coverage, socket owners and nl80211 advertised modes."""
import pathlib,subprocess,json,os,socket,struct,time,collections

def attrs(data):
 out=[];pos=0
 while pos+4<=len(data):
  n,t=struct.unpack_from('HH',data,pos)
  if n<4 or pos+n>len(data):raise ValueError('invalid netlink attribute length')
  out.append((t&0x3fff,data[pos+4:pos+n]));pos+=(n+3)&~3
 if pos>len(data)+3:raise ValueError('netlink padding overflow')
 return out

def nla(t,data):return struct.pack('HH',4+len(data),t)+data+b'\0'*((-len(data))%4)

def nl_query(sock,family,cmd,payload=b'',dump=False):
 seq=int(time.monotonic_ns()&0xffffffff);body=struct.pack('BBH',cmd,1,0)+payload;flags=1|(0x300 if dump else 0)
 sock.send(struct.pack('IHHII',16+len(body),family,flags,seq,0)+body);messages=[]
 for _ in range(128):
  raw=sock.recv(1<<20);pos=0
  while pos+16<=len(raw):
   n,typ,fl,sq,pid=struct.unpack_from('IHHII',raw,pos)
   if n<16 or pos+n>len(raw):raise ValueError('invalid netlink message')
   data=raw[pos+16:pos+n];pos+=(n+3)&~3
   if sq!=seq:continue
   if typ==2:
    err=struct.unpack_from('i',data)[0]
    if err:raise OSError(-err,os.strerror(-err))
    continue
   if typ==3:return messages
   if len(data)<4:raise ValueError('short generic-netlink response')
   messages.append(dict(attrs(data[4:])))
   if not dump and not fl&2:return messages
 raise ValueError('netlink response budget exceeded')

def wifi_capabilities():
 sock=socket.socket(socket.AF_NETLINK,socket.SOCK_RAW,16);sock.settimeout(2);sock.bind((0,0))
 try:
  family=nl_query(sock,16,3,nla(2,b'nl80211\0'))[0];fid=struct.unpack('H',family[1])[0]
  replies=nl_query(sock,fid,1,nla(174,b''),dump=True);phys={}
  labels={0:'unspecified',1:'adhoc',2:'managed',3:'AP',4:'AP-VLAN',5:'WDS',6:'monitor',7:'mesh',8:'P2P-client',9:'P2P-GO',10:'P2P-device',11:'OCB',12:'NAN'}
  for row in replies:
   index=struct.unpack('I',row[1])[0] if 1 in row else -1;p=phys.setdefault(index,{'wiphy':index,'name':None,'supported_types':set(),'software_types':set(),'interface_combination_entries':0,'combinations':[]})
   if 2 in row:p['name']=row[2].rstrip(b'\0').decode(errors='replace')
   for key,target in [(32,'supported_types'),(121,'software_types')]:
    if key in row:p[target].update(t for t,b in attrs(row[key]))
   if 120 in row:
    for number,payload in attrs(row[120]):
     combo=dict(attrs(payload));limits=[]
     for number,part in attrs(combo.get(1,b'')):
      limit=dict(attrs(part));limits.append({'max':struct.unpack('I',limit[1])[0],'types':[labels.get(t,str(t)) for t,b in attrs(limit.get(2,b''))]})
     p['combinations'].append({'max_interfaces':struct.unpack('I',combo[2])[0] if 2 in combo else None,'channels':struct.unpack('I',combo[4])[0] if 4 in combo else None,'limits':limits})
    p['interface_combination_entries']=len(p['combinations'])
  result=[]
  for p in phys.values():
   p['monitor_advertised']=6 in p['supported_types'] or 6 in p['software_types']
   p['supported_types']=[labels.get(t,str(t)) for t in sorted(p['supported_types'])];p['software_types']=[labels.get(t,str(t)) for t in sorted(p['software_types'])];p['monitor_runtime_accepted']=False;p['AP_runtime_accepted']=False;result.append(p)
  return {'read_succeeded':True,'wiphys':result,'radio_mode_changed':False,'scan_triggered':False,'advertisement_is_not_runtime_acceptance':True}
 except (OSError,ValueError,KeyError,struct.error) as exc:return {'read_succeeded':False,'error':str(exc),'radio_mode_changed':False,'scan_triggered':False}
 finally:sock.close()

def command(args):
 p=subprocess.run(args,capture_output=True,text=True,timeout=5)
 if p.returncode:raise RuntimeError('read failed: '+args[0])
 return p.stdout

def select_local(links):
 eligible=[];unsupported=[]
 for link in links:
  if 'UP' not in link.get('flags',[]):continue
  if link.get('link_type') in ('ether','loopback'):eligible.append(link['ifname'])
  else:unsupported.append({'interface':link['ifname'],'link_type':link.get('link_type'),'reason':'raw-IP/tunnel link header requires separate link-type implementation'})
 eligible.sort(key=lambda n:(n!='lo',n not in ('wlan0','usb0'),n))
 return {'selected':eligible[:16],'omitted_due_interface_limit':eligible[16:],'active_unsupported_links':unsupported,'selection_scope':'current network namespace, active Ethernet/loopback links at capture start','new_interfaces_after_start':'restart session to include them','whole_LAN_visibility_proven':False}

def namespaces():
 groups={};skipped=0
 for p in pathlib.Path('/proc').iterdir():
  if not p.name.isdecimal():continue
  try:key=str((p/'ns/net').readlink())
  except (OSError,PermissionError):skipped+=1;continue
  entry=groups.setdefault(key,{'namespace':key,'process_count':0,'example_pids':[]});entry['process_count']+=1
  if len(entry['example_pids'])<8:entry['example_pids'].append(int(p.name))
 own=str(pathlib.Path('/proc/self/ns/net').readlink());return {'current':own,'namespaces':list(groups.values()),'vanished_or_unreadable_processes':skipped,'capture_in_other_namespaces_performed':False,'note':'Root veth/bridge may expose container traffic; container loopback requires its own namespace capture.'}

def main():
 import argparse
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('view',choices=['coverage','connections','wifi'],default='coverage',nargs='?');args=p.parse_args()
 if args.view=='wifi':result=wifi_capabilities()
 elif args.view=='connections':
  text=command(['ss','-H','-t','-u','-n','-a','-p']);rows=text.splitlines();result={'version':'E527-local-observer','scope':'socket snapshot in current network namespace','rows':rows[:1024],'truncated':len(rows)>1024,'payload_or_process_arguments_included':False,'PID_to_packet_attribution':'snapshot only; short-lived sockets can disappear and forwarding traffic has no local process owner','network_requests_sent':False}
 else:
  links=json.loads(command(['ip','-j','link']));result={'version':'E527-local-observer','boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'capture_selection':select_local(links),'network_namespaces':namespaces(),'wifi':wifi_capabilities(),'routes_changed':False,'packet_capture_started':False}
 print(json.dumps(result))
if __name__=='__main__':main()
