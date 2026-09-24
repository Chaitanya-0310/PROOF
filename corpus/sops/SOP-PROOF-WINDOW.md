---
doc_id: SOP-PROOF-WINDOW
title: Proof Window Control and Over-Proof Disposition
doc_type: sop
version: 4.1
effective_date: 2025-09-15
owner: Quality Assurance
applies_to_categories: [flatbread, artisan, sweet_goods]
applies_to_lines: []
---

## 1. Purpose
To define the maximum permitted time between final proof staging and oven
entry, and the disposition of product that exceeds it.

## 2. Scope
All proofed dough batches on all lines at all sites. This procedure governs
the proof window value recorded against each SKU in the production system.

## 3. Definitions
3.1 **Proof window** -- the elapsed time, in minutes, from the moment a batch
is staged at the end of final proof until it must enter the oven.

3.2 **Over-proofed** -- a batch whose staged time has exceeded its proof
window. Over-proofed dough exhibits loss of gas retention, collapse on
handling, and irregular cell structure after bake.

3.3 **Staged time** -- recorded automatically at the proofer exit sensor. It
is not adjusted manually under any circumstances.

## 4. Proof window values
4.1 Proof windows are SKU-specific and are maintained in the production
system. Nominal values by category:

| Category    | Nominal window |
|-------------|----------------|
| Sweet goods | 70 minutes     |
| Flatbread   | 95 minutes     |
| Artisan     | 150 minutes    |

4.2 Sweet goods carry the shortest window because of higher yeast activity
and enriched dough. A stoppage on a sweet goods line therefore destroys
staged product faster than the same stoppage on an artisan line. Supervisors
must treat sweet goods stoppages as time-critical from the first minute.

4.3 The window is not extendable by retarding, chilling, or any other
intervention once a batch has been staged.

## 5. Disposition of over-proofed product
5.1 Batches that exceed their proof window are scrapped. They are not baked,
not reworked, and not downgraded to secondary channels.

5.2 Scrap is recorded against reason code OVERPROOF_EXPIRY with source
wip_expiry. Recording it as generic line loss obscures the cause and is a
non-conformance in its own right.

5.3 The shift supervisor records the batch code, unit count, and the
originating downtime event, if any, in the scrap log before end of shift.

## 6. Action during an unplanned stoppage
6.1 Immediately on a line stopping, the supervisor identifies every staged
batch on that line and its remaining time to expiry.

6.2 Batches whose remaining time is less than the estimated time to restore
are to be treated as lost. Do not wait for them to expire before planning the
recovery; the units are already committed to scrap.

6.3 Where an alternate compatible line is available, batches with remaining
time greater than the transfer and changeover duration may be moved. Transfer
requires QA sign-off under SOP-LINE-REALLOC and an allergen check under
SOP-ALLERGEN-CO.

6.4 The loss from expired staged dough is reported SEPARATELY from lost
throughput. The two are additive. Reporting only lost throughput understates
the true cost of a stoppage and has previously led to under-resourced
maintenance response.

## 7. Records
Scrap log entries are retained for 24 months. Proof window values and their
change history are retained for the life of the SKU plus 36 months.
