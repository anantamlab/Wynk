"""Outbound data policy (audit finding F2): screen what leaves for a model provider.

Uploaded datasets are screened when they are stored (``ingestion.sensitive``). The inputs of an
invocation and the values of a feedback never pass through an upload, so before this module they
reached the model provider (an invocation) or a challenger dataset (a feedback label) unscreened.
An ``OutboundPolicy`` closes that gap with the SAME fixed-format rules (``ingestion.sensitive.
find``): no model, no external screening service, no heuristics.

    off     nothing is screened (the behaviour before this policy existed)
    screen  every string and integer of the values (nested ones included) is matched; a
            category whose action is ``block`` refuses the request BEFORE any model or search
            request is bound or sent (``outbound_data_blocked``, 422); ``allow`` sends it

There is deliberately no ``redact``: an invocation's request hash binds its feedback, its replay
and its telemetry, and a redacted question is a different question. Rewriting inputs on their way
out would silently change their meaning, so a category is either sent verbatim or refused.

Where a policy comes from (``store.identity`` ``outbound_policies``, one append-only revision
history per workspace):

* a workspace created after this policy existed starts with ``DEFAULT_POLICY`` (``screen``,
  credentials and government / financial identifiers blocked, contact data allowed), recorded
  as revision 1 of that workspace;
* a workspace created before it (and a legacy single-tenant workspace) has no revision and keeps
  ``off``: its existing deployments behave exactly as before. ``python -m api.tenancy
  outbound-policy --workspace-id ws-... --set screen`` opts it in (explicit, versioned, recorded);
* an in-process ``ChampionDeployments`` without a policy source screens nothing (tests, tools).

A refusal names the categories and the input fields, never a value or any part of one.

Recall is bounded by construction: the rules find the listed formats (keys and tokens with a
fixed shape, Luhn-valid card numbers with a network prefix, US SSNs, IBANs, emails, ``+`` phone
numbers). Names, addresses, free-text health or financial details, card numbers with unusual
separators and anything without a fixed format are NOT detected and are sent as given.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.canonical import canonical_hash
from ingestion.sensitive import RULES, Category, Kind, find

OUTBOUND_SCHEMA = "wynk-outbound-policy/1"


class OutboundMode(StrEnum):
    OFF = "off"
    SCREEN = "screen"


class OutboundAction(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


_SECRETS = frozenset(r.category for r in RULES if r.kind is Kind.SECRET)
DEFAULT_ACTIONS: dict[Category, OutboundAction] = {
    **dict.fromkeys(sorted(_SECRETS), OutboundAction.BLOCK),
    Category.PAYMENT_CARD: OutboundAction.BLOCK,
    Category.US_SSN: OutboundAction.BLOCK,
    Category.IBAN: OutboundAction.BLOCK,
    # contact data is common in legitimate requests ("draft a reply to ...@..."): sent, but a
    # workspace can block it (``email=block,phone=block``)
    Category.EMAIL: OutboundAction.ALLOW,
    Category.PHONE: OutboundAction.ALLOW,
}


class OutboundPolicy(BaseModel):
    """One mode and one action per category. Total: a missing category is a config error, and
    a credential can never be allowed out while screening is on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_schema: Literal["wynk-outbound-policy/1"] = OUTBOUND_SCHEMA
    mode: OutboundMode = OutboundMode.SCREEN
    actions: dict[Category, OutboundAction] = Field(default_factory=lambda: dict(DEFAULT_ACTIONS))

    def model_post_init(self, _: Any) -> None:
        missing = set(Category) - set(self.actions)
        if missing:
            raise ValueError(f"the policy has no action for {sorted(c.value for c in missing)}")
        allowed = sorted(c.value for c in _SECRETS if self.actions[c] is OutboundAction.ALLOW)
        if allowed and self.mode is OutboundMode.SCREEN:
            raise ValueError(f"credentials cannot be allowed out: {allowed}")

    @property
    def policy_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))

    @property
    def screens(self) -> bool:
        return self.mode is OutboundMode.SCREEN

    @classmethod
    def parse(cls, spec: str) -> OutboundPolicy:
        """``off`` | ``screen`` | ``screen:email=block,phone=block``. An unknown mode, category
        or action - and ``redact``, which outbound screening never does - is refused."""
        mode, _, overrides = spec.strip().partition(":")
        try:
            parsed_mode = OutboundMode(mode.strip())
        except ValueError:
            raise ValueError("the outbound mode is 'off' or 'screen'") from None
        actions = dict(DEFAULT_ACTIONS)
        for item in filter(None, (p.strip() for p in overrides.split(","))):
            name, sep, value = item.partition("=")
            if not sep:
                raise ValueError(f"outbound policy entry {item[:60]!r} is not category=action")
            if value.strip() == "redact":
                raise ValueError(
                    "outbound screening never redacts: a rewritten input is a different request "
                    "(use block or allow)"
                )
            try:
                actions[Category(name.strip())] = OutboundAction(value.strip())
            except ValueError:
                raise ValueError(f"unknown outbound category or action in {item[:60]!r}") from None
        return cls(mode=parsed_mode, actions=actions)


DEFAULT_POLICY = OutboundPolicy()  # new workspaces
UNSCREENED = OutboundPolicy(mode=OutboundMode.OFF)  # workspaces from before the policy existed


class OutboundFinding(BaseModel):
    """What matched where. Never the value, a prefix or a hash of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str  # the top-level input / answer field (named by the pinned contract)
    category: Category
    action: OutboundAction


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, bool) or value is None or isinstance(value, float):
        return
    elif isinstance(value, int):
        yield str(value)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)


def screen_values(
    values: Mapping[str, Any] | None, policy: OutboundPolicy, *, prefix: str = ""
) -> list[OutboundFinding]:
    """Every (field, category) the policy finds in ``values`` (sorted; none when ``off``)."""
    if values is None or not policy.screens:
        return []
    found: set[tuple[str, Category]] = set()
    for name, value in values.items():
        for text in _strings(value):
            for _, _, category in find(text):
                found.add((prefix + str(name), category))
    return [
        OutboundFinding(field=f, category=c, action=policy.actions[c])
        for f, c in sorted(found, key=lambda fc: (fc[0], fc[1].value))
    ]


def blocked(findings: list[OutboundFinding]) -> list[OutboundFinding]:
    return [f for f in findings if f.action is OutboundAction.BLOCK]


def refusal_details(found: list[OutboundFinding], policy: OutboundPolicy) -> dict[str, str]:
    """The machine-readable part of a refusal: categories and fields, never a value."""
    return {
        "categories": ",".join(sorted({f.category.value for f in found})),
        "fields": ",".join(sorted({f.field for f in found})),
        "policy_version": OUTBOUND_SCHEMA,
        "policy_hash": policy.policy_hash,
    }


REFUSAL_MESSAGE = (
    "the request holds data this workspace's outbound policy does not send to a model provider "
    "({categories} in {fields}); nothing was sent. Remove that value, or ask a workspace owner "
    "to change the outbound policy"
)

__all__ = [
    "DEFAULT_ACTIONS",
    "DEFAULT_POLICY",
    "OUTBOUND_SCHEMA",
    "REFUSAL_MESSAGE",
    "UNSCREENED",
    "OutboundAction",
    "OutboundFinding",
    "OutboundMode",
    "OutboundPolicy",
    "blocked",
    "refusal_details",
    "screen_values",
]
