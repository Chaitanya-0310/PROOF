-- =====================================================================
-- OPERATIONAL TABLES -- the moving parts the agent reasons over.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Production runs: a scheduled block of one SKU on one line.
-- ---------------------------------------------------------------------
CREATE TABLE ops.production_runs (
    run_id         serial PRIMARY KEY,
    line_id        int NOT NULL REFERENCES ops.lines(line_id),
    sku_id         int NOT NULL REFERENCES ops.skus(sku_id),
    planned_start  timestamptz NOT NULL,
    planned_end    timestamptz NOT NULL,
    actual_start   timestamptz,
    actual_end     timestamptz,
    planned_units  int NOT NULL,
    actual_units   int NOT NULL DEFAULT 0,
    status         text NOT NULL CHECK (status IN
                     ('scheduled','running','complete','aborted')),
    CHECK (planned_end > planned_start)
);
CREATE INDEX ON ops.production_runs (line_id, planned_start);
CREATE INDEX ON ops.production_runs (status);

-- ---------------------------------------------------------------------
-- Downtime. ended_at IS NULL means the line is down RIGHT NOW -- that
-- open-event convention is what makes "what is at risk?" answerable.
-- ---------------------------------------------------------------------
CREATE TABLE ops.downtime_events (
    event_id      serial PRIMARY KEY,
    line_id       int NOT NULL REFERENCES ops.lines(line_id),
    run_id        int REFERENCES ops.production_runs(run_id),
    reason_code   text NOT NULL CHECK (reason_code IN (
                      'MECHANICAL','ELECTRICAL','CHANGEOVER','SANITATION',
                      'MATERIAL_OUT','STAFFING','QUALITY_HOLD','PLANNED_MAINT')),
    -- Free-text operator note. Messy on purpose: this is the column the
    -- Phase 7 finetune learns to classify into reason_code.
    reason_detail text,
    started_at    timestamptz NOT NULL,
    ended_at      timestamptz,
    -- Generated, so "duration" can never drift out of sync with the
    -- timestamps. NULL while the event is still open.
    duration_minutes int GENERATED ALWAYS AS (
        CASE WHEN ended_at IS NULL THEN NULL
             ELSE (EXTRACT(EPOCH FROM (ended_at - started_at)) / 60)::int END
    ) STORED,
    -- Operator estimate of time-to-restore, captured when the event opens.
    -- The agent needs a forward-looking number, not just elapsed time.
    eta_minutes   int
);
CREATE INDEX ON ops.downtime_events (line_id, started_at DESC);
-- Partial index: "which lines are down now" is the hottest query here.
CREATE INDEX ON ops.downtime_events (line_id) WHERE ended_at IS NULL;

-- ---------------------------------------------------------------------
-- WIP batches -- proofed dough waiting for the oven.
--
-- This is the most important table in the project. Each staged batch
-- carries an expiry timestamp; when a line stops, these keep ticking.
-- ---------------------------------------------------------------------
CREATE TABLE ops.wip_batches (
    batch_id         serial PRIMARY KEY,
    batch_code       text NOT NULL UNIQUE,
    run_id           int NOT NULL REFERENCES ops.production_runs(run_id),
    line_id          int NOT NULL REFERENCES ops.lines(line_id),
    sku_id           int NOT NULL REFERENCES ops.skus(sku_id),
    units            int NOT NULL,
    staged_at        timestamptz NOT NULL,
    proof_expires_at timestamptz NOT NULL,
    status           text NOT NULL CHECK (status IN ('staged','consumed','scrapped'))
);
CREATE INDEX ON ops.wip_batches (line_id, status, proof_expires_at);

-- ---------------------------------------------------------------------
-- Scrap. source distinguishes normal line loss from an expiry event,
-- which is what the Demo 3 root-cause analysis slices on.
-- ---------------------------------------------------------------------
CREATE TABLE ops.scrap_events (
    scrap_id    serial PRIMARY KEY,
    run_id      int REFERENCES ops.production_runs(run_id),
    line_id     int NOT NULL REFERENCES ops.lines(line_id),
    sku_id      int NOT NULL REFERENCES ops.skus(sku_id),
    units       int NOT NULL,
    reason_code text NOT NULL CHECK (reason_code IN (
                    'STARTUP_LOSS','OVERPROOF_EXPIRY','MISSHAPE','UNDERBAKE',
                    'FOREIGN_MATERIAL','CHANGEOVER_PURGE','QUALITY_REJECT')),
    occurred_at timestamptz NOT NULL,
    source      text NOT NULL CHECK (source IN ('line','wip_expiry','qa'))
);
CREATE INDEX ON ops.scrap_events (line_id, occurred_at);
CREATE INDEX ON ops.scrap_events (sku_id, occurred_at);

