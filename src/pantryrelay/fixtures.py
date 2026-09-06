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

#: In the order they land on a Tuesday morning.
MORNING = [BAKERY, PRODUCE, DRY_GOODS, BEVERAGE, DAIRY, FROZEN]

SAMPLE_FILES = [
    "01_email_bakery.txt",
    "02_sms_produce.txt",
    "03_email_drygoods.txt",
    "04_sms_beverage.txt",
    "05_voicemail_dairy.txt",
    "06_voicemail_frozen.txt",
]
