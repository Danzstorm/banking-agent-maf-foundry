"""Azure SQL compatibility, checked offline: no driver connection, no network."""

from sqlalchemy.dialects import mssql
from sqlalchemy.dialects.mssql.mssqlpython import MSDialect_mssqlpython
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateTable

from mcp_server.db import customers, idempotency_keys, metadata


def test_schema_compiles_for_sql_server_with_unicode_types():
    # A live connection to Azure SQL (>= 2012) turns this on, so UnicodeText -> NVARCHAR(max).
    dialect = mssql.dialect(deprecate_large_types=True)
    ddl = "\n".join(str(CreateTable(t).compile(dialect=dialect)) for t in metadata.sorted_tables)
    assert "VARCHAR(" not in ddl.replace("NVARCHAR(", "")  # every string column is NVARCHAR
    assert "NVARCHAR(max)" in ddl
    assert "IDENTITY" in str(CreateTable(customers).compile(dialect=dialect))
    assert "PRIMARY KEY (idem_key)" in str(CreateTable(idempotency_keys).compile(dialect=dialect))


def test_managed_identity_url_becomes_odbc_connection_string():
    url = make_url(
        "mssql+mssqlpython://@sql-x.database.windows.net:1433/sqldb-banking"
        "?Authentication=ActiveDirectoryMSI&UID=11111111-2222-3333-4444-555555555555&Encrypt=yes"
    )
    (conn_str,), _ = MSDialect_mssqlpython().create_connect_args(url)
    parts = set(conn_str.split(";"))
    assert {
        "Server=sql-x.database.windows.net,1433",
        "Database=sqldb-banking",
        "Authentication=ActiveDirectoryMSI",
        "UID=11111111-2222-3333-4444-555555555555",
        "Encrypt=yes",
    } <= parts
    assert not any(p.startswith("PWD=") for p in parts)
