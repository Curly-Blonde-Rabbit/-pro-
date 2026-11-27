"""Read-only capture topology inventory. No probes, packet sockets or reconfiguration."""
import json,pathlib,subprocess,socket,ipaddress

def read(path):
 try:return pathlib.Path(path).read_text().strip()
 except OSError:return None

def command(args):
 p=subprocess.run(args,capture_output=True,text=True,timeout=5)
 if p.returncode:raise RuntimeError('inventory failed: '+args[0])
 return json.loads(p.stdout)

def evaluate(links,addresses,routes,ssh_connection,forwarding):
 management={r.get('dev') for r in routes if r.get('dst')=='default'}|{'usb0','wlan0'}
 if ssh_connection:
  fields=ssh_connection.split()
  if len(fields)==4:
   for a in addresses:
    if any(x.get('local')==fields[2] for x in a.get('addr_info',[])):management.add(a['ifname'])
 rows=[]
 for link in links:
  name=link['ifname'];addr=next((x.get('addr_info',[]) for x in addresses if x['ifname']==name),[]);reasons=[];kind=link.get('linkinfo',{}).get('info_kind')
  if name in management:reasons.append('current-management-or-default-route-interface')
  if link.get('link_type')!='ether':reasons.append('not-an-Ethernet-style-interface')
  if kind in ('dummy','bond','veth','bridge','vti','vti6','sit','ip6tnl'):reasons.append('virtual-interface-not-physical-mirror-proof')
  if name.startswith(('p2p','wlan')):reasons.append('wireless-managed-or-P2P-not-wired-mirror-ingress')
  if addr:reasons.append('has-IPv4-or-IPv6-address')
  if 'UP' not in link.get('flags',[]):reasons.append('interface-not-up')
  if 'LOWER_UP' not in link.get('flags',[]):reasons.append('carrier-not-confirmed')
  rows.append({'interface':name,'ifindex':link['ifindex'],'link_type':link.get('link_type'),'kind':kind,'state':link.get('operstate'),'MAC':link.get('address'),'addresses':[x.get('local') for x in addr],'management_protected':name in management,'mirror_candidate':not reasons,'reasons':reasons,'physical_mirror_hardware_accepted':False})
 return {'interfaces':rows,'management_interfaces':sorted(x for x in management if x),'mirror_candidates':[x['interface'] for x in rows if x['mirror_candidate']],'IPv4_forwarding':forwarding,'whole_LAN_visibility_proven':False,'mirror_preflight_is_not_receive_only_hardware_proof':True,'required_ingress':'router/switch mirror or TAP; alternatively actual inline forwarding path','probe_packets_sent':False,'network_changes':False}

def main():
 import os
 links=command(['ip','-j','-d','link']);addresses=command(['ip','-j','addr']);routes=command(['ip','-j','route']);out=evaluate(links,addresses,routes,os.environ.get('SSH_CONNECTION'),read('/proc/sys/net/ipv4/ip_forward'))
 out.update(version='E517-topology-readonly',boot_id=read('/proc/sys/kernel/random/boot_id'),USB_devices=[])
 for p in pathlib.Path('/sys/bus/usb/devices').glob('*'):
  if (p/'idVendor').is_file():out['USB_devices'].append({k:read(p/k) for k in ['idVendor','idProduct','product','speed']})
 print(json.dumps(out))
if __name__=='__main__':main()
