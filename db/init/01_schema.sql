-- =====================================================================
-- PROOF -- operational schema for a multi-plant bakery manufacturer.
--
-- This models the SHAPE of the systems a real bakery runs on:
--   ops.*  ~ MES / SCADA  (lines, runs, downtime, scrap, WIP)
--   scm.*  ~ ERP / WMS    (materials, lots, POs, customer orders)
--   qms.*  ~ quality system (holds, allergen rules)
--
-- Every assumption here is one I would validate with a plant in week one.
-- The schema is deliberately narrow: only what the three demo scenarios
-- need. Adding tables nobody queries is how these projects die.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS scm;
CREATE SCHEMA IF NOT EXISTS qms;

-- ---------------------------------------------------------------------
-- Simulation clock.
--
-- Demos must be reproducible. Rather than depend on wall-clock now(),
-- the whole system reads "now" from this one row. The seeder anchors all
-- generated history to it, so `make seed` on any machine on any day
-- produces byte-identical demo output.
-- ---------------------------------------------------------------------
CREATE TABLE ops.sim_clock (
    id      smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    now_ts  timestamptz NOT NULL
);

-- =====================================================================
-- MASTER DATA
-- =====================================================================

CREATE TABLE ops.plants (
    plant_id    serial PRIMARY KEY,
    plant_code  text NOT NULL UNIQUE,      -- 'TOR1'
    name        text NOT NULL,
    city        text NOT NULL,
    region      text NOT NULL              -- 'CA-ON', 'US-TX' -- drives plant scoping in Phase 4
);

CREATE TABLE ops.lines (
    line_id              serial PRIMARY KEY,
    plant_id             int NOT NULL REFERENCES ops.plants(plant_id),
    line_code            text NOT NULL,     -- 'L3'
    name                 text NOT NULL,     -- 'Flatbread Line 3'
    line_type            text NOT NULL,     -- flatbread | artisan | sweet_goods
    units_per_hour_nom   int NOT NULL,      -- nameplate rate
    UNIQUE (plant_id, line_code)
);

CREATE TABLE ops.skus (
    sku_id                serial PRIMARY KEY,
    sku_code              text NOT NULL UNIQUE,
    name                  text NOT NULL,
    category              text NOT NULL,           -- flatbread | artisan | sweet_goods
    allergens             text[] NOT NULL DEFAULT '{}',  -- wheat, milk, egg, soy, sesame
    shelf_life_days       int NOT NULL,
    units_per_case        int NOT NULL,
    -- THE SCRAP CLOCK.
    -- Proofed dough is alive. Once a batch is staged it must reach the oven
    -- inside this window or it over-proofs and becomes scrap. This single
    -- column is what makes a downtime event urgent rather than merely annoying,
    -- and it is the constraint the agent has to reason about in Demo 1.
    requires_proofing     boolean NOT NULL DEFAULT true,
    proof_window_minutes  int     NOT NULL DEFAULT 120
);

-- Which lines can run which SKUs, at what rate, and what it costs to switch.
-- Drives the "can we move this run to another line?" reasoning.
CREATE TABLE ops.line_sku_compat (
    line_id            int NOT NULL REFERENCES ops.lines(line_id),
    sku_id             int NOT NULL REFERENCES ops.skus(sku_id),
    units_per_hour     int NOT NULL,        -- line-specific, <= nameplate
    changeover_minutes int NOT NULL,        -- sanitation + setup to switch TO this sku
    PRIMARY KEY (line_id, sku_id)
);

CREATE TABLE scm.customers (
    customer_id   serial PRIMARY KEY,
    customer_code text NOT NULL UNIQUE,
    name          text NOT NULL,
    channel       text NOT NULL,            -- qsr | grocery | foodservice
    -- Service-level tier. A QSR partner on a daily replenishment contract
    -- is not the same as a grocery DC with a weekly window; the agent must
    -- prioritise accordingly when it cannot satisfy everyone.
    priority_tier text NOT NULL CHECK (priority_tier IN ('strategic','core','standard'))
);

CREATE TABLE scm.suppliers (
    supplier_id     serial PRIMARY KEY,
    supplier_code   text NOT NULL UNIQUE,
    name            text NOT NULL,
    lead_time_days  int  NOT NULL
);

CREATE TABLE scm.materials (
    material_id   serial PRIMARY KEY,
    material_code text NOT NULL UNIQUE,
    name          text NOT NULL,            -- 'Hard Red Spring Flour'
    uom           text NOT NULL,            -- kg | L | ea
    allergens     text[] NOT NULL DEFAULT '{}'
);

-- Bill of materials: how much of each material one unit of a SKU consumes.
CREATE TABLE scm.sku_bom (
    sku_id       int NOT NULL REFERENCES ops.skus(sku_id),
    material_id  int NOT NULL REFERENCES scm.materials(material_id),
    qty_per_unit numeric(12,5) NOT NULL,
    PRIMARY KEY (sku_id, material_id)
);
