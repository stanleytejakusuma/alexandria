---
type: note
title: Catalogue record fields
status: stable
tags:
- catalogue
---
# Catalogue record fields

Every catalogue record carries a fixed core, and any field outside the core is
local and not exchanged with other systems.

Core fields are: control number, title proper, statement of responsibility,
edition, publication place, publisher, date of publication, extent, series,
subject headings, and one or more classification numbers.

The control number is assigned once and never reused, even after a record is
deleted. A deleted record leaves a tombstone carrying only the control number
and the deletion date, which is how a system that harvested the record learns
it has gone.

Local fields are prefixed with a lowercase x. Two are in general use: xshelf,
recording the human-readable shelf phrase printed on the spine label, and
xnote, a free-text staff note that is never displayed to the public.

Dates of publication are recorded as they appear on the item, with an inferred
date in square brackets where the item carries none.
