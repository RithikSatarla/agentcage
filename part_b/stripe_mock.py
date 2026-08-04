"""A stateful in-memory model of the part of the Stripe API that mutates money.

This is not a recorded-response fixture. Writes actually change state, and later reads
observe those changes: refunding a charge increases ``charge.amount_refunded``, and a
subsequent ``GET /v1/charges/{id}`` returns the updated charge. That is the property
replay-based mocking cannot give you, and the reason an agent that double-refunds is
caught here but not against a fixture.

Modelled behaviour (see PROTOCOL.md for the fidelity boundary):

* refunds are capped at the unrefunded amount, with Stripe's real error message;
* a fully refunded charge rejects further refunds with ``charge_already_refunded``;
* idempotency keys replay the original response, and reject reuse with different
  parameters;
* identifiers are assigned from a counter, so a run is byte-for-byte reproducible.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

__all__ = [
    "StripeError",
    "Customer",
    "Charge",
    "Refund",
    "StripeMock",
    "VALID_REFUND_REASONS",
]

VALID_REFUND_REASONS = {"duplicate", "fraudulent", "requested_by_customer"}


class StripeError(Exception):
    """A Stripe-shaped API error, carrying the HTTP status it would be returned with."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int = 400,
        code: Optional[str] = None,
        error_type: str = "invalid_request_error",
        param: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = code
        self.error_type = error_type
        self.param = param

    def to_dict(self) -> Dict[str, Any]:
        error: Dict[str, Any] = {"type": self.error_type, "message": self.message}
        if self.code:
            error["code"] = self.code
        if self.param:
            error["param"] = self.param
        return {"error": error}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<StripeError {self.http_status} {self.code or self.error_type}: {self.message}>"


# --- entities ----------------------------------------------------------------


class Customer(BaseModel):
    id: str
    object: str = "customer"
    created: int
    email: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    currency: Optional[str] = None
    delinquent: bool = False
    livemode: bool = False
    metadata: Dict[str, str] = Field(default_factory=dict)


class Charge(BaseModel):
    id: str
    object: str = "charge"
    created: int
    amount: int
    amount_captured: int = 0
    amount_refunded: int = 0
    currency: str = "usd"
    customer: Optional[str] = None
    description: Optional[str] = None
    captured: bool = True
    paid: bool = True
    refunded: bool = False
    status: str = "succeeded"
    livemode: bool = False
    metadata: Dict[str, str] = Field(default_factory=dict)

    @property
    def amount_refundable(self) -> int:
        """Amount still available to refund."""
        return max(0, self.amount_captured - self.amount_refunded)


class Refund(BaseModel):
    id: str
    object: str = "refund"
    created: int
    amount: int
    charge: str
    currency: str = "usd"
    reason: Optional[str] = None
    status: str = "succeeded"
    metadata: Dict[str, str] = Field(default_factory=dict)


def _money(amount: int, currency: str) -> str:
    """Format minor units the way Stripe renders them in error messages."""
    symbol = {"usd": "$", "eur": "€", "gbp": "£"}.get(currency.lower(), "")
    return f"{symbol}{amount / 100:.2f}"


