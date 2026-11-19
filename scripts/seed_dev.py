"""DEV ONLY. Seeds 2 tenants x 3 users (admin/editor/viewer). Idempotent."""
import psycopg

from app.core.config import settings
from app.core.security import hash_password

PASSWORD = "Passw0rd!dev"
TENANTS = [("acme", "Acme Corp"), ("globex", "Globex Inc")]
ROLES = ["admin", "editor", "viewer"]

dsn = settings.migration_database_url.replace("+psycopg", "")
pw_hash = hash_password(PASSWORD)

with psycopg.connect(dsn) as conn:  # commits on clean exit
    for slug, name in TENANTS:
        tenant_id = conn.execute(
            "INSERT INTO tenants (slug, name) VALUES (%s, %s) "
            "ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            (slug, name),
        ).fetchone()[0]
        for role in ROLES:
            user_id = conn.execute(
                "INSERT INTO users (tenant_id, email, password_hash) VALUES (%s, %s, %s) "
                "ON CONFLICT (tenant_id, email) DO UPDATE SET password_hash = EXCLUDED.password_hash "
                "RETURNING id",
                (tenant_id, f"{role}@{slug}.test", pw_hash),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO user_roles (tenant_id, user_id, role) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (tenant_id, user_id, role),
            )
        print(f"seeded tenant '{slug}': " + ", ".join(f"{r}@{slug}.test" for r in ROLES))

print(f"password for all dev users: {PASSWORD}")