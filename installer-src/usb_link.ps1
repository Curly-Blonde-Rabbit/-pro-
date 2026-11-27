param([Parameter(Mandatory=$true)][string]$Workspace,[switch]$Restore)
$ErrorActionPreference='Stop'
$backup=Join-Path $Workspace 'usb-adapter-before.json'
if($Restore){
    $b=Get-Content -LiteralPath $backup -Raw | ConvertFrom-Json
    $a=@(Get-CimInstance Win32_NetworkAdapter | Where-Object { $_.GUID -eq $b.GUID -and $_.PNPDeviceID -eq $b.PNPDeviceID })
    if($a.Count -ne 1){throw 'Saved USB adapter is absent; no other interface was changed.'}
    if(-not $b.AddressAlreadyPresent){
        Get-NetIPAddress -InterfaceIndex $a[0].InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object IPAddress -eq '172.31.255.2' | Remove-NetIPAddress -Confirm:$false
    }
    Set-NetIPInterface -InterfaceIndex $a[0].InterfaceIndex -AddressFamily IPv4 -Dhcp $b.Dhcp -PolicyStore ActiveStore
    'Dedicated USB adapter restored; Wi-Fi, default routes and DNS untouched.'
    exit 0
}
$a=@(Get-CimInstance Win32_NetworkAdapter | Where-Object { $_.PNPDeviceID -match 'VID_18D1&PID_4EE3' })
if($a.Count -ne 1){throw 'Exactly one DLT RNDIS USB adapter is required.'}
$a=$a[0]
$all=@(Get-NetIPAddress -AddressFamily IPv4 -IPAddress '172.31.255.2' -ErrorAction SilentlyContinue)
if(@($all | Where-Object InterfaceIndex -ne $a.InterfaceIndex).Count){throw 'USB address conflicts with another interface; no network configuration changed.'}
$exists=[bool]($all | Where-Object { $_.InterfaceIndex -eq $a.InterfaceIndex -and $_.PrefixLength -eq 30 })
if(-not(Test-Path -LiteralPath $backup)){
    $ip=Get-NetIPInterface -InterfaceIndex $a.InterfaceIndex -AddressFamily IPv4
    [pscustomobject]@{GUID=$a.GUID;PNPDeviceID=$a.PNPDeviceID;Dhcp=$ip.Dhcp.ToString();AddressAlreadyPresent=$exists} |
        ConvertTo-Json | Set-Content -LiteralPath $backup -Encoding UTF8
}else{
    $b=Get-Content -LiteralPath $backup -Raw | ConvertFrom-Json
    if($b.GUID -ne $a.GUID -or $b.PNPDeviceID -ne $a.PNPDeviceID){throw 'USB identity changed; baseline preserved.'}
}
if(-not $exists){New-NetIPAddress -InterfaceIndex $a.InterfaceIndex -IPAddress '172.31.255.2' -PrefixLength 30 -PolicyStore ActiveStore | Out-Null}
'Dedicated USB address present; no default gateway or DNS was set.'
