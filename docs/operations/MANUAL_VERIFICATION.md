# Manual paper verification

Start the local launcher with temporary all-tier read access:

```bash
python3 scripts/local_paper.py --unlock-tiers
```

The flag only grants full prediction reads when paper mode is also active.
The UI shows a paper-verification banner. It does not rewrite account tiers,
relax quality gates, authorize admin operations or enable real stakes. Restart
without the flag to restore normal subscription access.

## Baseline

Record publication time, model version, effective settings, source/cache ages,
fixture coverage, model-ready matches, priced matches and qualifying headlines.
An empty value list can be correct. Fixture discovery and the curated pick count
are different; the default feed has no 50-pick ceiling and uses one headline
per qualifying match. Upcoming views must follow kickoff order.

Use `python3 scripts/local_paper.py --diagnose` and the console's **Production
testing** page to distinguish bootstrap, missing history, provider failures,
stale quotes and no eligible selections. Browser refreshes must not cause new
provider requests. Existing predictions and grades must survive a restart.

## Grade exact contracts

Keep frozen snapshots and manual notes in ignored `data/manual-grading/`.
Preserve fixture identity, market, selection, line, regulation period, observed
quote/time, model probability and original journal/publication identity.

After the match, record the confirmed regulation final score, source and
individual contract grade: `WIN`, `HALF_WIN`, `VOID`, `HALF_LOSS` or `LOSS`.
Corner markets also require actual final corner counts. Postponed or unconfirmed
results remain pending. Several markets on a match are correlated observations.

Keep manual grades separate and compare with the automated Ledger. The legacy
match-wide manual-settlement action applies one grade to every market on that
fixture; do not use it to grade different contracts. Do not replace original
probabilities or prices with later model values. Preserve frozen sheets when
restarting or cleaning local build artifacts.

## Access and failure cases

Verify Free, Telegram-verified Free, Tier 1, Tier 2 and Tier 3 against
[the access ladder](../product/PICK_FEED.md). A client tier parameter cannot
upgrade access. Qualifying alternatives belong to Tier 3 in normal operation;
rejected low-quality candidates are hidden at every tier.

Also check price expiry, source outages, moved/postponed fixtures, a quiet day,
missing results and recovery after restart. A healthy web process is not proof
of a fresh publication or executable price. Record the exact timestamp, route,
fixture/contract and expected versus actual behavior when reporting an anomaly;
exclude keys, cookies and private account data.

Manual grading tests data integrity and settlement. Model quality requires a
larger chronological sample; deployment and a few results do not certify profit.
