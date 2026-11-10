<#
Deploy the whole VitalMesh demo on ONE Azure VM for one reviewer (D99).

    pwsh infra/azure-app/deploy.ps1 -DeleteBy 2026-11-14                # plan only, creates nothing
    pwsh infra/azure-app/deploy.ps1 -DeleteBy 2026-11-14 -Apply         # create, install, start

Needs Sahil's explicit go for every -Apply (the cloud account is his), and a signed-in Azure CLI
(`az login --use-device-code`, done by him: this script never sees a credential). It:
  1. plans (and with -Apply creates) one resource group `vitalmesh-demo` in indiasouthcentral: one
     2-vCPU VM, a static public address with a free DNS name, a firewall that opens only 80/443 to the
     internet and SSH to the operator's address;
  2. copies the three model-provider keys from the local .env (never printed), the guideline corpus
     and the service definitions to the VM over SSH;
  3. runs infra/azure-app/remote/bootstrap.sh there (code from the public repo at -GitRef, containers,
     services, HTTPS certificate, shared password), and checks the site from this machine.

Delete everything afterwards with teardown.ps1.
#>
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^\d{4}-\d{2}-\d{2}$')][string]$DeleteBy,
    [string]$DnsLabel = "vitalmesh-demo",
    [string]$GitRef = "",
    [string]$Notice = "Demo for one reviewer. Please use made-up health data only; do not enter real personal details. Not medical advice. This copy will be deleted soon.",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Resolve-Path (Join-Path $here "..\..")
$az = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
if (-not (Test-Path $az)) { $az = "az" }
$tf = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter terraform.exe -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
if (-not $tf) { $tf = (Get-Command terraform -ErrorAction SilentlyContinue).Source }
if (-not $tf) { throw "terraform.exe not found" }
$env:PATH = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin;" + $env:PATH
$key = Join-Path $HOME ".ssh\vitalmesh_azure"
$sshOpts = @("-i", $key, "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=$env:TEMP\vitalmesh_demo_known_hosts", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30")

# --- who is signed in (the id is used for this process only and never written down)
$sub = (& $az account show --query id -o tsv 2>$null)
if (-not $sub) { throw "Not signed in. Run: az login --use-device-code" }
$env:ARM_SUBSCRIPTION_ID = $sub

if (-not $GitRef) {
    & git -C $repo fetch --quiet origin main
    $GitRef = (& git -C $repo rev-parse origin/main).Trim()
}
Write-Output "Code that will be deployed: $GitRef (from the public repo)"

# --- the one address allowed to open SSH
$myIp = (Invoke-RestMethod -Uri "https://api.ipify.org").Trim()
$vars = Join-Path $env:TEMP "vitalmesh-demo.tfvars"
@"
ssh_allowed_cidr = "$myIp/32"
dns_label        = "$DnsLabel"
delete_by        = "$DeleteBy"
"@ | Set-Content -Path $vars -Encoding ascii

Push-Location $here
try {
    & $tf init -input=false | Out-Null
    & $tf validate
    & $tf plan -input=false -var-file=$vars -out=demo.tfplan
    if (-not $Apply) {
        Write-Output "`nPlan only: nothing was created. Re-run with -Apply (needs Sahil's go) to create it."
        exit 0
    }
    & $tf apply -input=false demo.tfplan
    $fqdn = (& $tf output -raw fqdn).Trim()
}
finally {
    Pop-Location
    Remove-Item $vars -ErrorAction SilentlyContinue
}
Write-Output "`nThe VM is created: https://$fqdn (not up yet: installing)"

# --- wait for SSH
$target = "vmadmin@$fqdn"
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    & ssh.exe @sshOpts -o BatchMode=yes $target "true" 2>$null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 10
}
if (-not $ready) { throw "SSH never became reachable on $fqdn" }

# --- stage the upload: model keys (never printed), corpus, units, banner text
$stage = Join-Path $env:TEMP "vitalmesh-upload"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path "$stage\units" | Out-Null
$wanted = @("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY")
$lines = foreach ($ln in Get-Content (Join-Path $repo ".env")) {
    $k, $v = $ln -split "=", 2
    if ($wanted -contains $k.Trim() -and $v) { "$($k.Trim())=$($v.Trim().Trim('"').Trim("'"))" }
}
if (@($lines).Count -lt 1) { throw "no model-provider key found in .env" }
[System.IO.File]::WriteAllText("$stage\models.env", (($lines -join "`n") + "`n"))
[System.IO.File]::WriteAllText("$stage\notice.txt", ($Notice + "`n"))
$corpus = Join-Path $repo "evals\results\deploy\corpus.sql.gz"
if (-not (Test-Path $corpus)) {
    throw "corpus dump missing: $corpus (create it with: docker exec vitalmesh-postgres pg_dump -U vitalmesh -d vitalmesh --no-owner --no-privileges -t documents -t chunks | gzip > that path)"
}
Copy-Item $corpus "$stage\corpus.sql.gz"
# Linux files must have Unix line endings, whatever git did on this machine
foreach ($f in Get-ChildItem (Join-Path $here "remote\units") -Filter *.service) {
    [System.IO.File]::WriteAllText("$stage\units\$($f.Name)", ((Get-Content $f.FullName -Raw) -replace "`r`n", "`n"))
}
[System.IO.File]::WriteAllText("$stage\bootstrap.sh", ((Get-Content (Join-Path $here "remote\bootstrap.sh") -Raw) -replace "`r`n", "`n"))

& ssh.exe @sshOpts $target "mkdir -p ~/upload"
& scp.exe @sshOpts -r "$stage\*" "${target}:upload/"
if ($LASTEXITCODE -ne 0) { throw "upload failed" }
Remove-Item $stage -Recurse -Force

# --- the shared password for the reviewer: generated here, hashed on the VM, shown once
$bytes = New-Object byte[] 18
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$password = [Convert]::ToBase64String($bytes).Replace("+", "A").Replace("/", "b").Replace("=", "")
$credFile = Join-Path $here ".reviewer-credentials.txt"
@"
site:     https://$fqdn
username: reviewer
password: $password
delete by $DeleteBy (run: pwsh infra/azure-app/teardown.ps1)
"@ | Set-Content -Path $credFile -Encoding ascii

Write-Output "Installing on the VM (several minutes: packages, containers, model download)..."
$password | & ssh.exe @sshOpts $target "bash ~/upload/bootstrap.sh $GitRef $fqdn"
if ($LASTEXITCODE -ne 0) { throw "bootstrap failed on the VM (see the output above)" }

# --- check from here, the way the reviewer will reach it
$code = (& curl.exe -s -o NUL -w "%{http_code}" -u "reviewer:$password" "https://$fqdn/healthz")
$nopw = (& curl.exe -s -o NUL -w "%{http_code}" "https://$fqdn/healthz")
Write-Output "`nFrom this machine: with the password $code (want 200), without it $nopw (want 401)."
Write-Output "`nShare with the reviewer (also saved in $credFile, which git ignores):"
Get-Content $credFile
