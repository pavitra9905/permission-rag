"""ORM models. The DDL source of truth is the Alembic migration (it also holds RLS + grants)."""
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.constants import EMBEDDING_DIM


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True), str: Text}


def uuid_pk():
    return mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))


def now_col():
    return mapped_column(server_default=text("now()"))


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    created_at: Mapped[datetime] = now_col()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "email"),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    email: Mapped[str]
    password_hash: Mapped[str]
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[datetime] = now_col()


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id"], ["users.tenant_id", "users.id"], ondelete="CASCADE"
        ),
    )
    tenant_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(primary_key=True)  # admin | editor | viewer


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(["tenant_id", "created_by"], ["users.tenant_id", "users.id"]),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    title: Mapped[str]
    filename: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    error: Mapped[str | None]
    created_by: Mapped[uuid.UUID]
    created_at: Mapped[datetime] = now_col()


class DocumentPermission(Base):
    __tablename__ = "document_permissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"], ["documents.tenant_id", "documents.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"], ["users.tenant_id", "users.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["tenant_id", "granted_by"], ["users.tenant_id", "users.id"]),
    )
    tenant_id: Mapped[uuid.UUID]
    document_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    permission: Mapped[str]  # read | write
    granted_by: Mapped[uuid.UUID]
    created_at: Mapped[datetime] = now_col()


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index"),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"], ["documents.tenant_id", "documents.id"], ondelete="CASCADE"
        ),
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID]
    document_id: Mapped[uuid.UUID]
    chunk_index: Mapped[int]
    content: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    created_at: Mapped[datetime] = now_col()


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[uuid.UUID | None]
    action: Mapped[str]
    resource_type: Mapped[str | None]
    resource_id: Mapped[uuid.UUID | None]
    details: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = now_col()