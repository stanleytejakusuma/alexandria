---
type: note
title: Self-checkout kiosk troubleshooting
status: stable
tags:
- operations
- equipment
---
# Self-checkout kiosk troubleshooting

A kiosk that reads a card but refuses every item is almost always failing to
deactivate the security strip. Take the kiosk out of service, clear the pad, and
check that the desensitiser light is green before returning it to service.

A kiosk that reads nothing at all has usually lost its network route. Power
cycle it once. If it comes back with a clock more than five minutes out, it has
failed to reach the time source and its transactions will post with wrong
timestamps, so leave it out of service and raise a ticket.

Receipt paper is replaced when the coloured stripe appears, never when the roll
runs out mid-transaction, because a mid-transaction change loses the item list.

Kiosks do not accept payment of charges over 25.00 and cannot process an
interlibrary loan return; both are referred to a staffed desk.
