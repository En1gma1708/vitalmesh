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
    # Where the two local-only inputs are. Defaults are the repo's own (the PC); in Azure Cloud Shell the
    # repo is a fresh clone with neither file, so upload them and pass the paths (see docs/RUNBOOK.md).
    [string]$ModelsEnv = "",
    [string]$Corpus = "",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
# Runs on Windows PowerShell 5.1 (the PC) and on pwsh on Linux (Azure Cloud Shell, which is already
# signed in and has terraform and az).
$onWindows = ($PSVersionTable.PSEdition -ne "Core") -or $IsWindows
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path (Join-Path $here "..") "..")).Path
$tmp = [System.IO.Path]::GetTempPath()
$exe = if ($onWindows) { ".exe" } else { "" }
$az = "az"
$tf = $null
if ($onWindows) {
    $az = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
    if (-not (Test-Path $az)) { $az = "az" }
    $tf = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter terraform.exe -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
    $env:PATH = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin;" + $env:PATH
}
if (-not $tf) { $tf = (Get-Command terraform -ErrorAction SilentlyContinue).Source }
if (-not $tf) { throw "terraform not found" }
$key = Join-Path (Join-Path $HOME ".ssh") "vitalmesh_azure"
if (-not (Test-Path $key)) {
    # A fresh machine (Cloud Shell): make the deployment's own key pair; it is used for this VM only.
    New-Item -ItemType Directory -Force -Path (Join-Path $HOME ".ssh") | Out-Null
    & ssh-keygen -q -t ed25519 -N "" -f $key -C "vitalmesh-demo"
    if ($LASTEXITCODE -ne 0) { throw "ssh-keygen failed" }
}
$sshOpts = @("-i", $key, "-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=$(Join-Path $tmp 'vitalmesh_demo_known_hosts')", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30")

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
$vars = Join-Path $tmp "vitalmesh-demo.tfvars"
@"
ssh_allowed_cidr = "$myIp/32"
dns_label        = "$DnsLabel"
delete_by        = "$DeleteBy"
"@ | Set-Content -Path $vars -Encoding ascii

Push-Location $here
try {
    & $tf init -input=false | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "terraform init failed" }
    & $tf validate
    if ($LASTEXITCODE -ne 0) { throw "terraform validate failed" }
    # quoted: an unquoted -name=$var is not expanded when handed to a native program on Linux pwsh
    & $tf plan -input=false "-var-file=$vars" "-out=demo.tfplan"
    if ($LASTEXITCODE -ne 0) { throw "terraform plan failed (see above); nothing was created" }
    if (-not $Apply) {
        Write-Output "`nPlan only: nothing was created. Re-run with -Apply (needs Sahil's go) to create it."
        exit 0
    }
    & $tf apply -input=false demo.tfplan
    if ($LASTEXITCODE -ne 0) { throw "terraform apply failed; run teardown.ps1 to remove anything half-created" }
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
    & "ssh$exe" @sshOpts -o BatchMode=yes $target "true" 2>$null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 10
}
if (-not $ready) { throw "SSH never became reachable on $fqdn" }

# --- stage the upload: model keys (never printed), corpus, units, banner text
$stage = Join-Path $tmp "vitalmesh-upload"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path (Join-Path $stage "units") | Out-Null
if (-not $ModelsEnv) { $ModelsEnv = Join-Path $repo ".env" }
if (-not (Test-Path $ModelsEnv)) { throw "model keys file missing: $ModelsEnv (pass -ModelsEnv <file with GROQ_API_KEY= etc.>)" }
$wanted = @("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY")
$lines = foreach ($ln in Get-Content $ModelsEnv) {
    $k, $v = $ln -split "=", 2
    if ($wanted -contains $k.Trim() -and $v) { "$($k.Trim())=$($v.Trim().Trim('"').Trim("'"))" }
}
if (@($lines).Count -lt 1) { throw "no model-provider key found in $ModelsEnv" }
[System.IO.File]::WriteAllText((Join-Path $stage "models.env"), (($lines -join "`n") + "`n"))
[System.IO.File]::WriteAllText((Join-Path $stage "notice.txt"), ($Notice + "`n"))
if (-not $Corpus) { $Corpus = Join-Path (Join-Path (Join-Path (Join-Path $repo "evals") "results") "deploy") "corpus.sql.gz" }
if (-not (Test-Path $Corpus)) {
    throw "corpus dump missing: $Corpus (create it with: docker exec vitalmesh-postgres pg_dump -U vitalmesh -d vitalmesh --no-owner --no-privileges -t documents -t chunks | gzip > that path; in Cloud Shell pass -Corpus <uploaded file>)"
}
Copy-Item $Corpus (Join-Path $stage "corpus.sql.gz")
# Linux files must have Unix line endings, whatever git did on this machine
$remoteDir = Join-Path $here "remote"
foreach ($f in Get-ChildItem (Join-Path $remoteDir "units") -Filter *.service) {
    [System.IO.File]::WriteAllText((Join-Path (Join-Path $stage "units") $f.Name), ((Get-Content $f.FullName -Raw) -replace "`r`n", "`n"))
}
[System.IO.File]::WriteAllText((Join-Path $stage "bootstrap.sh"), ((Get-Content (Join-Path $remoteDir "bootstrap.sh") -Raw) -replace "`r`n", "`n"))

& "ssh$exe" @sshOpts $target "mkdir -p ~/upload"
& "scp$exe" @sshOpts -r (Join-Path $stage "*") "${target}:upload/"
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
$password | & "ssh$exe" @sshOpts $target "bash ~/upload/bootstrap.sh $GitRef $fqdn"
if ($LASTEXITCODE -ne 0) { throw "bootstrap failed on the VM (see the output above)" }

# --- check from here, the way the reviewer will reach it
$null_ = if ($onWindows) { "NUL" } else { "/dev/null" }
$code = (& "curl$exe" -s -o $null_ -w "%{http_code}" -u "reviewer:$password" "https://$fqdn/healthz")
$nopw = (& "curl$exe" -s -o $null_ -w "%{http_code}" "https://$fqdn/healthz")
Write-Output "`nFrom this machine: with the password $code (want 200), without it $nopw (want 401)."
Write-Output "`nShare with the reviewer (also saved in $credFile, which git ignores):"
Get-Content $credFile