-- =====================================================================
-- SUPPLY CHAIN
-- =====================================================================

CREATE TABLE scm.inventory_lots (
    lot_id      serial PRIMARY KEY,
    lot_code    text NOT NULL UNIQUE,
    plant_id    int NOT NULL REFERENCES ops.plants(plant_id),
    material_id int NOT NULL REFERENCES scm.materials(material_id),
    qty_on_hand numeric(14,3) NOT NULL,
    uom         text NOT NULL,
    received_at timestamptz NOT NULL,
    expires_at  timestamptz,
    status      text NOT NULL CHECK (status IN ('available','held','consumed'))
);
CREATE INDEX ON scm.inventory_lots (plant_id, material_id, status);

CREATE TABLE scm.purchase_orders (
    po_id       serial PRIMARY KEY,
    po_code     text NOT NULL UNIQUE,
    supplier_id int NOT NULL REFERENCES scm.suppliers(supplier_id),
    plant_id    int NOT NULL REFERENCES ops.plants(plant_id),
    material_id int NOT NULL REFERENCES scm.materials(material_id),
    qty         numeric(14,3) NOT NULL,
    uom         text NOT NULL,
    ordered_at  timestamptz NOT NULL,
    promised_at timestamptz NOT NULL,   -- what the contract says
    eta_at      timestamptz NOT NULL,   -- what the carrier says today (Demo 2 slips this)
    status      text NOT NULL CHECK (status IN ('open','in_transit','received','cancelled'))
);
CREATE INDEX ON scm.purchase_orders (plant_id, material_id, status);

CREATE TABLE scm.customer_orders (
    order_id         serial PRIMARY KEY,
    order_code       text NOT NULL UNIQUE,
    customer_id      int NOT NULL REFERENCES scm.customers(customer_id),
    plant_id         int NOT NULL REFERENCES ops.plants(plant_id),
    promised_ship_at timestamptz NOT NULL,
    status           text NOT NULL CHECK (status IN ('open','shipped','short','cancelled'))
);
CREATE INDEX ON scm.customer_orders (plant_id, promised_ship_at);

CREATE TABLE scm.customer_order_lines (
    order_line_id serial PRIMARY KEY,
    order_id      int NOT NULL REFERENCES scm.customer_orders(order_id),
    sku_id        int NOT NULL REFERENCES ops.skus(sku_id),
    qty_units     int NOT NULL,
    qty_fulfilled int NOT NULL DEFAULT 0
);
CREATE INDEX ON scm.customer_order_lines (sku_id);

-- The join that makes Demo 1 possible: it is what turns "line 3 stopped"
-- into "QSR-A order 88213 is 4,200 units short". Without an explicit
-- allocation table the agent would have to guess which order a run serves.
CREATE TABLE scm.run_order_allocations (
    run_id          int NOT NULL REFERENCES ops.production_runs(run_id),
    order_line_id   int NOT NULL REFERENCES scm.customer_order_lines(order_line_id),
    allocated_units int NOT NULL,
    PRIMARY KEY (run_id, order_line_id)
);

-- =====================================================================
-- QUALITY
-- =====================================================================

CREATE TABLE qms.quality_holds (
    hold_id     serial PRIMARY KEY,
    plant_id    int NOT NULL REFERENCES ops.plants(plant_id),
    sku_id      int REFERENCES ops.skus(sku_id),
    lot_id      int REFERENCES scm.inventory_lots(lot_id),
    reason      text NOT NULL,
    status      text NOT NULL CHECK (status IN ('open','released','rejected')),
    opened_at   timestamptz NOT NULL,
    released_at timestamptz
);
CREATE INDEX ON qms.quality_holds (plant_id, status);
