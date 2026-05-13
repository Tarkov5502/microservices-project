-- scripts/init-db.sql
-- Creates per-service databases inside the single PostgreSQL container.
-- In production each service has its own Flexible Server (separate billing/isolation).
-- Locally we share one server but maintain separate databases for the same isolation pattern.

CREATE DATABASE userdb;
CREATE DATABASE taskdb;

-- Grant the app user access to both databases
GRANT ALL PRIVILEGES ON DATABASE userdb TO appuser;
GRANT ALL PRIVILEGES ON DATABASE taskdb TO appuser;
