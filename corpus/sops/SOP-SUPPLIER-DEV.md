---
doc_id: SOP-SUPPLIER-DEV
title: Supplier Delivery Deviation and Material Shortage Response
doc_type: sop
version: 2.2
effective_date: 2025-10-20
owner: Supply Chain
applies_to_categories: []
applies_to_lines: []
---

## 1. Purpose
To define the response when an inbound material delivery will arrive later
than its contracted date.

## 2. Identification
2.1 Carrier ETA is compared against the contracted promise date daily. Any
positive variance is a deviation and is logged, however small.

2.2 A deviation is assessed against stock on hand and scheduled consumption,
not against stock on hand alone. A two-day slip on a material with four days
of cover is not an incident; the same slip with one day of cover is.

## 3. Impact assessment
3.1 Explode the affected material through the bill of materials to identify
every SKU that consumes it.

3.2 Walk the forward production schedule and compute a running balance. The
first scheduled run whose consumption takes the balance below zero is the run
that cannot be built. That run's start time, not the delivery ETA, is the
deadline that matters.

3.3 Identify the customer orders allocated to the affected runs, and their
customer tiers.

## 4. Mitigation, in order of preference
4.1 **Expedite the existing order.** Contact the supplier for a partial early
delivery. Suppliers can frequently split a shipment where they cannot
advance the whole of it.

4.2 **Alternate approved supplier.** Only suppliers already approved for the
material may be used. A new supplier requires qualification and is not a
same-week option.

4.3 **Transfer stock between plants.** Requires goods-in QA acceptance at the
receiving plant and full lot traceability under SOP-LOT-TRACE.

4.4 **Reschedule production to protect priority commitments.** Where the
shortfall cannot be closed, build the SKUs serving strategic-tier customers
first. Notify affected customers under POL-CUSTOMER-NOTIF.

4.5 Substitution of a different material specification is not permitted
without formulation and label review. This is never a same-day option.

## 5. Escalation
5.1 Deviations that will stop a line within 48 hours are escalated to the
plant manager and the category buyer the same day.

## 6. Records
Deviation log and supplier performance record. Retained 24 months, and
reviewed at the quarterly supplier business review.
