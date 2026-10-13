<#
.SYNOPSIS
  Writes Wi-Fi / broker / CA settings into a board's NVS through its USB serial shell (until BLE
  provisioning, S2b, exists). Dev-PC helper: it talks only to the COM port you name.

.DESCRIPTION
  Secrets are prompted for (SecureString) or read from a file; they are never taken as command-line
  arguments (which would show up in process lists), never echoed and never logged. The CA is sent line by line.
  The board answers `ok` / `err ...` per line and never prints secrets back.

.EXAMPLE
  .\provision-serial.ps1 -Port COM5 -WifiSsid HomeNet -MqttHost 192.168.1.10 -MqttUser esp32-a1b2c3d4e5f6 `
      -CaFile ..\..\deploy\mosquitto\certs\ca.crt
  (prompts for the Wi-Fi and MQTT passwords)
#>
param(
    [Parameter(Mandatory)] [string] $Port,
    [Parameter(Mandatory)] [string] $WifiSsid,
    [Parameter(Mandatory)] [string] $MqttHost,
    [Parameter(Mandatory)] [string] $MqttUser,
    [Parameter(Mandatory)] [string] $CaFile,
    [int] $MqttPort = 8883,
    [string] $NtpHost = '',
    [string] $WifiPasswordFile = '',   # optional: file whose first line is the Wi-Fi password
    [string] $MqttPasswordFile = '',   # optional: file whose first line is the MQTT password
    [switch] $NoSave                   # send everything but do not run `save` (board does not reboot)
)
$ErrorActionPreference = 'Stop'

function Read-Secret([string] $prompt, [string] $file) {
    if ($file) { return (Get-Content -LiteralPath $file -TotalCount 1) }
    $s = Read-Host -Prompt $prompt -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}

if (-not (Test-Path -LiteralPath $CaFile)) { throw "CA file not found: $CaFile" }
$ca = Get-Content -LiteralPath $CaFile
if ($ca[0] -notmatch '^-----BEGIN CERTIFICATE-----') { throw 'CA file is not a PEM certificate' }
if ($ca -match 'PRIVATE KEY') { throw 'Refusing to send a file that contains a private key' }

$wifiPass = Read-Secret 'Wi-Fi password (empty for an open network)' $WifiPasswordFile
$mqttPass = Read-Secret 'MQTT password for this board' $MqttPasswordFile

$sp = New-Object System.IO.Ports.SerialPort $Port, 115200, 'None', 8, 'One'
$sp.NewLine = "`n"
$sp.ReadTimeout = 3000
$sp.DtrEnable = $false   # do not reset the board when opening the port
$sp.RtsEnable = $false
$sp.Open()
Start-Sleep -Milliseconds 300
$sp.DiscardInBuffer()

function Send([string] $label, [string] $line) {
    $sp.WriteLine($line)
    $reply = $null
    for ($i = 0; $i -lt 40 -and -not $reply; $i++) {   # skip the firmware's own "[edge] ..." log lines
        try { $l = $sp.ReadLine().Trim() } catch { throw "No reply to '$label' (is the firmware running and the port correct?)" }
        if ($l -match '^(ok|err|config:|commands:)') { $reply = $l }
    }
    if (-not $reply) { throw "No reply to '$label'" }
    if ($reply -like 'err*') { throw "Board rejected '$label': $reply" }
    Write-Host ("{0,-28} {1}" -f $label, $reply)
}

try {
    Send 'set wifi_ssid'  "set wifi_ssid $WifiSsid"
    Send 'set wifi_pass'  "set wifi_pass $wifiPass"
    Send 'set mqtt_host'  "set mqtt_host $MqttHost"
    Send 'set mqtt_port'  "set mqtt_port $MqttPort"
    Send 'set mqtt_user'  "set mqtt_user $MqttUser"
    Send 'set mqtt_pass'  "set mqtt_pass $mqttPass"
    if ($NtpHost) { Send 'set ntp_host' "set ntp_host $NtpHost" }
    Send 'ca clear' 'ca clear'
    foreach ($l in $ca) { if ($l.Trim()) { Send 'ca line' ("ca line " + $l.Trim()) } }
    Send 'ca end' 'ca end'
    Send 'status' 'status'
    if (-not $NoSave) { Send 'save' 'save' }
} finally {
    $sp.Close()
    $wifiPass = $null; $mqttPass = $null
}
