"""PantryRelay — an agent that keeps rescued food moving.

Public surface is deliberately small: build the agents, or run the deterministic
routing policy the agents follow.
"""

from .gate import CoordinatorGate
from .models import DonationOffer, Escalation, FoodItem, MatchPlan, Pantry
from .routing import Outcome, route_offer

__all__ = [
    "CoordinatorGate",
    "DonationOffer",
    "Escalation",
    "FoodItem",
    "MatchPlan",
    "Outcome",
    "Pantry",
    "route_offer",
]

__version__ = "0.1.0"
