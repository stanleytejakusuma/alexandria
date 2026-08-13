---
type: note
title: Holds queue and shelf expiry
status: stable
tags:
- operations
- circulation
---
# Holds queue and shelf expiry

A hold places the borrower in a first-come queue against every copy of a title.
The queue is per title, never per copy, so a borrower is served by whichever
copy is checked in first anywhere in the system.

When a copy is trapped for a hold it is routed to the pickup branch and the
borrower is notified. The item then waits on the hold shelf for 7 open days.
If it is not collected the hold expires, the item passes to the next borrower
in the queue, and an uncollected-hold count is incremented on the account.

Three uncollected holds within a rolling 12 months suspends the borrower's
ability to place new holds for 30 days. The suspension is lifted early by a
supervisor only where the notification itself failed.

A borrower may suspend their own place in a queue for up to 180 days without
losing position, which is the correct action before a long absence.
