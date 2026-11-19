"""initial schema: tables, row-level security, grants

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Tables that need RLS enabled but NOT forced on the owner (login function reads them).
LOOKUP_TABLES = ["users", "user_roles"]
# Tables where even the owner must obey RLS.
FORCED_TABLES = ["documents", "document_permissions", "chunks"]

SCHEMA = [
    # Current tenant comes from a per-transaction setting. Unset/empty -> NULL -> no rows.
    """CREATE FUNCTION app_current_tenant() RETURNS uuid
       LANGUAGE sql STABLE
       AS $$ SELECT nullif(current_setting('app.current_tenant', true), '')::uuid $$""",
    """CREATE TABLE tenants (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
        name text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE users (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants (id),
        email text NOT NULL CHECK (email = lower(email)),
        password_hash text NOT NULL,
        is_active boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, id),
        UNIQUE (tenant_id, email)
    )""",
    """CREATE TABLE user_roles (
        tenant_id uuid NOT NULL,
        user_id uuid NOT NULL,
        role text NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
        PRIMARY KEY (user_id, role),
        FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE
    )""",
    """CREATE TABLE documents (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants (id),
        title text NOT NULL,
        filename text,
        status text NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
        error text,
        created_by uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, id),
        FOREIGN KEY (tenant_id, created_by) REFERENCES users (tenant_id, id)
    )""",
    """CREATE TABLE document_permissions (
        tenant_id uuid NOT NULL,
        document_id uuid NOT NULL,
        user_id uuid NOT NULL,
        permission text NOT NULL CHECK (permission IN ('read', 'write')),
        granted_by uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (document_id, user_id),
        FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id) ON DELETE CASCADE,
        FOREIGN KEY (tenant_id, user_id) REFERENCES users (tenant_id, id) ON DELETE CASCADE,
        FOREIGN KEY (tenant_id, granted_by) REFERENCES users (tenant_id, id)
    )""",
    "CREATE INDEX document_permissions_user_idx ON document_permissions (user_id)",
    # vector(384) is frozen on purpose: all-MiniLM-L6-v2 produces 384 dims.
    """CREATE TABLE chunks (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL,
        document_id uuid NOT NULL,
        chunk_index integer NOT NULL,
        content text NOT NULL,
        embedding vector(384) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (document_id, chunk_index),
        FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id) ON DELETE CASCADE
    )""",
    "CREATE INDEX chunks_tenant_document_idx ON chunks (tenant_id, document_id)",
    "CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)",
    """CREATE TABLE audit_log (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants (id),
        user_id uuid,
        action text NOT NULL,
        resource_type text,
        resource_id uuid,
        details jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX audit_log_tenant_created_idx ON audit_log (tenant_id, created_at DESC)",
    # Defense in depth: even the owner cannot rewrite history.
    """CREATE FUNCTION audit_log_block_mutation() RETURNS trigger
       LANGUAGE plpgsql
       AS $$ BEGIN RAISE EXCEPTION 'audit_log is append-only'; END $$""",
    """CREATE TRIGGER audit_log_no_update_delete
       BEFORE UPDATE OR DELETE ON audit_log
       FOR EACH ROW EXECUTE FUNCTION audit_log_block_mutation()""",
    """CREATE TRIGGER audit_log_no_truncate
       BEFORE TRUNCATE ON audit_log
       FOR EACH STATEMENT EXECUTE FUNCTION audit_log_block_mutation()""",
]

# Login must find a user BEFORE any tenant is known, so it goes through this one
# audited door instead of loosening RLS on users.
LOGIN_FUNCTION = """CREATE FUNCTION app_login_lookup(p_slug text, p_email text)
RETURNS TABLE (user_id uuid, tenant_id uuid, password_hash text, roles text[])
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT u.id, u.tenant_id, u.password_hash,
           COALESCE(array_agg(r.role) FILTER (WHERE r.role IS NOT NULL), ARRAY[]::text[])
    FROM tenants t
    JOIN users u ON u.tenant_id = t.id
    LEFT JOIN user_roles r ON r.user_id = u.id
    WHERE t.slug = p_slug AND u.email = lower(p_email) AND u.is_active
    GROUP BY u.id, u.tenant_id, u.password_hash
$$"""

GRANTS = [
    "GRANT SELECT ON tenants TO rag_app",
    "GRANT SELECT, INSERT, UPDATE ON users TO rag_app",
    "GRANT SELECT, INSERT, DELETE ON user_roles TO rag_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON documents TO rag_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON document_permissions TO rag_app",
    "GRANT SELECT, INSERT, DELETE ON chunks TO rag_app",
    "GRANT SELECT, INSERT ON audit_log TO rag_app",  # no UPDATE / DELETE / TRUNCATE
    "REVOKE ALL ON FUNCTION app_login_lookup(text, text) FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION app_login_lookup(text, text) TO rag_app",
]

TENANT_CHECK = "tenant_id = (SELECT app_current_tenant())"


def upgrade() -> None:
    for stmt in SCHEMA:
        op.execute(stmt)

    # --- Row-Level Security ---
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON tenants USING (id = (SELECT app_current_tenant()))"
    )
    for table in LOOKUP_TABLES + FORCED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({TENANT_CHECK}) WITH CHECK ({TENANT_CHECK})"
        )
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY audit_select ON audit_log FOR SELECT USING ({TENANT_CHECK})")
    op.execute(f"CREATE POLICY audit_insert ON audit_log FOR INSERT WITH CHECK ({TENANT_CHECK})")
    for table in FORCED_TABLES + ["audit_log"]:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(LOGIN_FUNCTION)
    for stmt in GRANTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_login_lookup(text, text)")
    for table in ["audit_log", "chunks", "document_permissions", "documents",
                  "user_roles", "users", "tenants"]:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS audit_log_block_mutation()")
    op.execute("DROP FUNCTION IF EXISTS app_current_tenant()")
    