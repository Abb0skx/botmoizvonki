# Repeated requests and supplier prices

Matching collapses identical active requests in the same group into one product
candidate. Equality is exact after Unicode/case/whitespace normalization, keeping
memory, color, connectivity, condition and quantity. It never merges groups.

Bare-price and model matching choose the latest eligible original request; manual
assignments and explicit reply/forward targets retain the original IDs and their
individual seven-minute expiry. Prices anchored to another active duplicate are
available to price-distance matching. Inferred prices never become anchors and
remain outside min/max coloring. A repost does not reset the product introduction
time used by price-far inference while earlier equivalent requests remain active.

Regression example: Amazfit request with a 330 USD anchor, then two identical
iPhone requests followed by 1470 and 1475. These now remain two product candidates,
not three. The later prices are attached to iPhone as hypotheses, not certainty.

The Finance mirror in market_quotes.py / market_private_quotes.py must stay in
sync. The report groups presentation separately and preserves raw demand counts.
