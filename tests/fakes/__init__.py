"""The titan2 test harness (spec §16.1): FakeKite, ScriptedMarket, FaultInjector, FaultyPaperExchange, the
ScenarioRunner, the dual-run trace and the chaos invariant checker.

Imported as the top-level package `fakes` (pyproject puts tests/ on sys.path): a `tests` package installed in
site-packages would shadow `tests.fakes`.
"""
