<#
Delete EVERYTHING the demo deployment created in Azure (D99).

    pwsh infra/azure-app/teardown.ps1            # shows what exists, then deletes it
    pwsh infra/azure-app/teardown.ps1 -WhatIf    # only shows what exists

It can only ever touch the ONE resource group `vitalmesh-demo`: the name is fixed here and never read
from an argument, so a typo cannot delete something else. It deletes the whole group (VM, disk,
public address, network), waits, and proves the group is gone. Run it when the reviewer is done, and
whenever you are unsure what is still running.
#>
param([switch]$WhatIf)

$ErrorActionPreference = "Stop"
$group = "vitalmesh-demo"
$az = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
if (-not (Test-Path $az)) { $az = "az" }

$exists = (& $az group exists --name $group) -eq "true"
if ($exists) {
    Write-Output "Resources in '$group':"
    & $az resource list --resource-group $group --query "[].{name:name, type:type}" -o table
    if ($WhatIf) { Write-Output "(WhatIf) nothing was deleted."; exit 0 }
    Write-Output "Deleting resource group '$group' and everything in it..."
    & $az group delete --name $group --yes
    $still = (& $az group exists --name $group) -eq "true"
    if ($still) { Write-Error "The group still exists. Check the portal; do not leave it running."; exit 1 }
} else {
    Write-Output "Resource group '$group' does not exist."
}

Remove-Item (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) ".reviewer-credentials.txt") -ErrorAction SilentlyContinue
Remove-Item "$env:TEMP\vitalmesh_demo_known_hosts" -ErrorAction SilentlyContinue
Write-Output "Done: '$group' no longer exists. Nothing from the demo is running or billing."
