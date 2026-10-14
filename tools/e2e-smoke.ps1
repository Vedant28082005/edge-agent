<#
.SYNOPSIS
  Local end-to-end smoke test (amd64, Docker): the REAL device-hub, gateway and agent images on the production compose
  file, a dev mosquitto with TLS + dynsec, a mock LLM backend and the S2c simulator as a node.
.DESCRIPTION
  Everything runs under a unique compose project (s11-<random>) on ephemeral loopback ports and a private subnet; all
  generated material (dev CA, secrets, data) lives in the git-ignored deploy\e2e-work\<project>\ and is deleted at the
  end (-Keep to inspect). Only what this script started is torn down: the sim by its PID, the stack by its project name.
  Scenario: bootstrap-admin, login, provision the simulated nodes, approve + name endpoints, "turn on the bedroom fan"
  through POST /chat (cmd -> ack -> state -> final SSE), a risk:confirm action through the full token flow, a
  room-restricted guest being refused, a WebSocket ticket connect, a forget that revokes the node's MQTT access.
  Prints PASS/FAIL per step; exit code 1 on any failure, 2 if a precondition is missing.
.PARAMETER SkipBuild
  Reuse the existing edge-agent/<svc>:dev amd64 images instead of building them.
#>
param([switch]$SkipBuild, [switch]$Keep, [string]$Tag = 'dev')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$suffix = -join ((1..4) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })
$project = "s11-$suffix"
$work = Join-Path $root "deploy\e2e-work\$project"
$script:results = @()
$script:simProc = $null
$script:bg = @()

function Step([string]$name, [scriptblock]$body) {
    try {
        $r = & $body
        if ($r -eq $false) { throw 'returned false' }
        Write-Host "PASS $name" -ForegroundColor Green
        $script:results += [pscustomobject]@{ Name = $name; Ok = $true }
    } catch {
        $m = ($_.Exception.Message -replace '\s+', ' ')
        if ($m.Length -gt 300) { $m = $m.Substring(0, 300) }
        Write-Host "FAIL $name : $m" -ForegroundColor Red
        $script:results += [pscustomobject]@{ Name = $name; Ok = $false }
    }
}
function Need([bool]$c, [string]$msg) { if (-not $c) { throw $msg } }
function Fwd([string]$p) { $p.Replace('\', '/') }
function Rand([int]$n) { $b = New-Object byte[] $n; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); ([Convert]::ToBase64String($b)).TrimEnd('=').Replace('+', 'A').Replace('/', 'b') }
function Free-Port { $l = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0); $l.Start(); $p = $l.LocalEndpoint.Port; $l.Stop(); $p }
function Sha256Hex([string]$s) { ($(New-Object Security.Cryptography.SHA256Managed).ComputeHash([Text.Encoding]::ASCII.GetBytes($s)) | ForEach-Object { $_.ToString('x2') }) -join '' }
function Put([string]$path, [string]$text) { New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null; [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding($false))) }

# ---------------------------------------------------------------- preconditions
docker version --format '{{.Server.Version}}' 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host 'Docker is not available.'; exit 2 }
if (-not (Get-Command openssl -ErrorAction SilentlyContinue)) {
    $gitSsl = 'C:\Program Files\Git\usr\bin'
    if (Test-Path "$gitSsl\openssl.exe") { $env:PATH += ";$gitSsl" } else { Write-Host 'openssl not found on PATH.'; exit 2 }
}
$simPy = Join-Path $root 'tools\sim-esp32\.venv\Scripts\python.exe'
if (-not (Test-Path $simPy)) { & (Join-Path $root 'tools\sim-esp32\run-tests.ps1') | Out-Null }
if (-not (Test-Path $simPy)) { Write-Host 'sim-esp32 venv could not be created.'; exit 2 }
if (-not $SkipBuild) { & (Join-Path $PSScriptRoot 'build-images.ps1') -Platform amd64 -Tag $Tag; if ($LASTEXITCODE) { exit 2 } }
foreach ($s in 'device-hub', 'gateway', 'agent') {
    docker image inspect "edge-agent/${s}:$Tag" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "image edge-agent/${s}:$Tag missing (run without -SkipBuild)"; exit 2 }
}

