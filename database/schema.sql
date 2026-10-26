-- JP-PisoWiFi schema, version 1. All statements are idempotent.
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, name TEXT, applied_at INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS admins (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'owner' CHECK (role IN ('owner','staff')),
  must_change_password INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  last_login_at INTEGER);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS rates (
  id INTEGER PRIMARY KEY,
  amount INTEGER NOT NULL CHECK (amount > 0),
  minutes INTEGER NOT NULL CHECK (minutes > 0),
  enabled INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS clients (
  id INTEGER PRIMARY KEY,
  mac TEXT NOT NULL UNIQUE,
  ip TEXT,
  hostname TEXT,
  blocked INTEGER NOT NULL DEFAULT 0,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY,
  client_id INTEGER NOT NULL REFERENCES clients(id),
  started_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  purchased_seconds INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','ended')),
  ended_at INTEGER,
  down_kbps INTEGER NOT NULL DEFAULT 0,
  up_kbps INTEGER NOT NULL DEFAULT 0,
  authorized_expiry INTEGER);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_client ON sessions(client_id, status);

CREATE TABLE IF NOT EXISTS coin_claims (
  id INTEGER PRIMARY KEY,
  client_id INTEGER NOT NULL REFERENCES clients(id),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed','expired')));
CREATE INDEX IF NOT EXISTS idx_claims_status ON coin_claims(status, expires_at);

CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY,
  payment_id TEXT NOT NULL UNIQUE,
  client_id INTEGER REFERENCES clients(id),
  session_id INTEGER REFERENCES sessions(id),
  amount INTEGER NOT NULL,
  pulses INTEGER,
  credited_seconds INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('credited','unassigned','rejected')),
  source TEXT NOT NULL,
  created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_payments_created ON payments(created_at);

CREATE TABLE IF NOT EXISTS coin_events (
  id INTEGER PRIMARY KEY,
  event_uid TEXT NOT NULL UNIQUE,
  pulses INTEGER,
  value INTEGER,
  source TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'received'
    CHECK (status IN ('received','credited','unassigned','invalid')),
  payment_id INTEGER REFERENCES payments(id),
  created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_coin_events_created ON coin_events(created_at);

CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY,
  payment_row_id INTEGER NOT NULL UNIQUE REFERENCES payments(id),
  amount INTEGER NOT NULL,
  sale_date TEXT NOT NULL,
  created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(sale_date);

CREATE TABLE IF NOT EXISTS system_events (
  id INTEGER PRIMARY KEY,
  level TEXT NOT NULL, source TEXT NOT NULL, message TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_created ON system_events(created_at);

CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY,
  admin_id INTEGER REFERENCES admins(id),
  username TEXT, action TEXT NOT NULL, detail TEXT, ip TEXT, created_at INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS login_attempts (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_login_key ON login_attempts(key, ts);
