# Titan v1 documentation (archived)

These files describe **Titan v1**, the bot that titan2 replaced in v2.0.0. Nothing here describes how titan2 works:
for that, read [TITAN_INSTRUCTIONS.md](../../TITAN_INSTRUCTIONS.md) and the [RUNBOOK](../RUNBOOK.md).

| File | What it is |
|---|---|
| [TECHNICAL_DOCUMENTATION.md](TECHNICAL_DOCUMENTATION.md) | v1's technical reference: data pipeline, modules, scoring, risk, order execution, configuration |
| [MISSED_TRADE_DIAGNOSTIC_PLAYBOOK.md](MISSED_TRADE_DIAGNOSTIC_PLAYBOOK.md) | v1's procedure for "why did the bot miss this trade?" |
| [TITAN_INSTRUCTIONS_v1.md](TITAN_INSTRUCTIONS_v1.md) | v1's instructions file (v5.2, 2026-03-09) as it stood before the v2 rewrite |

They are kept for reading old research: the trade ledgers, daily summaries and dumps that v1 produced still use v1's
strategy names, exit types and fields. The first two were moved here unchanged with `git mv`; the third is a copy of
the old `TITAN_INSTRUCTIONS.md`.

**The v1 code** leaves the tree in the v2.0.0 deletion commit. The git tag `v1-final`, on commit `b4694f3` (the
2026-07-14 EC2 production snapshot), keeps every v1 file. The `_*.py` research scripts stay in the tree unchanged;
the ones that import v1 modules run from a v1 worktree:

```
git worktree add ../titan_v1 v1-final
```

The paths and commands inside these documents (for example `python autonomous_trader.py`,
`systemctl restart titan-bot`, port 5000) refer to v1 and no longer apply. On the server, v1 is masked and archived
as `/home/ubuntu/titan/v1_frozen_<date>` (RUNBOOK, Stage 0); it never trades again.