def _as_int(value: Any, param: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise StripeError(
            f"Invalid integer: {value!r}", code="parameter_invalid_integer", param=param
        )


def _as_metadata(value: Any) -> Dict[str, str]:
    if not value:
        return {}
    if not isinstance(value, dict):
        raise StripeError("Invalid metadata: expected an object", param="metadata")
    return {str(k): str(v) for k, v in value.items()}


# --- the mock ----------------------------------------------------------------


class StripeMock:
    """In-memory Stripe. Every method mirrors one real endpoint."""

    def __init__(self, clock: Optional[Callable[[], float]] = None):
        self._clock = clock or time.time
        self._counters: Dict[str, int] = {}
        self.customers: Dict[str, Customer] = {}
        self.charges: Dict[str, Charge] = {}
        self.refunds: Dict[str, Refund] = {}
        #: idempotency_key -> (endpoint, parameter fingerprint, stored result)
        self._idempotency: Dict[str, Tuple[str, str, Any]] = {}

    # -- helpers -------------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}_{self._counters[prefix]:08d}"

    def _now(self) -> int:
        return int(self._clock())

    def _idempotent(self, key: Optional[str], endpoint: str, params: Dict[str, Any]) -> Any:
        """Return a stored result for ``key``, or raise if reused with other parameters."""
        if not key:
            return None
        fingerprint = repr(sorted((k, repr(v)) for k, v in params.items() if v is not None))
        stored = self._idempotency.get(key)
        if stored is None:
            return None
        seen_endpoint, seen_fingerprint, result = stored
        if seen_endpoint != endpoint or seen_fingerprint != fingerprint:
            raise StripeError(
                "Keys for idempotent requests can only be used with the same parameters "
                "they were first used with.",
                code="idempotency_key_in_use",
            )
        return result

    def _remember(self, key: Optional[str], endpoint: str, params: Dict[str, Any],
                  result: Any) -> None:
        if not key:
            return
        fingerprint = repr(sorted((k, repr(v)) for k, v in params.items() if v is not None))
        self._idempotency[key] = (endpoint, fingerprint, result)

    # -- customers -----------------------------------------------------------

    def post_customer(
        self,
        email: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Customer:
        """POST /v1/customers"""
        customer = Customer(
            id=self._next_id("cus"),
            created=self._now(),
            email=email,
            name=name,
            description=description,
            metadata=_as_metadata(metadata),
        )
        self.customers[customer.id] = customer
        return customer

    def get_customer(self, customer_id: str) -> Customer:
        """GET /v1/customers/{id}"""
        customer = self.customers.get(customer_id)
        if customer is None:
            raise StripeError(
                f"No such customer: '{customer_id}'",
                http_status=404,
                code="resource_missing",
                param="id",
            )
        return customer

    # -- charges -------------------------------------------------------------

    def post_charge(
        self,
        amount: int,
        currency: str = "usd",
        customer: Optional[str] = None,
        description: Optional[str] = None,
        capture: bool = True,
        metadata: Optional[Dict[str, str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Charge:
        """POST /v1/charges"""
        params = {
            "amount": amount, "currency": currency, "customer": customer,
            "description": description, "capture": capture, "metadata": metadata,
        }
        replayed = self._idempotent(idempotency_key, "post_charge", params)
        if replayed is not None:
            return replayed

        amount = _as_int(amount, "amount")
        if amount <= 0:
            raise StripeError(
                "Invalid positive integer", code="parameter_invalid_integer", param="amount"
            )
        if not isinstance(currency, str) or len(currency) != 3:
            raise StripeError(f"Invalid currency: {currency}", param="currency")
        if customer is not None:
            self.get_customer(customer)  # raises 404 resource_missing

        charge = Charge(
            id=self._next_id("ch"),
            created=self._now(),
            amount=amount,
            amount_captured=amount if capture else 0,
            currency=currency.lower(),
            customer=customer,
            description=description,
            captured=bool(capture),
            metadata=_as_metadata(metadata),
        )
        self.charges[charge.id] = charge
        self._remember(idempotency_key, "post_charge", params, charge)
        return charge

    def get_charge(self, charge_id: str) -> Charge:
        """GET /v1/charges/{id} - reflects every mutation applied so far."""
        charge = self.charges.get(charge_id)
        if charge is None:
            raise StripeError(
                f"No such charge: '{charge_id}'",
                http_status=404,
                code="resource_missing",
                param="id",
            )
        return charge

    # -- refunds -------------------------------------------------------------

    def post_refund(
        self,
        charge: str,
        amount: Optional[int] = None,
        reason: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Refund:
        """POST /v1/refunds - mutates the referenced charge."""
        params = {"charge": charge, "amount": amount, "reason": reason, "metadata": metadata}
        replayed = self._idempotent(idempotency_key, "post_refund", params)
        if replayed is not None:
            return replayed

        target = self.get_charge(charge)  # raises 404 resource_missing

        if not target.captured:
            raise StripeError(
                f"Charge {target.id} has not been captured; cannot refund an uncaptured charge.",
                code="charge_not_captured",
                param="charge",
            )
        if target.refunded or target.amount_refundable == 0:
            raise StripeError(
                f"Charge {target.id} has already been refunded.",
                code="charge_already_refunded",
                param="charge",
            )
        if reason is not None and reason not in VALID_REFUND_REASONS:
            raise StripeError(
                f"Invalid reason: {reason}. Must be one of "
                f"{', '.join(sorted(VALID_REFUND_REASONS))}.",
                param="reason",
            )

        amount = target.amount_refundable if amount is None else _as_int(amount, "amount")
        if amount <= 0:
            raise StripeError(
                "Invalid positive integer", code="parameter_invalid_integer", param="amount"
            )
        if amount > target.amount_refundable:
            raise StripeError(
                f"Refund amount ({_money(amount, target.currency)}) is greater than "
                f"unrefunded amount on charge "
                f"({_money(target.amount_refundable, target.currency)})",
                code="amount_too_large",
                param="amount",
            )

        refund = Refund(
            id=self._next_id("re"),
            created=self._now(),
            amount=amount,
            charge=target.id,
            currency=target.currency,
            reason=reason,
            metadata=_as_metadata(metadata),
        )
        self.refunds[refund.id] = refund

        # the mutation the whole framework exists to make observable
        target.amount_refunded += amount
        target.refunded = target.amount_refunded >= target.amount_captured

        self._remember(idempotency_key, "post_refund", params, refund)
        return refund

    def get_refund(self, refund_id: str) -> Refund:
        """GET /v1/refunds/{id}"""
        refund = self.refunds.get(refund_id)
        if refund is None:
            raise StripeError(
                f"No such refund: '{refund_id}'",
                http_status=404,
                code="resource_missing",
                param="id",
            )
        return refund

    def list_refunds(self, charge: Optional[str] = None, limit: int = 10) -> Dict[str, Any]:
        """GET /v1/refunds"""
        items = [r for r in self.refunds.values() if charge is None or r.charge == charge]
        items.sort(key=lambda r: r.id, reverse=True)
        clipped = items[: max(1, min(int(limit), 100))]
        return {
            "object": "list",
            "url": "/v1/refunds",
            "has_more": len(clipped) < len(items),
            "data": [r.model_dump() for r in clipped],
        }

    # -- HTTP surface --------------------------------------------------------

    def handle(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Route one request. Returns ``(status_code, json_body)`` and never raises
        :class:`StripeError` - errors come back in Stripe's error envelope, as they
        would over the wire."""
        body = dict(body or {})
        verb = method.upper()
        segments = [s for s in path.split("?")[0].strip("/").split("/") if s]
        if segments and segments[0] == "v1":
            segments = segments[1:]

        try:
            result = self._dispatch(verb, segments, body, idempotency_key)
        except StripeError as exc:
            return exc.http_status, exc.to_dict()

        if result is None:
            return 404, StripeError(
                f"Unrecognized request URL ({verb}: {path}).",
                http_status=404,
                code="url_invalid",
            ).to_dict()

        status = 200 if verb == "GET" else 200  # Stripe returns 200 on successful writes
        payload = result.model_dump() if isinstance(result, BaseModel) else result
        return status, payload

    def _dispatch(self, verb: str, segments: List[str], body: Dict[str, Any],
                  idempotency_key: Optional[str]) -> Any:
        if not segments:
            return None
        resource, rest = segments[0], segments[1:]

        if resource == "customers":
            if verb == "POST" and not rest:
                return self.post_customer(
                    email=body.get("email"), name=body.get("name"),
                    description=body.get("description"), metadata=body.get("metadata"),
                )
            if verb == "GET" and len(rest) == 1:
                return self.get_customer(rest[0])

        if resource == "charges":
            if verb == "POST" and not rest:
                return self.post_charge(
                    amount=body.get("amount"), currency=body.get("currency", "usd"),
                    customer=body.get("customer"), description=body.get("description"),
                    capture=body.get("capture", True), metadata=body.get("metadata"),
                    idempotency_key=idempotency_key,
                )
            if verb == "GET" and len(rest) == 1:
                return self.get_charge(rest[0])
            # POST /v1/charges/{id}/refunds
            if verb == "POST" and len(rest) == 2 and rest[1] == "refunds":
                return self.post_refund(
                    charge=rest[0], amount=body.get("amount"), reason=body.get("reason"),
                    metadata=body.get("metadata"), idempotency_key=idempotency_key,
                )

        if resource == "refunds":
            if verb == "POST" and not rest:
                return self.post_refund(
                    charge=body.get("charge"), amount=body.get("amount"),
                    reason=body.get("reason"), metadata=body.get("metadata"),
                    idempotency_key=idempotency_key,
                )
            if verb == "GET" and len(rest) == 1:
                return self.get_refund(rest[0])
            if verb == "GET" and not rest:
                return self.list_refunds(charge=body.get("charge"), limit=body.get("limit", 10))

        return None

    def __repr__(self) -> str:
        return (
            f"<StripeMock customers={len(self.customers)} charges={len(self.charges)} "
            f"refunds={len(self.refunds)}>"
        )
