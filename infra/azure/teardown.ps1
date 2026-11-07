<#
Delete EVERYTHING the benchmark created in Azure (D90). Run it when a test is finished, and
whenever you are unsure what is still running.

    pwsh infra/azure/teardown.ps1            # shows what exists, then deletes it
    pwsh infra/azure/teardown.ps1 -WhatIf    # only shows what exists

It can only ever touch the ONE resource group `vitalmesh-bench`: the name is fixed here and is
never read from an argument, so a typo cannot delete something else. It deletes the whole group
(VMs, disks, public IP, network), waits, and then proves the group is gone.
#>
param([switch]$WhatIf)

$ErrorActionPreference = "Stop"
$group = "vitalmesh-bench"
$az = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
if (-not (Test-Path $az)) { $az = "az" }

$exists = (& $az group exists --name $group) -eq "true"
if (-not $exists) {
    Write-Output "Nothing to delete: resource group '$group' does not exist."
    exit 0
}

Write-Output "Resources in '$group':"
& $az resource list --resource-group $group --query "[].{name:name, type:type}" -o table
if ($WhatIf) { Write-Output "(WhatIf) nothing was deleted."; exit 0 }

Write-Output "Deleting resource group '$group' and everything in it..."
& $az group delete --name $group --yes
$still = (& $az group exists --name $group) -eq "true"
if ($still) { Write-Error "The group still exists. Check the portal; do not leave it running."; exit 1 }
Write-Output "Done: '$group' no longer exists. Nothing from the benchmark is running or billing."
