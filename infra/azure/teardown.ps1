<#
Delete EVERYTHING the benchmark created in Azure (D90, D92). Run it when a test is finished, and
whenever you are unsure what is still running.

    pwsh infra/azure/teardown.ps1            # shows what exists, then deletes it
    pwsh infra/azure/teardown.ps1 -WhatIf    # only shows what exists

It can only ever touch the ONE resource group `vitalmesh-bench`: the name is fixed here and is
never read from an argument, so a typo cannot delete something else. It deletes the whole group
(VMs, disks, public IP, network, Key Vault), waits, proves the group is gone, and then purges the
soft-deleted Key Vault (Azure keeps a deleted vault, and its name, for 7 days otherwise). The purge
runs even when the group is already gone, so a second run finishes the job.
#>
param([switch]$WhatIf)

$ErrorActionPreference = "Stop"
$group = "vitalmesh-bench"
$az = "C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"
if (-not (Test-Path $az)) { $az = "az" }

# filter in PowerShell: az.cmd goes through cmd.exe, which mangles a JMESPath query with brackets
function Get-DeletedVaults {
    @((& $az keyvault list-deleted -o json | ConvertFrom-Json) | Where-Object { $_.name -like "vmbench*" })
}

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

if ($WhatIf) { Write-Output "(WhatIf) soft-deleted vaults: $((Get-DeletedVaults).Count)"; exit 0 }
foreach ($v in (Get-DeletedVaults)) {
    Write-Output "Purging soft-deleted Key Vault $($v.name) ..."
    & $az keyvault purge --name $v.name | Out-Null
}
if ((Get-DeletedVaults).Count -gt 0) { Write-Error "A soft-deleted vault is still there; purge it by hand."; exit 1 }
Write-Output "Done: '$group' no longer exists and no vault is left. Nothing from the benchmark is running or billing."
