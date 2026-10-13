<#
.SYNOPSIS
  Tests new-certs.ps1 SAN generation with openssl, in a temp directory (touches nothing under deploy/mosquitto/certs).
  Cases: inputs that collapse to one DNS name and one IP (the old "DNS:localhostIP:127.0.0.1" bug), and a normal set.
  Prints the SANs of each certificate and exits 1 on any mismatch.
#>
$ErrorActionPreference = 'Stop'
$ssl = (Get-Command openssl -ErrorAction Stop).Source
$script = Join-Path $PSScriptRoot 'new-certs.ps1'
$fail = 0
$cases = @(
    @{ Name = 'collapsed (one DNS, one IP)'; Args = @{ LanIp = '127.0.0.1'; TailnetName = 'localhost' };
       Want = @('DNS:localhost', 'IP:127.0.0.1') },
    @{ Name = 'normal'; Args = @{ LanIp = '192.168.1.50'; TailnetName = 'pi-brain.example.ts.net' };
       Want = @('DNS:localhost', 'DNS:pi-brain.example.ts.net', 'IP:127.0.0.1', 'IP:192.168.1.50') },
    @{ Name = 'extras and duplicates'; Args = @{ LanIp = '192.168.1.50'; TailnetName = 'pi-brain.example.ts.net'; ExtraDns = @('pi-brain.local', 'localhost'); ExtraIp = @('100.64.0.7', '192.168.1.50') };
       Want = @('DNS:localhost', 'DNS:pi-brain.example.ts.net', 'DNS:pi-brain.local', 'IP:127.0.0.1', 'IP:192.168.1.50', 'IP:100.64.0.7') }
)
foreach ($c in $cases) {
    $dir = Join-Path ([IO.Path]::GetTempPath()) ("newcerts-test-" + [guid]::NewGuid().ToString('N'))
    try {
        $p = $c.Args
        & $script @p -OutDir $dir | Out-Null     # new-certs.ps1 itself throws if its own SAN check fails
        $ErrorActionPreference = 'Continue'
        $txt = (& $ssl x509 -in (Join-Path $dir 'server.crt') -noout -ext subjectAltName 2>&1) | Out-String
        $ErrorActionPreference = 'Stop'
        Write-Host "[$($c.Name)]"; Write-Host $txt.TrimEnd()
        $got = @($txt -split "[`r`n,]" | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '^(DNS:|IP Address:)' } | ForEach-Object { $_ -replace '^IP Address:', 'IP:' })
        $bad = @($got | Where-Object { $_ -notmatch '^(DNS:[A-Za-z0-9.-]+|IP:[0-9.]+)$' })
        $diff = Compare-Object ($c.Want | Sort-Object) ($got | Sort-Object)
        if ($diff -or $bad.Count) { $fail++; Write-Host "FAIL $($c.Name): want [$($c.Want -join ' ')] got [$($got -join ' ')]" -ForegroundColor Red }
        else { Write-Host "ok   $($c.Name)" }
    } catch {
        $fail++; Write-Host "FAIL $($c.Name): $_" -ForegroundColor Red
    } finally {
        if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
    }
}
if ($fail) { Write-Host "$fail SAN test(s) failed" -ForegroundColor Red; exit 1 }
Write-Host 'all SAN tests passed'
