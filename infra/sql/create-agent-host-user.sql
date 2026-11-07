-- Phase 6. Run once after `terraform apply`, connected to sqldb-banking as the Entra admin.
-- The name must match the user-assigned identity: id-agent-host.
-- Least privilege: the host maps oid -> customer and keeps conversation checkpoints. It never
-- reads accounts, cards or claims; those go through the MCP server.

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'id-agent-host')
    CREATE USER [id-agent-host] FROM EXTERNAL PROVIDER;

GRANT SELECT ON dbo.clientes TO [id-agent-host];
GRANT SELECT, INSERT, UPDATE ON dbo.sesiones TO [id-agent-host];
