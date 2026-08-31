"""Shared role enums, mirroring the server's `OrgRole`."""

from __future__ import annotations

from enum import Enum


class OrgRole(str, Enum):
    ORG_ADMIN = "ORG_ADMIN"
    USER = "USER"
    PLATFORM_ADMIN = "PLATFORM_ADMIN"


class UserOrgRole(str, Enum):
    """The subset `vl user add` grants — a new user is never bootstrapped as PLATFORM_ADMIN."""

    ORG_ADMIN = "ORG_ADMIN"
    USER = "USER"
