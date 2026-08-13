---
type: note
title: Item barcode format
status: stable
tags:
- catalogue
- equipment
---
# Item barcode format

An item barcode is 14 digits. The first digit is always 3 and identifies stock
owned by this system. Digits two and three identify the owning branch. Digits
four to thirteen are the sequence, assigned from a per-branch block. The
fourteenth digit is a modulo-10 check digit.

A barcode is bound to the physical piece, not to the catalogue record. Rebinding
a volume keeps its barcode; splitting a bound run into two pieces means the
second piece is issued a new barcode and a new item record under the same
control number.

Borrower cards use a 13-digit barcode beginning with 2, which is how a scanner
at a staffed desk can reject a card presented as an item without a lookup.

Barcodes are never reused. A withdrawn barcode is retired with its item record,
so a scan of a retired barcode resolves to the withdrawal date rather than to
nothing at all.
