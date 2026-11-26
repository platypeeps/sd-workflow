fix(writing): a fresh review keeps the resolution ledger

A fix that changes index.md after a review leaves the gate refusing
the old index_sha256, so the apply session must run the review again.
That run replaced adversarial.md whole and erased every decision the
resolution ledger held. A fresh run now writes a new stamp, layout
proof and report, and carries the prior ledger forward after it.

Closes: sd:10719
