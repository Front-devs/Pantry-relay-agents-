"""Pre-parsed offers matching the files in samples/.

These are what the reader agent should produce from the raw sources. They let
the demo and the test suite run end-to-end with no AWS credentials, and they
double as the expected output when you do want to check the reader's work.
"""

from __future__ import annotations

from .models import DonationOffer, FoodItem

BAKERY = DonationOffer(
    donor_name="Brightline Bakeries",
    donor_contact="dispatch@brightlinebakeries.com",
    channel="email",
    pickup_location="Halsey depot loading dock",
    items=[
        FoodItem(
            description="Sourdough boules and seed loaves, 18 grey bins",
            category="bakery",
            quantity_lbs=396.0,
            storage="ambient",
            hours_until_unusable=72.0,
            notes="Estimated at 22 lbs/bin from the donor's own figure.",
        ),
        FoodItem(
            description="Sandwich rolls",
            category="bakery",
            quantity_lbs=40.0,
            storage="ambient",
            hours_until_unusable=72.0,
        ),
    ],
    extraction_confidence=0.93,
    ambiguities=["Depot closes 'till 6 most days' — exact cutoff not stated."],
)

PRODUCE = DonationOffer(
    donor_name="Nguyen Family Farm",
    donor_contact="+1-555-0281",
    channel="sms",
    pickup_location="Route 9 by the old feed store",
    items=[
        FoodItem(
            description="Chard and romaine, three waxed boxes",
            category="produce",
            quantity_lbs=130.0,
            storage="refrigerated",
            hours_until_unusable=48.0,
            notes="Donor said '130lb ish, maybe a little more'. Held in cooler since Sunday.",
        )
    ],
    extraction_confidence=0.84,
    ambiguities=["Weight given as an approximation.", "Photo suggests possible fourth box."],
)

DRY_GOODS = DonationOffer(
    donor_name="Meridian Logistics",
    donor_contact="g.okafor@meridianlogistics.com",
    channel="email",
    pickup_location="Marcy Avenue cross-dock",
    items=[
        FoodItem(
            description="Mixed pallet: rice, lentils, canned tomatoes, pasta",
            category="dry_goods",
            quantity_lbs=620.0,
            storage="ambient",
            hours_until_unusable=720.0,
            notes="Carton damage only; inner packaging intact and QA-cleared.",
        )
    ],
    extraction_confidence=0.96,
)

BEVERAGE = DonationOffer(
    donor_name="Corner Market",
    donor_contact="+1-555-0344",
    channel="sms",
    pickup_location="Grace Ave",
    items=[
        FoodItem(
            description="Old-label bottled water and juice boxes",
            category="beverage",
            quantity_lbs=300.0,
            storage="ambient",
            hours_until_unusable=720.0,
            notes="Label change only; product in date.",
        )
    ],
    extraction_confidence=0.95,
)

DAIRY = DonationOffer(
    donor_name="River Road Creamery",
    donor_contact="+1-555-0416",
    channel="voicemail",
    pickup_location="River Road",
    items=[
        FoodItem(
            description="Yogurt quart tubs and cottage cheese",
            category="dairy",
            quantity_lbs=220.0,
            storage="refrigerated",
            hours_until_unusable=72.0,
            notes="Moved as a precaution — chiller compressor fault, tech due Thursday.",
        )
    ],
    extraction_confidence=0.88,
    ambiguities=["Weight given as 'two hundred, two twenty maybe'."],
)

FROZEN = DonationOffer(
    donor_name="Cold Storage (name inaudible)",
    donor_contact="+1-555-0502",
    channel="voicemail",
    pickup_location="Unclear — caller's facility name was inaudible",
    items=[
        FoodItem(
            description="Frozen chicken and ground turkey, 11 large cases",
            category="meat",
            quantity_lbs=900.0,
            storage="frozen",
            hours_until_unusable=8760.0,
            notes="Donor will not split the lot — all or nothing.",
        )
    ],
    extraction_confidence=0.82,
    ambiguities=["Facility name inaudible in transcript.", "Weight given as 'give or take'."],
)

#: A voicemail the transcriber half-lost. The reader's job here is to be honest
#: that it could not resolve the weight, the storage class or even the donor's
#: name, rather than to pick the likelier number and move on. Confidence sits
#: below the gate's 0.75 bar on purpose: this is what an unreadable source
#: looks like, and no amount of routing skill should paper over it.
CATERING = DonationOffer(
    donor_name="Harborview Catering",
    donor_contact="+1-555-0631",
    channel="voicemail",
    pickup_location="Unclear — caller did not finish the address",
    items=[
        FoodItem(
            description="Trays of cooked chicken and rice, plus salad",
            category="prepared",
            quantity_lbs=60.0,
            storage="refrigerated",
            hours_until_unusable=24.0,
            notes=(
                "Weight unresolved: caller said 'sixty pounds? Maybe a hundred "
                "and sixty'. Lower figure taken; the higher one is not ruled out."
            ),
        )
    ],
    extraction_confidence=0.41,
    ambiguities=[
        "Caller's name inaudible.",
        "Weight given as '60, maybe 160' — a factor of nearly three.",
        "Some stock in the walk-in since Friday, some not; caller could not say which.",
        "Message truncated at the 45s limit before the callback window was given.",
    ],
)

#: A store closing today. Nothing here is ambiguous and the load fits — the only
#: reason this stops is that the donor needs an answer inside the day, which
#: commits volunteer time the agent cannot see. That is the whole point of the
#: same-day check: a hold that has nothing to do with the food being wrong.
GROCERY_CLOSING = DonationOffer(
    donor_name="Westbrook Grocer",
    donor_contact="dmoreau@westbrookgrocer.com",
    channel="email",
    pickup_location="Prescott St store, loading bay open until 18:00",
    items=[
        FoodItem(
            description="Rice, dried beans, pasta, canned vegetables, cartons",
            category="dry_goods",
            quantity_lbs=180.0,
            storage="ambient",
            hours_until_unusable=720.0,
            notes="Counted off the store's own count sheet; sealed, in date, palletised.",
        )
    ],
    needs_same_day_answer=True,
    extraction_confidence=0.94,
    ambiguities=["Hard 14:00 cutoff for a yes or no; bay closes 18:00."],
)

#: In the order they land on a Tuesday morning.
MORNING = [BAKERY, PRODUCE, DRY_GOODS, BEVERAGE, DAIRY, FROZEN, CATERING, GROCERY_CLOSING]

SAMPLE_FILES = [
    "01_email_bakery.txt",
    "02_sms_produce.txt",
    "03_email_drygoods.txt",
    "04_sms_beverage.txt",
    "05_voicemail_dairy.txt",
    "06_voicemail_frozen.txt",
    "07_voicemail_catering.txt",
    "08_email_grocery_closing.txt",
]
