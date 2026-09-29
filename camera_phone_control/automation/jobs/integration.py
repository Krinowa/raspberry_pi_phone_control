"""Provider-neutral boundary for future external job transports."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from automation.workflows.validation import identifier, resolve_variables

from .storage import RecordConflict


def _text(value, label, maximum, *, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    value = value.strip()
    if (
        (required and not value)
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{label} must be valid text of at most {maximum} characters.")
    return value or None


def _payload(value):
    if not isinstance(value, dict):
        raise ValueError("payload must be a JSON object.")
    try:
        encoded = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must contain valid JSON values.") from exc
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise ValueError("payload is too large.")
    return deepcopy(value)


@dataclass(frozen=True)
class NormalizedJobRequest:
    """Transport-independent data produced by an external adapter."""

    external_ref: str | None
    bank: str
    action: str
    payload: dict

    @classmethod
    def create(cls, *, external_ref=None, bank, action, payload):
        return cls(
            external_ref=_text(external_ref, "external_ref", 256, required=False),
            bank=_text(bank, "bank", 80),
            action=_text(action, "action", 80),
            payload=_payload(payload),
        )


@dataclass(frozen=True)
class JobRoute:
    """Internal route selected independently of external payload parsing."""

    account_id: str
    workflow: str

    @classmethod
    def create(cls, *, account_id, workflow):
        return cls(
            account_id=identifier(account_id),
            workflow=identifier(workflow),
        )


@runtime_checkable
class IntegrationAdapter(Protocol):
    def normalize(self, external_payload: Any) -> NormalizedJobRequest:
        """Normalize one provider payload without resolving a device."""


@runtime_checkable
class JobRouteResolver(Protocol):
    def resolve(self, request: NormalizedJobRequest) -> JobRoute:
        """Map normalized metadata/reference to an existing account and workflow."""


class ExternalIntegrationService:
    """Normalize, route, validate, and enqueue an external request."""

    def __init__(
        self,
        adapter,
        route_resolver,
        account_store,
        workflow_storage,
        scheduler,
    ):
        self.adapter = adapter
        self.route_resolver = route_resolver
        self.account_store = account_store
        self.workflow_storage = workflow_storage
        self.scheduler = scheduler

    def submit(self, external_payload):
        request = self.adapter.normalize(external_payload)
        if not isinstance(request, NormalizedJobRequest):
            raise TypeError("Integration adapters must return a NormalizedJobRequest.")

        route = self.route_resolver.resolve(request)
        if not isinstance(route, JobRoute):
            raise TypeError("Job route resolvers must return a JobRoute.")

        account = self.account_store.get(route.account_id)
        if not account.enabled:
            raise RecordConflict("Account binding is disabled.")
        
        workflow = self.workflow_storage.read(route.workflow)
        resolve_variables(workflow, request.payload)
        return self.scheduler.enqueue(
            account,
            workflow["id"],
            request.payload,
            external_ref=request.external_ref,
            bank=request.bank,
            action=request.action,
        )
