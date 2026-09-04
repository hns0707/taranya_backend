"""
Metal and MetalMasterRule helpers: base purity lookup and derived rate calculation.
Branch override → master fallback for purity rules.
"""
from decimal import Decimal

from shared.models import MetalMasterRule, MetalBranchRule


def get_base_purity(metal_id, branch_id=None):
    """
    Return the base purity rule (is_base=True) for the metal.
    If branch_id given: check MetalBranchRule (is_current=True) first, else MetalMasterRule.
    Raises MetalMasterRule.DoesNotExist (or MetalBranchRule) if no base rule exists.
    """
    if branch_id:
        branch_rule = MetalBranchRule.objects.filter(
            branch_id=branch_id,
            metal_id=metal_id,
            is_base=True,
            is_current=True,
        ).first()
        if branch_rule:
            return branch_rule
    return MetalMasterRule.objects.get(metal_id=metal_id, is_base=True)


def get_purity_rule(metal_id, purity_name=None, branch_id=None):
    """
    Return purity rule for metal (and optional purity_name).
    If branch_id: MetalBranchRule (is_current=True) first, else MetalMasterRule.
    """
    if branch_id:
        qs = MetalBranchRule.objects.filter(branch_id=branch_id, metal_id=metal_id, is_current=True)
        if purity_name:
            qs = qs.filter(purity_name=purity_name)
        rule = qs.first()
        if rule:
            return rule
    qs = MetalMasterRule.objects.filter(metal_id=metal_id)
    if purity_name:
        qs = qs.filter(purity_name=purity_name)
    return qs.first()


def get_rules_for_metal(metal_id, branch_id=None):
    """
    Return list of purity rules for metal. Branch override (is_current=True) else master.
    Each rule has: id, metal_id (for branch rules metal_id from FK), purity_name, purity_percentage, description, type, is_base.
    """
    if branch_id:
        rules = list(
            MetalBranchRule.objects.filter(
                branch_id=branch_id,
                metal_id=metal_id,
                is_current=True,
            ).order_by("id")
        )
        if rules:
            return rules
    return list(MetalMasterRule.objects.filter(metal_id=metal_id).order_by("id"))


def calculate_derived_rate(base_rate, base_pct, target_pct):
    """
    Compute derived rate from base rate and purity percentages.
    Formula: Derived Rate = base_rate * (target_pct / base_pct)
    Rounded to 2 decimal places.
    """
    if base_pct is None or base_pct == 0:
        raise ValueError("base_pct must be non-zero")
    base_rate = Decimal(str(base_rate))
    base_pct = Decimal(str(base_pct))
    target_pct = Decimal(str(target_pct))
    return round(base_rate * (target_pct / base_pct), 2)