# ---------------------------------------------------------------- environment
$mqPort = Free-Port; $gwPort = Free-Port
$used = @(docker network ls -q | ForEach-Object { docker network inspect -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' $_ }) -join ' '
do { $oct = Get-Random -Minimum 21 -Maximum 250; $subnet = "172.30.$oct.0/24" } while ($used -like "*$subnet*")
$edgeGw = "172.30.$oct.1"
$mq = Join-Path $work 'mq'; $sec = Join-Path $work 'secrets'; $cfg = Join-Path $work 'config'; $data = Join-Path $work 'data'
New-Item -ItemType Directory -Force $mq, $sec, $cfg, $data | Out-Null
$compose = @('compose', '-p', $project, '-f', (Join-Path $root 'deploy\compose.yml'), '-f', (Join-Path $work 'compose.e2e.yml'))
$env:EDGE_TAG = $Tag; $env:MOSQUITTO_CONTAINER = "$project-mq"
$env:MOSQUITTO_PUBLISH = "127.0.0.1:$mqPort"; $env:GATEWAY_PUBLISH = "127.0.0.1:$gwPort"
$env:EDGE_SUBNET = $subnet; $env:EDGE_GATEWAY_IP = $edgeGw
$env:MOSQUITTO_STATE_DIR = Fwd $mq; $env:SECRETS_DIR = Fwd $sec; $env:CONFIG_DIR = Fwd $cfg; $env:DATA_DIR = Fwd $data
$base = "http://127.0.0.1:$gwPort"
Write-Host "project $project  gateway $base  broker 127.0.0.1:$mqPort  subnet $subnet"

function Api([string]$method, [string]$path, $body = $null, [string]$token = '') {
    $h = @{}; if ($token) { $h['Authorization'] = "Bearer $token" }
    $a = @{ Uri = "$base$path"; Method = $method; Headers = $h; UseBasicParsing = $true; TimeoutSec = 30 }
    if ($null -ne $body) { $a['Body'] = ($body | ConvertTo-Json -Depth 10 -Compress); $a['ContentType'] = 'application/json' }
    try { $r = Invoke-WebRequest @a; $code = [int]$r.StatusCode; $txt = $r.Content }
    catch [System.Net.WebException] {
        if (-not $_.Exception.Response) { throw }
        $code = [int]$_.Exception.Response.StatusCode
        $txt = (New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())).ReadToEnd()
    }
    $j = $null; if ($txt) { try { $j = $txt | ConvertFrom-Json } catch { } }
    [pscustomobject]@{ Status = $code; Json = $j; Text = $txt }
}
function Wait-Until([scriptblock]$cond, [int]$sec, [string]$what) {
    $end = (Get-Date).AddSeconds($sec)
    while ((Get-Date) -lt $end) { $v = & $cond; if ($v) { return $v }; Start-Sleep -Milliseconds 500 }
    throw "timeout waiting for $what"
}
function Start-Sse([string]$token, [string]$text, [string]$session, [int]$maxSec, [string]$out) {
    $bodyFile = "$out.body"; Put $bodyFile (@{ session_id = $session; text = $text } | ConvertTo-Json -Compress)
    $p = Start-Process -FilePath curl.exe -ArgumentList @('-sN', '--max-time', "$maxSec", '-H', '"Authorization: Bearer '+$token+'"', '-H', '"Content-Type: application/json"', '--data-binary', "@$bodyFile", "$base/chat", '-o', $out, '-D', "$out.hdr", '-S') -RedirectStandardError "$out.err" -PassThru -WindowStyle Hidden
    $script:bg += $p.Id
    $p
}
function Sse-Events([string]$file) {
    if (-not (Test-Path $file)) { return @() }
    $t = Get-Content $file -Raw -ErrorAction SilentlyContinue
    if (-not $t) { return @() }
    @([regex]::Matches($t, '(?m)^event: (\S+)\r?\ndata: (.*)$') | ForEach-Object { [pscustomobject]@{ Name = $_.Groups[1].Value; Data = $_.Groups[2].Value.Trim() } })
}
function Dc { $p = $ErrorActionPreference; $ErrorActionPreference = 'Continue'; try { & docker @compose @args } finally { $ErrorActionPreference = $p } }

try {
    # ------------------------------------------------------------ generated dev material (git-ignored work dir)
    Step 'generate dev CA, broker cert, dynsec, service secrets and config' {
        & (Join-Path $root 'deploy\mosquitto\new-certs.ps1') -LanIp 127.0.0.1 -TailnetName e2e.test.ts.net -ExtraDns mosquitto -OutDir (Join-Path $mq 'certs') | Out-Null
        Need (Test-Path (Join-Path $mq 'certs\server.key')) 'cert generation failed'
        $hubPw = Rand 24
        New-Item -ItemType Directory -Force (Join-Path $mq 'data') | Out-Null
        $prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        docker run --rm -v "$(Fwd (Join-Path $mq 'data')):/mosquitto/data" eclipse-mosquitto:2.0.20 mosquitto_ctrl dynsec init /mosquitto/data/dynamic-security.json hub-admin $hubPw 2>&1 | Out-Null
        $ErrorActionPreference = $prev
        $ds = Join-Path $mq 'data\dynamic-security.json'
        Need (Test-Path $ds) 'dynsec init failed'
        $j = Get-Content $ds -Raw | ConvertFrom-Json
        foreach ($n in @($j.defaultACLAccess.PSObject.Properties.Name)) { $j.defaultACLAccess.$n = $false }
        $adminRole = $j.roles | Where-Object { $_.rolename -eq 'admin' }
        $adminRole.acls += [pscustomobject]@{ acltype = 'publishClientSend'; topic = '#'; allow = $true }
        $j | ConvertTo-Json -Depth 20 | Set-Content -Encoding ascii $ds
        # secrets exactly as tools/setup-pi-brain.sh lays them out (no trailing newline; verifier = sha256 hex of the secret)
        Put (Join-Path $sec 'hub-mqtt.password') $hubPw
        foreach ($p in @(@('device-hub', 'verifiers'), @('gateway', 'hub-verifiers'), @('agent', 'hub-verifiers'), @('agent-api', 'agent-verifiers'))) {
            $s = Rand 32
            Put (Join-Path $sec "$($p[0]).secret") $s
            Put (Join-Path $sec "$($p[1])\$($p[0]).sha256") (Sha256Hex $s)
        }
        # non-secret config from the shipped templates
        (Get-Content (Join-Path $root 'deploy\config\device-hub.json.template') -Raw).Replace('__BROKER_HOST__', '127.0.0.1') | Set-Content -Encoding ascii (Join-Path $cfg 'device-hub.json')
        Copy-Item (Join-Path $root 'deploy\config\gateway.json') $cfg
        (Get-Content (Join-Path $root 'deploy\config\agent.json') -Raw).Replace('http://host.docker.internal:8080', 'http://llm:8080') | Set-Content -Encoding ascii (Join-Path $cfg 'agent.json')
        foreach ($d in 'device-hub', 'gateway', 'agent') { New-Item -ItemType Directory -Force (Join-Path $data $d) | Out-Null }
        # mock LLM backend: the agent image running tools/e2e/mock_llm_server.py (ScriptedLLM behind llama-server's HTTP routes)
        $mock = Fwd (Join-Path $root 'tools\e2e\mock_llm_server.py')
        Put (Join-Path $work 'compose.e2e.yml') @"
services:
  llm:
    image: edge-agent/agent:$Tag
    entrypoint: ["python", "/e2e/mock_llm_server.py"]
    command: []
    environment: {PYTHONPATH: /app}
    user: "10103:10103"
    read_only: true
    tmpfs: ["/tmp"]
    networks: [edge]
    pull_policy: never
    restart: "no"
    mem_limit: 128m
    healthcheck: {disable: true}
    volumes:
      - ${mock}:/e2e/mock_llm_server.py:ro
  agent:
    depends_on:
      llm: {condition: service_started}
"@
        $true
    }

    Step 'docker compose config (production file + e2e override) is valid' {
        & docker @compose config -q 2>&1 | Out-Null
        $LASTEXITCODE -eq 0
    }
    Step 'stack starts and device-hub, gateway, agent report healthy' {
        $o = Dc up -d 2>&1
        Need ($LASTEXITCODE -eq 0) ($o -join ' ')
        foreach ($svc in 'device-hub', 'gateway', 'agent') {
            Wait-Until { $id = (Dc ps -q $svc); $id -and ((docker inspect -f '{{.State.Health.Status}}' $id) -eq 'healthy') } 150 "$svc healthy" | Out-Null
        }
        $true
    }
    Step 'only gateway (loopback) and mosquitto are reachable from the host; 8100/8200/8001 are not' {
        $ports = @(Dc ps --format json | ForEach-Object { $_ | ConvertFrom-Json } | ForEach-Object { $_.Publishers } | Where-Object { $_.PublishedPort } | ForEach-Object { "$($_.TargetPort)" })
        Need ((($ports | Sort-Object -Unique) -join ',') -eq '8000,8883') "published targets: $($ports -join ',')"
        foreach ($p in 8100, 8200, 8001) {
            $c = New-Object Net.Sockets.TcpClient; $ar = $c.BeginConnect('127.0.0.1', $p, $null, $null)
            $open = $ar.AsyncWaitHandle.WaitOne(500) -and $c.Connected; $c.Close()
            Need (-not $open) "port $p answers on the host"
        }
        $true
    }

    # ------------------------------------------------------------ admin, login
    $adminPw = (Rand 18) + 'aA1'
    Step 'gateway bootstrap-admin (password via stdin) creates the first admin' {
        $pp = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        $o = $adminPw | & docker @compose run --rm -T gateway --config /etc/edge-agent/config/gateway.json bootstrap-admin --username admin --password-stdin 2>&1
        Need ($LASTEXITCODE -eq 0 -and ($o -join ' ') -match "admin 'admin' created") ($o -join ' ')
        $true
    }
    $tok = $null
    Step 'POST /auth/login (admin) returns tokens; wrong password is 401' {
        $r = Api POST '/auth/login' @{ username = 'admin'; password = $adminPw }
        Need ($r.Status -eq 200 -and $r.Json.access_token) "status $($r.Status)"
        $script:tok = $r.Json.access_token
        $bad = Api POST '/auth/login' @{ username = 'admin'; password = 'wrong-password-123' }  # scan-ok: test fixture, not a credential
        Need ($bad.Status -eq 401) "wrong password gave $($bad.Status)"
        $true
    }
    $n1 = 'esp32-a1b2c3d4e501'; $n2 = 'esp32-a1b2c3d4e502'

    # ------------------------------------------------------------ provision + simulator
    $credDir = Join-Path $work 'creds'; New-Item -ItemType Directory -Force $credDir | Out-Null
    Step 'provision both simulated nodes (hub creates MQTT users through the dynsec API)' {
        foreach ($n in $n1, $n2) {
            $r = Api POST '/admin/provision-sessions' @{ node_id = $n; wifi = @{ ssid = 'e2e-sim'; password = 'e2e-sim-wifi-pw' } } $tok  # scan-ok: test fixture, not a credential
            Need ($r.Status -eq 201 -and $r.Json.bundle.mqtt.username -eq $n) "$n -> $($r.Status) $($r.Text.Length)"
            Put (Join-Path $credDir "$n.cred") "username=$($r.Json.bundle.mqtt.username)`npassword=$($r.Json.bundle.mqtt.password)`n"
        }
        $true
    }
    $yaml = Join-Path $work 'sim.yaml'
    Put $yaml @"
nodes:
  - node_id: $n1
    heartbeat_s: 30
    endpoints:
      - local_id: ch1
        type: fan
        capabilities:
          - {id: power, boot_value: false, failsafe_value: false}
          - {id: speed, min: 1, max: 5, boot_value: 1}
  - node_id: $n2
    heartbeat_s: 30
    endpoints:
      - local_id: d1
        type: lock
        capabilities:
          - {id: lock, boot_value: locked, failsafe_value: locked}
"@
    $simOut = Join-Path $work 'sim.out.log'; $simErr = Join-Path $work 'sim.err.log'; $stopFile = Join-Path $work 'sim.stop'
    Step 'start the S2c simulator (TLS to the dev broker, real credentials)' {
        $script:simProc = Start-Process -FilePath $simPy -ArgumentList @('-u', (Join-Path $root 'tools\e2e\run_sim.py'), $stopFile, $yaml, '--host', 'localhost', '--port', "$mqPort", '--ca', (Join-Path $mq 'certs\ca.crt'), '--creds-dir', $credDir) -RedirectStandardOutput $simOut -RedirectStandardError $simErr -PassThru -WindowStyle Hidden
        Wait-Until { (Test-Path $simOut) -and ((Get-Content $simOut -Raw) -match 'node\(s\) running') } 30 'sim start' | Out-Null
        $true
    }
    Step 'announced nodes appear as pending; approve and name their endpoints' {
        Wait-Until { $r = Api GET '/admin/nodes' $null $tok; $r.Status -eq 200 -and @($r.Json | Where-Object { $_.node_id -in $n1, $n2 }).Count -eq 2 } 40 'both nodes announced' | Out-Null
        $a = Api POST "/admin/nodes/$n1/approve" @{ endpoints = @(@{ local_id = 'ch1'; name = 'Bedroom Fan'; room = 'Bedroom' }) } $tok
        Need ($a.Status -eq 200) "approve fan: $($a.Status) $($a.Text)"
        $b = Api POST "/admin/nodes/$n2/approve" @{ endpoints = @(@{ local_id = 'd1'; name = 'Front Door'; room = 'Hall'; risk = 'confirm' }) } $tok
        Need ($b.Status -eq 200) "approve lock: $($b.Status) $($b.Text)"
        $true
    }
    $fanId = $null; $lockId = $null
    Step 'GET /devices lists both endpoints online with the admin-set names and risk' {
        $d = Wait-Until { $r = Api GET '/devices' $null $tok; if ($r.Status -eq 200 -and @($r.Json | Where-Object { $_.online }).Count -eq 2) { $r.Json } } 30 'endpoints online'
        $fan = $d | Where-Object { $_.name -eq 'Bedroom Fan' }; $lock = $d | Where-Object { $_.name -eq 'Front Door' }
        Need ($fan -and $lock -and $lock.risk -eq 'confirm' -and $fan.room -eq 'Bedroom') 'registry content wrong'
        $script:fanId = $fan.endpoint_id; $script:lockId = $lock.endpoint_id
        $true
    }

    # ------------------------------------------------------------ chat: "turn on the bedroom fan"
    Step 'POST /chat "turn on the bedroom fan": tool call, verified result, final SSE event' {
        $out = Join-Path $work 'chat1.sse'
        $p = Start-Sse $tok 'turn on the bedroom fan' 'e2e-1' 90 $out
        Wait-Until { @(Sse-Events $out | Where-Object { $_.Name -eq 'final' }).Count -ge 1 } 80 'final SSE event' | Out-Null
        $ev = Sse-Events $out
        Need (@($ev | Where-Object { $_.Name -eq 'tool_call' }).Count -ge 1) 'no tool_call event'
        $tr = @($ev | Where-Object { $_.Name -eq 'tool_result' })
        Need ($tr.Count -ge 1 -and $tr[0].Data -match '"ok":\s*true' -and $tr[0].Data -match '"verified":\s*true') "tool_result: $($tr[0].Data)"
        $true
    }
    Step 'sim node state converged (power on) as seen through GET /devices' {
        Wait-Until { $r = Api GET '/devices' $null $tok; ($r.Json | Where-Object { $_.endpoint_id -eq $fanId }).state.power -eq $true } 15 'fan power true' | Out-Null
        $true
    }

    # ------------------------------------------------------------ risk:confirm full token flow
    Step 'risk:confirm "unlock the front door": confirmation_request, approve, token consumed once, executed, final' {
        $out = Join-Path $work 'chat2.sse'
        $p = Start-Sse $tok 'unlock the front door' 'e2e-2' 120 $out
        $req = Wait-Until { Sse-Events $out | Where-Object { $_.Name -eq 'confirmation_request' } | Select-Object -First 1 } 60 'confirmation_request'
        $rid = ($req.Data | ConvertFrom-Json).request_id
        Need ([bool]$rid) 'no request_id'
        $d0 = (Api GET '/devices' $null $tok).Json | Where-Object { $_.endpoint_id -eq $lockId }
        Need ($d0.state.lock -eq 'locked') "lock moved before approval: $($d0.state.lock)"
        $m = Api POST '/confirmations' @{ request_id = $rid } $tok
        Need ($m.Status -eq 200 -and $m.Json.status -eq 'delivered_to_agent' -and -not $m.Json.confirmation_token) "mint: $($m.Status) $($m.Text)"
        Wait-Until { @(Sse-Events $out | Where-Object { $_.Name -eq 'final' }).Count -ge 1 } 60 'final after approval' | Out-Null
        Wait-Until { ((Api GET '/devices' $null $tok).Json | Where-Object { $_.endpoint_id -eq $lockId }).state.lock -eq 'unlocked' } 15 'lock unlocked' | Out-Null
        $all = Get-Content $out -Raw
        Need ($all -notmatch '"confirmation_token"') 'a confirmation_token appeared in the SSE stream'
        $again = Api POST '/confirmations' @{ request_id = $rid } $tok
        Need ($again.Status -in 404, 409) "second approval of the same request was $($again.Status)"
        $true
    }
    Step 'risk:confirm without a token is refused by the gateway (409, nothing executed)' {
        $before = ((Api GET '/devices' $null $tok).Json | Where-Object { $_.endpoint_id -eq $lockId }).state.lock
        $r = Api POST "/devices/$lockId/set" @{ capability = 'lock'; value = 'locked' } $tok
        Need ($r.Status -eq 409) "status $($r.Status): $($r.Text)"
        Start-Sleep -Milliseconds 800
        $after = ((Api GET '/devices' $null $tok).Json | Where-Object { $_.endpoint_id -eq $lockId }).state.lock
        Need ($before -eq $after) 'state changed without a token'
        $true
    }

    # ------------------------------------------------------------ room-restricted guest
    $gtok = $null
    Step 'guest restricted to Bedroom: sees only Bedroom, cannot set, can never mint a confirmation' {
        $gpw = (Rand 14) + 'gG1'
        $c = Api POST '/admin/users' @{ username = 'guest1'; password = $gpw; role = 'guest'; rooms = @('Bedroom') } $tok
        Need ($c.Status -eq 201) "create guest: $($c.Status) $($c.Text)"
        $l = Api POST '/auth/login' @{ username = 'guest1'; password = $gpw }
        Need ($l.Status -eq 200) "guest login $($l.Status)"
        $script:gtok = $l.Json.access_token
        $dev = Api GET '/devices' $null $gtok
        Need ($dev.Status -eq 200 -and @($dev.Json).Count -eq 1 -and $dev.Json[0].room -eq 'Bedroom') "guest device list: $($dev.Text)"
        $s = Api POST "/devices/$fanId/set" @{ capability = 'power'; value = $false } $gtok
        Need ($s.Status -eq 403) "guest set fan -> $($s.Status)"
        $s2 = Api POST "/devices/$lockId/set" @{ capability = 'lock'; value = 'unlocked' } $gtok
        Need ($s2.Status -in 403, 404) "guest set lock -> $($s2.Status)"
        $m = Api POST '/confirmations' @{ action = @{ type = 'set_state'; endpoint_id = $lockId; capability = 'lock'; value = 'unlocked' } } $gtok
        Need ($m.Status -eq 403) "guest mint -> $($m.Status)"
        $a = Api GET '/admin/users' $null $gtok
        Need ($a.Status -eq 403) "guest admin -> $($a.Status)"
        $true
    }

    # ------------------------------------------------------------ websocket ticket
    Step 'WebSocket: connect with a one-time ticket in Sec-WebSocket-Protocol; a reused ticket and a URL JWT are refused' {
        $t = Api POST '/ws/ticket' $null $tok
        Need ($t.Status -eq 200 -and $t.Json.ticket) "ticket $($t.Status)"
        $ws = New-Object Net.WebSockets.ClientWebSocket
        $ws.Options.AddSubProtocol("edge-ticket.$($t.Json.ticket)")
        $cts = New-Object Threading.CancellationTokenSource(10000)
        $ws.ConnectAsync([Uri]"ws://127.0.0.1:$gwPort/ws", $cts.Token).Wait()
        Need ($ws.State -eq 'Open') "state $($ws.State)"
        # a state change must reach the socket: flip the fan off over REST and read frames
        Api POST "/devices/$fanId/set" @{ capability = 'power'; value = $false } $tok | Out-Null
        $buf = New-Object byte[] 8192; $got = $false; $end = (Get-Date).AddSeconds(15)
        while (-not $got -and (Get-Date) -lt $end) {
            $c2 = New-Object Threading.CancellationTokenSource(3000)
            try { $res = $ws.ReceiveAsync((New-Object ArraySegment[byte] -ArgumentList @(, $buf)), $c2.Token).Result
                  $msg = [Text.Encoding]::UTF8.GetString($buf, 0, $res.Count); if ($msg -match '"type"\s*:\s*"state"') { $got = $true } } catch { }
        }
        $ws.Dispose()
        Need $got 'no state message on the socket'
        $ws2 = New-Object Net.WebSockets.ClientWebSocket; $ws2.Options.AddSubProtocol("edge-ticket.$($t.Json.ticket)")
        $refused = $false; try { $ws2.ConnectAsync([Uri]"ws://127.0.0.1:$gwPort/ws", (New-Object Threading.CancellationTokenSource(8000)).Token).Wait() } catch { $refused = $true }
        if (-not $refused) { try { $b2 = New-Object byte[] 512; $null = $ws2.ReceiveAsync((New-Object ArraySegment[byte] -ArgumentList @(, $b2)), (New-Object Threading.CancellationTokenSource(4000)).Token).Result; $refused = ($ws2.State -ne 'Open') } catch { $refused = $true } }
        Need $refused 'a used ticket was accepted again'
        $ws3 = New-Object Net.WebSockets.ClientWebSocket
        $jwtRefused = $false; try { $ws3.ConnectAsync([Uri]"ws://127.0.0.1:$gwPort/ws?token=$tok", (New-Object Threading.CancellationTokenSource(8000)).Token).Wait() } catch { $jwtRefused = $true }
        if (-not $jwtRefused) { try { $b3 = New-Object byte[] 512; $null = $ws3.ReceiveAsync((New-Object ArraySegment[byte] -ArgumentList @(, $b3)), (New-Object Threading.CancellationTokenSource(7000)).Token).Result; $jwtRefused = ($ws3.State -ne 'Open') } catch { $jwtRefused = $true } }
        Need $jwtRefused 'a JWT in the URL was accepted'
        $true
    }

    # ------------------------------------------------------------ forget revokes the node
    Step 'forget (DELETE ?confirm=true) deletes the node and revokes its MQTT credentials at the broker' {
        $r1 = Api DELETE "/admin/nodes/$n1" $null $tok
        Need ($r1.Status -in 204, 409) "forget (no confirm) -> $($r1.Status)"
        $r = Api DELETE "/admin/nodes/${n1}?confirm=true" $null $tok
        Need ($r.Status -eq 204) "forget -> $($r.Status) $($r.Text)"
        Wait-Until { $x = Api GET '/devices' $null $tok; @($x.Json | Where-Object { $_.endpoint_id -eq $fanId }).Count -eq 0 } 20 'fan removed from /devices' | Out-Null
        $pw = ((Get-Content (Join-Path $credDir "$n1.cred") | Where-Object { $_ -like 'password=*' }) -replace '^password=', '')
        $probe = Join-Path $work 'probe.py'
        Put $probe @"
import ssl, sys, os, paho.mqtt.client as m
c = m.Client(m.CallbackAPIVersion.VERSION2, client_id=sys.argv[1])
c.username_pw_set(sys.argv[1], os.environ['P'])
c.tls_set(ca_certs=sys.argv[3], tls_version=ssl.PROTOCOL_TLS_CLIENT)
rc = []
c.on_connect = lambda cl, u, f, r, p: rc.append(str(r))
c.connect('localhost', int(sys.argv[2])); c.loop_start()
import time
for _ in range(50):
    if rc: break
    time.sleep(0.1)
print('RESULT', rc[0] if rc else 'none')
"@
        $env:P = $pw
        $o = & $simPy $probe $n1 $mqPort (Join-Path $mq 'certs\ca.crt') 2>&1
        Remove-Item Env:P
        Need (($o -join ' ') -match 'RESULT' -and ($o -join ' ') -notmatch 'RESULT Success') "old credentials still accepted: $($o -join ' ')"
        $true
    }
    Step 'second node (lock) is untouched by the forget' {
        $x = Api GET '/devices' $null $tok
        @($x.Json | Where-Object { $_.endpoint_id -eq $lockId -and $_.online }).Count -eq 1
    }

    # ------------------------------------------------------------ sim evidence
    Step 'sim stats: commands applied and acks sent by the simulated nodes' {
        Put $stopFile 'stop'
        if ($script:simProc) { $script:simProc.WaitForExit(15000) | Out-Null }
        $txt = Get-Content $simOut -Raw
        $m = [regex]::Match($txt, "$n1 (\{.*\})")
        Need $m.Success 'no stats line for the fan node'
        $s = $m.Groups[1].Value | ConvertFrom-Json
        Need ($s.cmds_applied -ge 2 -and $s.acks_sent -ge 2) "fan stats: $($m.Groups[1].Value)"
        $s2 = ([regex]::Match($txt, "$n2 (\{.*\})").Groups[1].Value | ConvertFrom-Json)
        Need ($s2.cmds_applied -ge 1 -and $s2.acks_sent -ge 1) 'lock node saw no command'
        Need ($s.violations -eq 0 -and $s2.violations -eq 0) 'sim reported contract violations'
        $true
    }
    Step 'no service secret, token or password appears in any container log' {
        $logs = (Dc logs --no-color 2>&1) -join "`n"
        foreach ($f in 'device-hub.secret', 'gateway.secret', 'agent.secret', 'agent-api.secret', 'hub-mqtt.password') {
            $v = [IO.File]::ReadAllText((Join-Path $sec $f))
            Need ($logs.IndexOf($v, [StringComparison]::Ordinal) -lt 0) "$f value found in logs"
        }
        Need ($logs.IndexOf($adminPw, [StringComparison]::Ordinal) -lt 0) 'admin password found in logs'
        $true
    }
}
finally {
    Write-Host '--- teardown (only what this run started)'
    if ($script:simProc -and -not $script:simProc.HasExited) { Put $stopFile 'stop'; if (-not $script:simProc.WaitForExit(8000)) { Stop-Process -Id $script:simProc.Id -Force } }
    foreach ($id in $script:bg) { $pr = Get-Process -Id $id -ErrorAction SilentlyContinue; if ($pr) { Stop-Process -Id $id -Force } }
    $ErrorActionPreference = 'Continue'
    if ($script:results | Where-Object { -not $_.Ok }) { & docker @compose logs --no-color --tail 40 2>&1 | ForEach-Object { Write-Host "  | $_" } }
    & docker @compose down -v --remove-orphans 2>&1 | Out-Null
    foreach ($v in 'EDGE_TAG', 'MOSQUITTO_CONTAINER', 'MOSQUITTO_PUBLISH', 'GATEWAY_PUBLISH', 'EDGE_SUBNET', 'EDGE_GATEWAY_IP', 'MOSQUITTO_STATE_DIR', 'SECRETS_DIR', 'CONFIG_DIR', 'DATA_DIR') { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
    if (-not $Keep) {
        # new-certs.ps1 restricts the key files to the current user; grant ourselves access so the delete cannot be refused
        icacls $work /grant "${env:USERNAME}:(OI)(CI)F" /T /C 2>&1 | Out-Null
        Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
    } else { Write-Host "kept $work" }
}
$bad = @($script:results | Where-Object { -not $_.Ok })
Write-Host ''
Write-Host ("{0} steps, {1} passed, {2} failed" -f $script:results.Count, ($script:results.Count - $bad.Count), $bad.Count)
if ($bad.Count) { exit 1 }
Write-Host 'e2e smoke passed' -ForegroundColor Green
