-- Run once after `terraform apply`, connected to the sqldb-banking database (not master)
-- as the Entra admin (the user who ran terraform).
-- The name must match the user-assigned identity: id-mcp-server.

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'id-mcp-server')
    CREATE USER [id-mcp-server] FROM EXTERNAL PROVIDER;

-- Reads and writes on the banking tables. The schema is created by the seed (run as admin),
-- so the identity does not need DDL rights.
ALTER ROLE db_datareader ADD MEMBER [id-mcp-server];
ALTER ROLE db_datawriter ADD MEMBER [id-mcp-server];
