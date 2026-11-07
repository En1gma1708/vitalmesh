# Cost report

Honest accounting of what the cloud work cost and what the full system would cost. Figures are
labelled **measured** (read from a log), **arithmetic** (computed from list prices) or
**assumption**. The student subscription's own cost analysis is the authority for the real bill;
it lags by a day or two, so the figures below are estimates until it is read.

## What was spent

| item | amount | tag |
|---|---|---|
| personal money | 0 USD | measured (no card on the student subscription; a spending limit applies) |
| first deploy attempt (region refused by the subscription policy) | 0 USD: only an empty resource group existed, for about one minute | measured |
| the Azure test session | about 0.3 to 0.4 USD | arithmetic |

The test session ran three 2-vCPU VMs in one region for about 51 minutes (from the end of the
deployment to the start of the teardown), plus one public address, three small disks and a Key
Vault with a handful of operations. At list prices of roughly 0.10 USD per VM-hour that is about
0.26 USD for the VMs, and a few cents for everything else. The student credit is 100 USD and the
agreed ceiling for this project was about 15 USD, so less than 3% of the ceiling was used.

Everything was deleted at the end by `infra/azure/teardown.ps1`, which deletes the one resource
group, checks that it is gone and purges the soft-deleted Key Vault (a deleted vault keeps its name
for 7 days otherwise). A second run on an empty subscription changes nothing. Azure itself creates a
free `NetworkWatcherRG` group when a virtual network is made; it is not part of this project and
costs nothing.

## What the full system would cost (arithmetic, not a quote)

From `scripts/capacity_model.py` with the stated assumptions for one million people:

| item | estimate | tag |
|---|---|---|
| about six 2-vCPU VMs (processors, API, Kafka, Redis, Postgres) | about 420 USD a month at list price | arithmetic |
| model calls | the dominant cost and the first bottleneck: about 208,000 tokens a minute at peak, which the free tier cannot serve (about 27 keys' worth) | arithmetic from assumptions |
| managed services (a managed Kafka, a managed Redis, a managed Postgres) | not estimated; these usually cost more than self-run VMs | not computed |
| egress, storage, monitoring | not estimated | not computed |

So the sentence to use is: the compute for a million people is a few hundred dollars a month at list
price, and the model bill, which depends on how many questions people ask and which model is used,
would be larger than all the compute combined. The system is designed to keep the model off the
common path (tiers, caches, load shedding) for exactly this reason.

## Controls that kept the spend at zero

* A fixed budget box agreed in advance: one resource group, at most three 2-vCPU VMs, one region,
  about 15 USD, everything deleted after each test, nothing left overnight.
* Terraform validates the VM count and sizes before it creates anything.
* A cost log written during the session, and a teardown script written before the first deploy.
* SSH and the HTTPS check were open only to one operator address; the VMs had no other public
  exposure.
