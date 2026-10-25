# Patterns: co-occurrence between daily series (experimental)

```bash
.venv\Scripts\python.exe -m lifecrm patterns                 # the last 90 days, the default family
.venv\Scripts\python.exe -m lifecrm patterns --days 180 --all  # every pair tested, and why it was not reported
.venv\Scripts\python.exe -m lifecrm patterns --json
```

```
Patterns (EXPERIMENTAL) · 2026-07-24 to 2026-10-21 · family of 273 tests over 14 series
  sleep (sensor) tends to go with lower resting heart rate the same day in this window
  (rho -0.71 over 42 paired days, q 0.000; resting heart rate averaged 63.2 on the
  lowest-sleep days and 58.9 on the highest; co-occurrence, not cause) · shift check p 0.012
  workouts tends to go with higher steps the same day in this window (rho +0.58 ...)
A health series here is a comparison with your own record, not medical advice; a pattern
is co-occurrence, never cause.
```

`lifecrm patterns` asks one question of the daily series
([assistant.md](assistant.md), "Coverage"): which pairs go together, the same
day or the next day, more than chance would make them? It is statistics
over your own records and nothing else — no model — and it is
**experimental**: off everywhere by default (the nudges pack, the review
pack, `/morning`) until the validation below has been run at full strength
and recorded here. `[assistant] patterns = "on"` turns it on in those
places; the command itself always runs.

## What a reported pair means, and what it does not

In this window, high days of one series tended to be high (or low) days of
the other. That is co-occurrence. It is never cause: a trip, a sick week or
a launch moves sleep, steps, mood and spending together, and a lag of one
day says nothing about direction beyond "the next day". A health series is
a comparison with your own record, not medical advice. The wording of every
pattern says so, and `/morning` is told to keep that register.

## The method

Everything runs on **calendar days**. A lag of one pairs day d with day
d + 1 and exists only when both are known; a gap of a week is a gap, not a
neighbour. For every pair of series and each lag (0, and 1 in both
directions):

1. the days both series know are kept; each side is **linearly detrended**
   on the day index, because a shared slow trend is not a day-to-day
   association;
2. **Spearman's rho** (Pearson on average ranks, ties sharing a rank) on the
   residuals;
3. a two-sided **p-value from Fisher's z** with the 1.06 small-sample
   factor, on an **effective sample size** corrected for serial correlation
   (Bretherton et al. 1999: n_eff = n (1 - r1 r2) / (1 + r1 r2), with r1 and
   r2 the lag-1 autocorrelations of the two residual series over
   consecutive calendar days). Ninety dependent days are not ninety
   independent ones. The correction is taken **conservatively**: each
   autocorrelation estimate is lifted by one standard error (1/√n) and a
   negative one counts as none. The first calibration showed why: a sample
   autocorrelation of ninety days has a standard error near 0.1, and when it
   came out low a borderline pair was handed a p-value it had not earned —
   false discoveries ran at twice the target on moderately autocorrelated
   null families until the estimate was lifted;
4. **Benjamini-Hochberg** over the whole family — every pair and lag tested,
   printed as the family size — at q ≤ 0.10; a pair is reported when it
   clears that, an effect floor (|rho| ≥ 0.4) and a sample floor (n ≥ 30
   paired days);
5. for reported pairs only, a **circular day-shift check**: one series moved
   by every whole-day shift of 7 or more days against the other, which keeps
   each series' own autocorrelation; the share of shifts whose |rho| reaches
   the observed one is `shift_p`. It is a check, not the inference: a
   shuffle-based p has a floor of 1/(shifts + 1), and no Benjamini-Hochberg
   threshold over a family this size can rest on it.

The default family is the registry's metric-like series (mood, energy,
sleep quality, sleep from the journal and from the sensor, steps, resting
heart rate, weight, exercise minutes, workouts, spending, people in touch).
Journal categories never join (positives only); `--keys` or `[assistant]
pattern_series` add others.

## Validation

`tests/test_patterns_validation.py` measures the method on simulated
families of 14 series over 90 days. The **false discovery rate** is the
expected false discovery proportion over repetitions (false discoveries /
discoveries, 0 when nothing is discovered); in an all-null family it equals
the probability of reporting anything. Each suite runs R seeded repetitions
and asserts the FDR estimate is at or under q and the Wilson 95 % upper
bound of P(any false) under q + 0.05.

The criterion: Benjamini-Hochberg controls the FDR at or below q, and on an
all-null family it sits near q itself, so an estimate above q is expected
about half the time and proves nothing. The gate fails when the estimate is
significantly above q (the Wilson interval's lower bound over q) or grossly
above it (the point estimate over q + 0.05 at R = 200).

Suites: independent series; AR(1) with phi 0.4 and 0.8; a shared linear
trend; heavy ties (counts 0-3); 30 % missing days at random and in four-day
runs; power on a planted rho 0.5 (same day) and 0.6 (next day); twelve
overlapping 90-day windows over a simulated year (the planted pair must be
found in at least 10, no false pair in more than one).

Validation is separate from tuning. The development seed set shaped the
floors; the **reserved** seed set is run once after tuning stops, with
`LIFECRM_PATTERN_GATE=1` (R = 200, every suite; the regular test run uses
R = 40 on three suites). Any later change to the family, the floors, the
detrending or the null gets a fresh reserved set, and the run is recorded
here. Passing is evidence about these scenarios, not a guarantee: real
series have structure no simulation has.

### Runs

| date | seeds | R | result |
|---|---|---|---|
| 2026-10-24 | dev, before the conservative n_eff | 40 | null FDR: independent 0.00, phi 0.4 0.22, phi 0.5 0.10, phi 0.8 0.03, trend 0.00, ties 0.05, missing 0.03 — the phi 0.4 figure is what led to lifting the autocorrelation estimate by one standard error |
| 2026-10-24 | dev, conservative n_eff | 40-80 | null FDR: independent 0.03, phi 0.4 0.03, phi 0.5 0.03, phi 0.8 0.00, missing in runs 0.05; power 0.80 at rho 0.5 over 90 days, 1.00 at rho 0.6 lag 1 |
| | reserved | 200 | not yet run — patterns stay off by default |

## In the fronts

With `[assistant] patterns = "on"`: `lifecrm nudges --json` carries a
`patterns` block whose items are `P#` ids in the same index (so a brief can
cite them and `--check --pack` holds them to the saved pack); the review
pack adds a `patterns` section (lens-filtered: a series belongs to the
lenses in the registry); `/morning` adds an optional "Patterns (experimental)"
paragraph. Off, none of that exists and the briefs say nothing about
patterns.
