"""Market data (spec §3.2, §10.3): the shared QuoteCache, the KiteTicker feed and the 5-minute bars.

- quotes.QuoteCache: latest depth snapshot per symbol; fresh() never returns a stale quote.
- feed.Feed: the kite-ws side (ticks -> QuoteCache / signal_q / dirty set, order updates, subscriptions through
  reactor.callFromThread). twisted is imported lazily, so every module here imports on Windows.
- bars.Bars: today's LIVE bars (signals thread), the previous session's HIST bars, and the 15:36 bar check.

Import the submodules directly; this package imports nothing so that core code can use market.quotes alone.
"""
