-- =====================================================================
-- ACTION QUEUE -- the write path.
--
-- Everything before this phase was read-only. The value of an operations
-- copilot, though, is in ACTING: reallocating a run, drafting the customer
-- notification, raising the expedite request. That is also where it becomes
-- genuinely dangerous.
--
-- The design rule: the agent may PROPOSE, and only a named human may
-- APPROVE. Execution is a third step, performed by a process the agent does
-- not control. Those three are separated by database role as well as by
-- code, so "the agent executed something nobody approved" is not a bug that
-- can be introduced by a prompt change.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS act;

-- ---------------------------------------------------------------------
-- Who may do what.
--
-- Roles are coarse on purpose. A plant supervisor can propose anything at
-- their own plant and approve nothing; a plant manager approves within their
-- plant; only a regional director approves across plants. That mirrors
-- SOP-LINE-REALLOC section 3, which is the actual governing document -- the
-- authorization model is a transcription of a procedure that already exists,
-- not an invention of this project.
-- ---------------------------------------------------------------------
CREATE TABLE act.principals (
    principal_id  text PRIMARY KEY,          -- 'j.okafor'
    display_name  text NOT NULL,
    role          text NOT NULL CHECK (role IN
                      ('line_operator', 'shift_supervisor',
                       'plant_manager', 'regional_director')),
    -- Empty array means every plant. Anything else is an explicit allow-list.
    plant_scope   text[] NOT NULL DEFAULT '{}',
    active        boolean NOT NULL DEFAULT true
);

-- ---------------------------------------------------------------------
-- Proposed actions.
--
-- `payload` is deliberately structured, not free text. An approval screen
-- showing "reallocate run 923" is not enough for a plant manager to make a
-- decision; they need the units, the line, the changeover cost and what gets
-- displaced. Free text would also make the executor parse prose, which is
-- exactly where an injection would land.
-- ---------------------------------------------------------------------
CREATE TABLE act.proposed_actions (
    action_id     serial PRIMARY KEY,
    action_type   text NOT NULL CHECK (action_type IN (
                      'reallocate_run',
                      'notify_customer',
                      'expedite_purchase_order',
                      'hold_product')),
    plant_code    text NOT NULL,
    -- Who the system was acting for when the proposal was made. Taken from
    -- the session principal, never from anything the model produced.
    proposed_by   text NOT NULL REFERENCES act.principals(principal_id),
    proposed_at   timestamptz NOT NULL,
    -- Why. Shown verbatim on the approval screen.
    rationale     text NOT NULL,
    payload       jsonb NOT NULL,
    -- Cheap provenance: the question that led here, so an approver can see
    -- what was actually asked.
    source_question text,

    status        text NOT NULL DEFAULT 'pending' CHECK (status IN
                      ('pending', 'approved', 'rejected', 'executed', 'expired')),

    decided_by    text REFERENCES act.principals(principal_id),
    decided_at    timestamptz,
    decision_note text,
    executed_at   timestamptz,

    -- An approval that sat unactioned through the window it was about is not
    -- an approval any more. Perishable decisions expire.
    expires_at    timestamptz NOT NULL,

    -- A decision must name a decider, and vice versa. Enforced here rather
    -- than in application code so no future writer can skip it.
    CONSTRAINT decision_is_complete CHECK (
        (status IN ('pending', 'expired'))
        OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)
    ),
    -- Nothing reaches 'executed' without having passed through 'approved'.
    CONSTRAINT executed_implies_decided CHECK (
        status <> 'executed' OR (decided_by IS NOT NULL AND executed_at IS NOT NULL)
    )
);
CREATE INDEX ON act.proposed_actions (status, plant_code, proposed_at DESC);

-- ---------------------------------------------------------------------
-- Append-only decision log.
--
-- proposed_actions carries current state; this carries history. Separate
-- because "who approved what, when, and on what basis" is the record an
-- auditor asks for, and it must survive any later update to the row.
-- ---------------------------------------------------------------------
CREATE TABLE act.decision_log (
    log_id       serial PRIMARY KEY,
    action_id    int NOT NULL REFERENCES act.proposed_actions(action_id),
    at           timestamptz NOT NULL,
    principal_id text NOT NULL REFERENCES act.principals(principal_id),
    event        text NOT NULL CHECK (event IN
                     ('proposed', 'approved', 'rejected', 'executed', 'expired')),
    detail       text
);
CREATE INDEX ON act.decision_log (action_id, at);

-- ---------------------------------------------------------------------
-- Denied authorization attempts.
--
-- Recorded rather than silently refused. A pattern of an agent repeatedly
-- attempting cross-plant actions is a signal worth having -- either the
-- scoping is wrong for how people actually work, or something is trying it
-- on. Phase 5 traces join to this.
-- ---------------------------------------------------------------------
CREATE TABLE act.authz_denials (
    denial_id    serial PRIMARY KEY,
    at           timestamptz NOT NULL,
    principal_id text NOT NULL,
    role         text NOT NULL,
    attempted    text NOT NULL,            -- 'propose:reallocate_run'
    plant_code   text,
    reason       text NOT NULL
);
CREATE INDEX ON act.authz_denials (at DESC);
