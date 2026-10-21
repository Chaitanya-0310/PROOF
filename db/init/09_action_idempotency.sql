-- =====================================================================
-- IDEMPOTENT PROPOSALS.
--
-- A proposal is the one write the agent makes, and it can be repeated
-- without anyone intending it: the coordinator re-asks the actions agent in
-- a later planning round, a tool call is retried after a timeout, or the
-- same question is asked twice by a supervisor who did not see the first
-- answer. Each of those used to queue another pending row, and an approval
-- screen showing the same reallocation three times invites approving it
-- three times.
--
-- So every proposal carries a key derived from WHAT it asks for (who, which
-- action, which plant, which run/order/PO/scope), not how it is worded. While
-- a matching proposal is pending and unexpired, proposing it again returns
-- the existing action instead of creating one.
--
-- Not a UNIQUE index, deliberately. "Pending" is not enough: a pending row
-- whose expires_at has passed is dead (expiry is only written when someone
-- tries to decide it), and re-proposing it must work. A partial index cannot
-- say "and not expired" because that depends on the clock. The writer takes
-- a transaction-scoped advisory lock on the key instead -- see
-- proof/tools/actions.py -- which serialises concurrent identical proposals
-- and leaves the expiry rule in one readable place.
--
-- Idempotent: `make migrate` replays it on an existing database.
-- =====================================================================

ALTER TABLE act.proposed_actions
    ADD COLUMN IF NOT EXISTS idempotency_key text;

CREATE INDEX IF NOT EXISTS proposed_actions_idem_pending_idx
    ON act.proposed_actions (idempotency_key)
    WHERE status = 'pending';
