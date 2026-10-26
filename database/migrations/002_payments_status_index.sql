-- Migration 2: speeds up the unassigned-coin report.
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status, created_at);
