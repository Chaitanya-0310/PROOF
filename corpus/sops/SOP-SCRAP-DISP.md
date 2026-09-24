---
doc_id: SOP-SCRAP-DISP
title: Scrap Classification, Reason Coding and Disposition
doc_type: sop
version: 4.3
effective_date: 2025-06-15
owner: Operations
applies_to_categories: [flatbread, artisan, sweet_goods]
applies_to_lines: []
---

## 1. Purpose
To ensure scrap is classified against the correct reason code so that
recurring losses can be identified and addressed.

## 2. Reason codes
2.1 **STARTUP_LOSS** -- product diverted at the start of a run or after a
restart, per the applicable restart SOP. Expected on every run.

2.2 **CHANGEOVER_PURGE** -- product diverted after a changeover under
SOP-ALLERGEN-CO section 5. Expected on every changeover.

2.3 **OVERPROOF_EXPIRY** -- staged dough that exceeded its proof window.
Source is recorded as wip_expiry, not line.

2.4 **MISSHAPE** -- forming or handling defects.

2.5 **UNDERBAKE** -- product failing bake colour or internal temperature.

2.6 **FOREIGN_MATERIAL** -- product rejected for physical contamination.

2.7 **QUALITY_REJECT** -- product rejected on any other specification
failure.

## 3. Classification discipline
3.1 The reason code records the CAUSE, not the point of detection. Dough that
over-proofed during a stoppage and was discarded at the oven is
OVERPROOF_EXPIRY, not MISSHAPE.

3.2 Coding an expiry loss as generic line loss obscures the relationship
between downtime and scrap, and has previously delayed identification of
recurring maintenance problems.

## 4. Interpreting scrap rates
4.1 Scrap rate is scrap units divided by planned units for the same set of
runs. When comparing periods, aggregate scrap to one figure per run before
dividing. Summing scrap events against a repeated run total inflates the
denominator in proportion to the number of scrap events and will make a
worsening trend appear to improve.

4.2 A rise in STARTUP_LOSS or CHANGEOVER_PURGE share usually indicates a
change in run pattern -- shorter runs or more frequent changeovers -- rather
than a decline in line performance. Investigate the schedule before
investigating the line.

4.3 Expected total scrap in normal operation is 1.2 to 2.0 percent of planned
units. Sweet goods sit at the upper end of that range.

## 5. Disposition
5.1 Scrap is routed to animal feed where it meets the feed specification, and
to waste otherwise. Product rejected for foreign material or allergen
cross-contact never goes to feed.

## 6. Records
Scrap log retained 24 months.
