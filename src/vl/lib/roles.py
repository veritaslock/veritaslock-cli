"""Shared role enums, mirroring the server's `OrgRole`."""

from __future__ import annotations

from enum import Enum


class OrgRole(str, Enum):
    ORG_ADMIN = "ORG_ADMIN"
    USER = "USER"
    PLATFORM_ADMIN = "PLATFORM_ADMIN"
    # Organization-key read access only — independently grantable/revocable,
    # not a super/subset of USER (idp-org-key-rbac-spec.md).
    KEY_READER = "KEY_READER"


class UserOrgRole(str, Enum):
    """The subset `vl user add` grants — a new user is never bootstrapped as PLATFORM_ADMIN."""

    ORG_ADMIN = "ORG_ADMIN"
    USER = "USER"


class ServiceAccountRole(str, Enum):
    """Server's `ServiceAccountRole` minus PUBLISHER (retired, never issued locally)."""

    ACCOUNT = "ACCOUNT"
    NODE = "NODE"
    SYSTEM = "SYSTEM"
    INGEST_CLIENT = "INGEST_CLIENT"
