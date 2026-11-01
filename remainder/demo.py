"""A fictional youth soccer league for the demo. Every name and amount is made up."""

TITLE = "Alder Creek Youth Soccer, spring season refund"

NOTICE = """Alder Creek Youth Soccer - spring season update

Dear families,

Hollis Park's fields were closed for four of our ten Saturdays this spring after the April floods. The city has refunded $1,000.00 of our field permit, and on Thursday the board voted to return all of it to you, split in proportion to what each family paid for registration.

Uniforms were ordered and delivered in March, so each family's share is based on what you paid minus the $15.00 uniform fee per player.

The Okafor family already received a full refund when they moved away in March, so they are not included.

Refunds will go back to the PayPal account or card you paid with.

Thanks for your patience,
Dana Whitfield, Treasurer"""

FULL, SIBLING, SCHOLARSHIP = 12000, 10800, 6000

# (family name, fees per player). Order is the order they paid in.
FAMILIES = [
    ("Okafor", [FULL]),
    ("Alvarez", [FULL]),
    ("Brennan", [FULL, SIBLING]),
    ("Chen", [FULL]),
    ("Dimitrov", [SCHOLARSHIP]),
    ("Esposito", [FULL]),
    ("Fischer", [FULL, SIBLING]),
    ("Gupta", [FULL]),
    ("Haddad", [FULL]),
    ("Ito", [SCHOLARSHIP]),
    ("Jensen", [FULL]),
    ("Kowalski", [FULL, SIBLING]),
    ("Larsen", [FULL]),
    ("Mbeki", [FULL]),
    ("Nakamura", [FULL]),
    ("O'Neill", [FULL, SIBLING]),
    ("Petrov", [FULL]),
    ("Quinn", [SCHOLARSHIP]),
    ("Rossi", [FULL]),
    ("Santos", [FULL, SIBLING]),
    ("Tanaka", [FULL]),
    ("Usman", [FULL]),
    ("Vargas", [FULL]),
    ("Whitaker", [FULL]),
]

# Refunded in full before this round (they moved away). Seeded as a real prior refund.
REFUNDED_BEFORE = {"Okafor"}
