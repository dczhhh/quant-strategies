"""Versioned investor scenarios, never a determination of actual tax liability."""

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from .models import aware

if TYPE_CHECKING:
    from .config import ConstraintConfig
    from .corporate_actions.events import CorporateAction


@dataclass(frozen=True)
class DividendTaxRule:
    source_country: str
    rate: float
    effective_from: date
    effective_to: date
    known_at: datetime
    verified_at: datetime
    source: str
    version: str
    distribution_type: str = "ordinary_stock"

    def __post_init__(self):
        aware(self.known_at)
        aware(self.verified_at)
        if (
            len(self.source_country) != 2
            or not self.source_country.isascii()
            or not self.source_country.isupper()
            or not self.source_country.isalpha()
            or isinstance(self.rate, bool)
            or not isinstance(self.rate, (int, float))
            or not math.isfinite(self.rate)
            or not 0 <= self.rate <= 1
            or type(self.effective_from) is not date
            or type(self.effective_to) is not date
            or self.effective_from >= self.effective_to
            or self.known_at > self.verified_at
            or not self.source.strip()
            or not self.version.strip()
            or self.distribution_type != "ordinary_stock"
        ):
            raise ValueError("Invalid ordinary dividend tax rule")


@dataclass(frozen=True)
class DividendTaxQualification:
    tax_residence: str
    beneficial_owner: bool
    treaty_documents_valid: bool
    effective_from: date
    effective_to: date
    known_at: datetime
    source: str
    non_us_ca_tax_resident: bool = False
    individual: bool = False

    def __post_init__(self):
        aware(self.known_at)
        if (
            type(self.beneficial_owner) is not bool
            or type(self.treaty_documents_valid) is not bool
            or type(self.non_us_ca_tax_resident) is not bool
            or type(self.individual) is not bool
            or type(self.effective_from) is not date
            or type(self.effective_to) is not date
            or self.effective_from >= self.effective_to
            or not self.tax_residence.strip()
            or not self.source.strip()
        ):
            raise ValueError("Invalid dividend treaty qualification scenario")


def evidence(value) -> dict:
    return {
        key: item.isoformat() if isinstance(item, (date, datetime)) else item
        for key, item in asdict(value).items()
    }


def resolve_dividend_tax_rate(
    config: "ConstraintConfig", event: "CorporateAction", asof: datetime
) -> tuple[float, dict]:
    """Read-only selection before economic mutation. Intervals are [from, to)."""
    aware(asof)
    if config.dividend_tax_profile == "legacy":
        return (
            event.withholding_rate
            if event.withholding_rate is not None
            else config.dividend_withholding_rate,
            {"profile": "legacy", "basis": config.dividend_tax_scenario},
        )
    if (
        event.currency != "USD"
        or event.terms != "ordinary"
        or event.distribution_type != "ordinary_stock"
        or not event.tax_source_country
        or not event.tax_source
        or event.tax_source_known_at is None
        or event.tax_source_known_at > asof
    ):
        raise ValueError("Dividend tax source/classification unavailable or unsupported")
    day = event.effective_date
    candidates = [
        rule
        for rule in config.dividend_tax_rules
        if rule.source_country == event.tax_source_country
        and rule.effective_from <= day < rule.effective_to
        and rule.known_at <= asof
        and rule.verified_at <= asof
    ]
    # Multiple visible rules are ambiguous, including equal rates with different sources.
    if len(candidates) > 1:
        raise ValueError("Conflicting dividend tax rules")
    rule = candidates[0] if candidates else None
    details = {
        "profile": config.dividend_tax_profile,
        "source_country": event.tax_source_country,
        "classification": event.distribution_type,
        "tax_source": event.tax_source,
        "tax_source_known_at": event.tax_source_known_at.isoformat(),
        "rule": evidence(rule) if rule else None,
        "capital_gains_tax_mode": config.capital_gains_tax_mode,
    }
    if event.withholding_rate is not None:
        if (
            not event.withholding_source
            or event.withholding_known_at is None
            or event.withholding_known_at > asof
        ):
            raise ValueError("Account withholding requires visible account/event evidence")
        conflict = rule is not None and not math.isclose(event.withholding_rate, rule.rate)
        if conflict and config.dividend_tax_conflict_policy == "reject":
            raise ValueError("Account withholding conflicts with scenario; reconcile")
        details.update(
            basis="source_account_withholding",
            account_source=event.withholding_source,
            account_known_at=event.withholding_known_at.isoformat(),
            scenario_rate_conflict=conflict,
        )
        return event.withholding_rate, details
    if rule is None:
        raise ValueError("Unknown dividend tax country or historical rate; explicit rule required")
    if config.dividend_tax_profile == "cn_mainland_individual_treaty":
        qualification = config.dividend_tax_qualification
        if (
            qualification is None
            or qualification.tax_residence != "CN-mainland"
            or not qualification.beneficial_owner
            or not qualification.treaty_documents_valid
            or not qualification.non_us_ca_tax_resident
            or not qualification.individual
            or qualification.known_at > asof
            or not qualification.effective_from <= day < qualification.effective_to
        ):
            raise ValueError("Dividend treaty eligibility unconfirmed/expired")
        details["qualification"] = evidence(qualification)
    details["basis"] = "versioned_source_country_rule"
    return rule.rate, details
