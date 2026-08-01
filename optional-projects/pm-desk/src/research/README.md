# Research harnesses (`src/research/`)

Paper-only research utilities. Nothing here can trade, sign, place an order, or
touch a wallet — that boundary is enforced structurally by the package
[`guard` test](../../tests/guard.test.ts), which scans all of `src/` for trading,
signing, and wallet symbols.

## CPI nowcast → rounded-bucket calibration (`cpi_nowcast/`)

**What it is.** A read-only research harness that turns historical nowcast error
into the probability that a BLS CPI YoY print lands in a **one-decimal contract
bucket**, then compares that probability to a Polymarket mid. It is an *edge
detector*, never an order.

**Why.** A Polymarket CPI contract pays on the *rounded, displayed* print (one
decimal), not the raw index. Trading the point nowcast ignores the residual
distribution. This harness joins a historical nowcast series to actual BLS prints,
learns the residual distribution (`print - nowcast`), and bootstrap-shifts it by a
live nowcast to get `P(bucket)` after rounding.

**Pipeline.**

```
nowcasts × prints  →  residuals (print - nowcast, no-look-ahead)
                  →  empirical residual distribution
                  →  P(each 0.1% bucket) after one-decimal rounding
                  →  edge vs Polymarket mid, gated by half-spread + model haircut
                  →  fail closed if sample_size < min_n
```

**Math notes.**

- **Rounding** (`round.ts`): half-up at the second decimal, matching BLS-style
  display. `3.35 → 3.4`, `3.34 → 3.3`. A tiny epsilon (1e-9, scaled) protects
  against binary-float mis-rounding without dragging a genuinely-below-half value
  over the line.
- **Residuals** (`residuals.ts`): pairs by CPI reference month; a nowcast's
  vintage must strictly precede the print's release (no look-ahead). The latest
  eligible vintage wins. Violations are dropped, never silently used.
- **Calibration** (`calibrate.ts`): non-parametric empirical bootstrap —
  `P(bucket B | L) = #{ i : round1(L + r_i) == B } / N`. A Gaussian-with-RMSE
  approximation is intentionally *not* used; the bootstrap inherits the actual
  skew of historical nowcast error.
- **Edge** (`compare.ts`): `investigate_long` when `p - mid > halfSpread +
  modelHaircut`; `investigate_short` when `mid - p` clears the same threshold;
  else `no_trade`. `fail_closed` overrides everything when the sample is too
  small. The label is research only and is never an order.

**Provenance — read this before citing a number.**

A fixture run and a live run emit the same shape of result, and on 2026-07-31 a
reopen ran the fixture path and reported `p_bucket ≈ 0.75` against a `0.425` mid.
The live series said `0.38` — no trade. So every result carries:

| Field | Meaning |
|-------|---------|
| `series_provenance` | `fixture` \| `live` \| `mixed` — where the two series came from |
| `source_urls` | every public URL that contributed a row (empty for fixtures) |
| `entry_eligible` | may this result be cited to justify monitors or a paper entry? |
| `entry_block_reason` | one line saying why not, when it is false |
| `paired_n` | no-look-ahead pairs actually joined (alias of `sample_size`) |
| `data_plane_attempts` | every source-ladder rung tried, successes and failures |

`entry_eligible` is false whenever provenance is `fixture`, whenever it is
`mixed` without an explicit human `--allow-mixed-entry`, and whenever the run
failed closed. `decision` is untouched by this — a fixture may still print
`investigate_long`, it just is not allowed to mean anything. **A plan that cites
a result with `entry_eligible: false` as its entry edge is invalid.**

**CLI.**

```bash
npx tsx src/cli/pm-desk.ts research cpi-calibrate \
  --nowcasts fixtures/cpi_nowcast/nowcasts.csv \
  --prints  fixtures/cpi_nowcast/bls_yoy.csv \
  --as-of 2026-07-31 --live-nowcast 3.42 --bucket 3.4 --mid 0.43 --json
```

Live fetch (`--fetch-cleveland`, `--fetch-bls`) is opt-in via
`PM_DESK_LIVE_CPI=1`, best-effort, and never requires credentials. It is not
exercised by the hermetic tests; a public-endpoint redesign fails loudly rather
than emitting plausible-but-wrong rows.

**Tests.** `tests/cpi_nowcast.test.ts` covers rounding boundaries, the no-look-
ahead join, the latest-eligible-vintage rule, bootstrap probabilities summing to
~1, `fail_closed` on tiny `N`, edge thresholds, the CSV loaders, and the hermetic
CLI path.
