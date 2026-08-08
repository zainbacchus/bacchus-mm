# Crypto.com predictions venue census (2026-08-08)

Question (owner): should the bot expand to Crypto.com's prediction markets?
They advertise zero maker fees (takers $0.01-0.0175/contract scaled to
price), which is the economic precondition our Kalshi strategy runs on.

Method: their public data API (data-api.crypto.com, unauthenticated). The
full route surface per its own OpenAPI spec is five read endpoints: events
list/search/detail, contracts-of-event, and a per-contract price route. No
orderbook depth, no trades tape, no volume anywhere - it is a display API.
Census below is from the events payload (yes/no are the two ask prices;
spread = yes + no - 1). 488 events, 6,838 contracts.

| group | contracts | two-sided | median spread |
|---|---|---|---|
| crypto (BTC, ETH, 16 assets; hourly strike ladders + 5-min sprints) | 813 | 8% | 13.0c |
| financials (GOLD, CRUDE, indexes, FX) | 27 | 59% | 5.0c |
| sports/other | 5,998 | 58% | 5.0c |

## Verdict: NO adapter now

The bull case was "young venue, retail flow, wide uncompeted spreads." The
census kills it on the first fact: the crypto boards are not wide, they are
EMPTY - 92% of crypto contracts have no two-sided market at all, and the
65 that do sit at a 13c median spread because nobody quotes, not because
makers are being paid. There is also no public volume or trade data with
which to detect taker flow, and no visible public trading API for the event
contracts (execution appears to live behind their institutional/derivatives
stack). Kalshi 15M for comparison: 0.1-1c spreads, full public book/trade
/candle data, a documented trading API with post-only, and our own 6,700
settled contracts per day of measured flow.

Revisit trigger: re-run this census (~2 minutes, no credentials) if their
crypto boards ever show majority two-sidedness or spreads inside ~5c. The
concrete catalyst to watch: WSJ reported 2026-07 that Robinhood is in talks
to carry Crypto.com's prediction markets - Robinhood distribution pouring
retail flow onto these empty boards is exactly the event that would flip
this verdict. Ecosystem context (verified 2026-08-08): Coinbase's
prediction markets route to Kalshi; Robinhood routes across Kalshi,
ForecastEx, and its own Rothera DCM (a Susquehanna JV), and Robinhood's
BTC 15-minute markets carry Kalshi's exact BRTI contract spec - retail
flow from the big apps aggregates onto the books this bot already quotes,
and Kalshi is the only one of these venues with an open maker API. A
9-minute board-wide flow sample (this file's method, 2026-08-08): 725
contract-price changes, ~all sports; crypto boards moved twice. Compliance
note: owner reviewed and cleared participation for their situation
(2026-08-08) before this census was run.
