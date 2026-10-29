"""Local identity and explicit resource grants for isolated agent development.

Credentials and grants are supplied by the server operator, never by request
payloads. Company identity and production knowledge ACLs remain separate.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Principal:
    org_id: str
    employee_id: str

    def __post_init__(self):
        if not self.org_id.strip() or not self.employee_id.strip():
            raise ValueError('Organization and employee must be supplied')


@dataclass(frozen=True)
class Grant:
    principal: Principal
    action: str
    resource_type: str
    resource_id: str

    def __post_init__(self):
        if any(not value.strip() or value == '*' for value in (self.action, self.resource_type, self.resource_id)):
            raise ValueError('Grant must specify an exact action and resource')


class LocalAccess:
    """Verify local bearer credentials and authorize exact resources.

    Replace grants on revocation; authorization queries always consult current
    grants. A channel conversation ID and request-supplied employee ID never
    authenticate a caller. This is an isolated deployment component only.
    """
    def __init__(self, credentials: Mapping[str, Principal], grants: set[Grant]):
        if (not credentials or any(not token or not token.strip() for token in credentials)
                or any(principal.employee_id.startswith('group:') for principal in credentials.values())):
            raise ValueError('At least one nonempty local credential is required')
        self._credentials = dict(credentials)
        self._grants = frozenset(grants)

    def authenticate(self, bearer: str) -> Principal:
        if not bearer:
            raise PermissionError('Authentication failed')
        for token, principal in self._credentials.items():
            if hmac.compare_digest(token.encode('utf-8'), bearer.encode('utf-8')):
                return principal
        raise PermissionError('Authentication failed')

    def replace_grants(self, grants: set[Grant]) -> None:
        self._grants = frozenset(grants)

    def revoke_principal(self, principal: Principal) -> None:
        """Remove all credentials for a principal; queued work rechecks this."""
        self._credentials = {token: owner for token, owner in self._credentials.items() if owner != principal}

    def is_active(self, principal: Principal) -> bool:
        return principal in self._credentials.values()

    def allowed(self, principal: Principal, action: str, resource_type: str, resource_id: str) -> bool:
        return Grant(principal, action, resource_type, resource_id) in self._grants

    def require(self, principal: Principal, action: str, resource_type: str, resource_id: str) -> None:
        if not self.allowed(principal, action, resource_type, resource_id):
            raise PermissionError('Resource access denied')

    def require_group_delivery(self, principal: Principal, group_id: str,
                               resource_type: str, resource_id: str) -> None:
        self.require(principal, 'read', resource_type, resource_id)
        self.require(principal, 'deliver', 'group', group_id)
        self.require(Principal(principal.org_id, f'group:{group_id}'), 'read', resource_type, resource_id)
