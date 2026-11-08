"""Small provider representation; persisted and executable contracts stay local.

The playbook has twelve possible local values (plus absence). Encode each as
one choice instead of an object containing nested nullable/literal branches.
``unspecified`` preserves missing population/segment choices; it never supplies
the qualifying population or country grouping on the user's behalf.
"""
from typing import Literal

from .contracts import InterpretationProposal, PlaybookChoice, RequestSpec


PlaybookToken = Literal[
    "product_revenue|unspecified|unspecified",
    "product_revenue|unspecified|country",
    "product_revenue|qualifying_purchasers|unspecified",
    "product_revenue|qualifying_purchasers|country",
    "regional_spend|unspecified|unspecified",
    "regional_spend|unspecified|country",
    "regional_spend|qualifying_purchasers|unspecified",
    "regional_spend|qualifying_purchasers|country",
    "churn_change|unspecified|unspecified",
    "churn_change|unspecified|country",
    "churn_change|qualifying_purchasers|unspecified",
    "churn_change|qualifying_purchasers|country",
]


class ProviderRequestSpec(RequestSpec):
    playbook: PlaybookToken | None = None


class ProviderInterpretationProposal(InterpretationProposal):
    request: ProviderRequestSpec | None = None


def encode_interpretation(proposal: InterpretationProposal) -> dict:
    """Encode a validated local proposal for fixtures/provider protocol checks."""
    # Revalidate even model_copy/model_construct input; don't repair invalid data.
    data = InterpretationProposal.model_validate(proposal.model_dump(mode="json")).model_dump(mode="json")
    if data["request"] and data["request"]["playbook"] is not None:
        choice = data["request"]["playbook"]
        data["request"]["playbook"] = "|".join((choice["id"],
            choice["population"] or "unspecified", choice["segment"] or "unspecified"))
    return ProviderInterpretationProposal.model_validate(data).model_dump(mode="json")


def decode_interpretation(payload: dict, *, stage=None) -> InterpretationProposal:
    """Validate the wire response, then reconstruct and validate local choices."""
    if stage: stage("wire_validation")
    data = ProviderInterpretationProposal.model_validate(payload).model_dump(mode="json")
    if data["request"] and data["request"]["playbook"] is not None:
        identifier, population, segment = data["request"]["playbook"].split("|")
        data["request"]["playbook"] = PlaybookChoice(id=identifier, version=1,
            population=None if population == "unspecified" else population,
            segment=None if segment == "unspecified" else segment).model_dump(mode="json")
    if stage: stage("local_validation")
    return InterpretationProposal.model_validate(data)
