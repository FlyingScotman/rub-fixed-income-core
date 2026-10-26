# OFZ risk as RUONIA plus a basis (issue #83)

Status: released in 0.39.0; KBD floaters discounted on RUONIA (issue #90,
[below](#kbd-floaters-discounted-on-ruonia-issue-90)) released in 0.40.0. Decision of the owner of
2026-10-25: the RUONIA risk and the basis risk of an OFZ must be **computed**, not identical.
Research run on hub data of 2026-10-22: [proposals/ofz-curve-risk-factors.md](proposals/ofz-curve-risk-factors.md),
§11 (in Russian). Consumer issue: FlyingScotman/trading-desk-monitor#213.

Module `rub_fixed_income.ofz_basis`; opt-in in `value_portfolio` through
`ValuationPolicy.ofz_basis`. Nothing changes without it.

## What changes

Until now the «RUONIA + basis» view of an OFZ was `factors.basis_view` (variant **P**): a
linear change of coordinates of the KBD's v3 par risk. A RUONIA bucket was assumed to move
the KBD by the same par shift, so RUONIA risk ≡ basis risk ≡ KBD par risk, bucket by bucket.

Now (variant **Z** with the **ZX** tail) both legs are computed from their own curves:

| Factor | Moves | Equals |
| --- | --- | --- |
| `IR\|<ois curve>\|<bucket>` (RUONIA) | the OIS quotes by the bucket hats at their maturities; the OIS curve is rebootstrapped and its annual zero move is added to the KBD | the OIS curve's own quote factors, so OFZ and OIS swaps add up |
| `IR\|<basis id>\|<bucket>` (basis) | the KBD's v3 par instruments by the bucket shift, OIS held | the KBD's v3 par risk (`rub_ofz_kbd_v3`), to the bit |
| `CS` | the bond's own spread to the KBD | unchanged |

## The scheme

The KBD on par factors (`rub_ofz_kbd_v3`, `zero_shock="annual_par"`) under RUONIA bucket
shifts `s` and basis bucket shifts `q` (bp):

    Y'(t) = Y(t) + Σ_n w_n(t)·x_n(q) + [Z_s(t) − Z_0(t)],      DF'(t) = (1 + Y'(t))^(−t)

- `t`: the KBD's curve time (ACT/365F from its curve date); `Y(t) = DF(t)^(−1/t) − 1` the
  KBD's effective annual zero yield;
- `x(q)`: the v3 par overlay (`_par_overlay`), solved on the **base** KBD: node shifts of the
  annual zero (hats on 1Y–20Y+, flat below 1Y) that reprice the eight par instruments at
  their shifted yields;
- `Z_s(t) = DF_s(t)^(−1/t) − 1`: the effective annual zero, on the KBD's curve time, of the
  RUONIA OIS curve rebootstrapped from quotes moved by `s` (`_risk_common._shocked_request`:
  each quote by `Σ_b hat_b(T_quote)·s_b`, the OIS curve's own quote-risk rule).

With RUONIA held (`s = 0`) the shocked curve **is** the v3 view of `q`
(`_risk_common._shock_curve`): basis risk, its Hessian and every basis-only price are the
v3 numbers to the bit. Only the RUONIA leg is new. The basis 1M–6M buckets have no
instrument, as in v3 (zero risk; scenario shifts there must be zero or equal to 1Y).

**Joint moves add in annual zero.** The overlay is solved on the base KBD and the OIS zero
move is added to it. After a joint move the KBD par yields are therefore `base + q` only up
to the par effect of the OIS move. First-order risk does not depend on this convention;
cross gammas (RUONIA × basis) and joint scenarios follow it
(`test_joint_shifts_add_in_annual_zero`).

## The long end (ZX)

The RUONIA OIS quotes stop at 10Y. Beyond its last quote the OIS curve extrapolates
flat-forward from its last segment, so a RUONIA bump of the 7Y or 10Y bucket would move
the long KBD by several bp of opposite signs (research: +3.9 bp at 15Y per bp of the 10Y
bucket), and a 15Y OFZ would show RUONIA 7Y/10Y risk of ±700 RUB/bp per 1 mn against a
basis of ±50 (raw Z). ZX removes this:

- the OIS request used here (`OfzBasisCurve.extended`) gets one **synthetic quote** per v3
  par tenor (`PAR_YEARS`) maturing after its last quote — 15Y and 20Y after a 10Y last
  quote;
- each is `KBD par(m_T) − s_tail`, with `s_tail = KBD par(m_last) − quote_last`
  (`OfzBasisCurve.tail_spread`, decimal);
- `KBD par(m)` is the par yield of an annual-coupon bond on the base KBD from its curve date
  to `m`: coupon dates rolled yearly from the curve date unadjusted, a roll date within
  180 days before `m` dropped (the last period is long), accrual on the KBD's day count
  (ACT/365F), DF from the KBD. At a maturity on a roll date it is the v3 instrument's par
  yield. `m_last` is the last real quote's maturity; `quote_last` its par rate as quoted
  (the spot-starting swap of the OIS convention). So `s_tail` compares a KBD par yield and
  an OIS par quote of the same maturity date; the instruments differ in start (curve date
  vs spot) and coupon conventions, which is the research run's definition;
- `m_T` is where the tenor resolves under the OIS convention (`ois_swap`: spot lag and
  business-day rule on its calendar; a legacy request: `T` years from the curve date). A
  calendar bounded before `m_T` is a `CalendarRangeError`;
- the synthetic quotes are fixed at the base: RUONIA buckets move them by their hats like
  any quote (15Y, 20Y+); basis buckets never rebuild or read the OIS curve.

With ZX, RUONIA ≈ basis on the long end again (research SU26238: 15Y −406.6 against
−417.9; totals within 0.35 %). Beyond the last synthetic quote (20Y) the extended OIS curve
extrapolates flat-forward again; no OFZ is that long today.

When the quotes already reach 20Y no tenor is added: the extended request is the OIS
request itself and every shocked curve is shared with the OIS curve's own risk.

## Shocked OIS curves and the cache

The extended request differs from the plain one whenever a tenor is added, so its shocked
curves are separate `ShockedCurveCache` entries (the same `(request, shifts)` keys, another
request). OIS positions (swaps, futures, bonds discounted on RUONIA) keep the **plain**
request: their numbers, curves and keys are unchanged. Per job:

- first order: 22 extended rebuilds (±bump per bucket) plus the base;
- the full RUONIA Hessian: 220 more (four corners per bucket pair), the shift vectors of
  the bumped Hessian, so the closed form and the bump path share them.

At ~5 ms per MOEX-v2 bootstrap that is ~0.1 s (first order) and ~1.2 s (Hessian) per job,
independent of the book's size. `export` / `seed` persist them like any shocked curve.

## Risk of a bond

`OfzBasisRiskEngine(engine, curve)` takes a `BondRiskEngine` calibrated on the KBD (its
spread is held) and an `OfzBasisCurve`; factors are 11 RUONIA buckets, 11 basis buckets and
the spread.

- **Bump** (`method="bump"`, any bond the engine prices): central differences of
  `price(ruonia, basis, spread)` on the composed curve. Fixed bonds, perpetual tails
  (beyond 30Y the tail reads the composed curve date by date where it is not
  flat-forward, as on a shocked v3 curve), single-curve
  floaters on the KBD (`ZR_YLD_CRV`: every coupon re-projected off the composed curve) and
  floaters projected off the same RUONIA quotes (a RUONIA shift then moves the projection
  too). A floater projected off any other curve is `InvalidTerms`.
- **Closed form** (`method="analytic"`, fixed cash flows): every flow is a function of
  `ln DF'(t) = −t·ln(G(t) + w(t)·x·1e-4 + ΔZ(t; s))`, `G = DF^(−1/t)`, through the
  settlement-forward DF and the Z-spread convention (`_analytic_risk.flow_partials`). The
  basis block is `BondRiskEngine.second_order(method="analytic")` itself (the v3 par
  Jacobian with the instruments' curvature). The RUONIA rows use `∂ΔZ/∂s` and
  `∂²ΔZ/∂s²` of the OIS zero at each flow date and at settlement
  (`OfzBasisCurve.ois_jets`, central differences of the cached shocked OIS curves with the
  same bump; memoised per date, so bonds sharing coupon dates share them). Cross terms:
  `∂²V/∂q∂s = J⁻ᵀ Eᵀ ∂²V/∂x∂s` (the overlay does not depend on `s`); there is no curvature
  term across families.

Accuracy (tests, 15Y / 9.4Y / 2.4Y bonds): RUONIA buckets against an independent
rebuild-compose-reprice 1e-9 relative; closed form against bump 2.6e-6 relative at 1 bp
and 2.6e-8 at 0.1 bp (gradient and Hessian, O(bump²)). A second-order Taylor expansion of a joint RUONIA +
basis + spread move of 4–10 bp leaves a third-order residual (halving the move divides it
by ~8; < 1e-4 of the P&L).

## KBD floaters discounted on RUONIA (issue #90)

The mirrored case: a KBD floater (MOEX `ZR_YLD_CRV`, `FloatingCouponRule(index="kbd")`)
discounted on the RUONIA OIS quotes plus its Z-spread, its coupons projected off the v3 KBD.
Without the opt-in it reports `IR|<ois>|*` (discount) and `IR|<kbd>|*` (projection, v3 par
buckets). `OfzBasisRiskEngine` takes it when the engine's discount curve is the view's OIS
request and its projection curve the view's KBD (`discounted_on == "ois"`); factors as for an
OFZ (11 RUONIA, 11 basis, the spread):

| Factor | Moves | Equals |
| --- | --- | --- |
| `IR\|<ois>\|<bucket>` (RUONIA) | the discount curve: the **plain** OIS request rebootstrapped from quotes moved by the bucket hats; and the projection: the composed KBD `Y + overlay(q) + [Z_s − Z_0]` with `Z` from the **extended** (ZX) request | the bond's own discount bucket plus the RUONIA leg of its projection |
| `IR\|<basis>\|<bucket>` (basis) | the projection only: the KBD's v3 par instruments by the bucket shift, RUONIA held | the bond's `IR\|<kbd>\|*` projection risk without the opt-in, to the bit (the same repricings) |
| `CS` | the bond's spread over the OIS curve | unchanged |

**Two requests, on purpose.** The discount leg uses the plain request, the rule of
`IR|<ois>|*` for every bond and swap on the OIS curve: the floater's RUONIA ladder adds up with
OIS swaps' and its discount part is the bond's own discount risk without the opt-in; the
shocked curves are the same cache entries. The projection's composed KBD uses the extended
request exactly as an OFZ does (`OfzBasisCurve.shocked`): a 7Y-tenor KBD index read up to 7Y
after a fixing reaches 14Y and beyond, past the last 10Y quote, where the plain request would
swing the long KBD by the flat-forward extrapolation. So a RUONIA 15Y / 20Y+ bump does not move
the discount (no plain quote there) and moves the projection through the synthetic quotes.
The two base curves are equal to the bit up to the 9Y node, differ by ~6e-10 in DF on the
last quote's segment (the 10Y swap's payment lag reads past its node, on the 15Y segment of the
extended curve) and diverge beyond 10Y, where only the extended one has quotes. **When the
quotes reach 20Y no tenor is added:** the extended request is the plain one and both legs read
the same shocked OIS curve.

With the projection's RUONIA held (`projection_shifts` zero) the price is the engine's
two-curve price with its projection moved by the basis, so basis risk, the basis × basis and
basis × spread Hessian, and every basis-only scenario are the plain numbers to the bit. Joint
RUONIA + basis moves add in annual zero inside the projection, as for an OFZ. Floating coupons
re-project, so the risk is bumped (first and second order); the closed form is refused.

Synthetic 7Y floater (`tests/test_ofz_basis_kbd_floater.py`: KBD 7Y + 0.50 %, 14 semiannual
coupons from 2026-10-23, spread 250 bp, the OIS quotes of the next section), RUB/bp per 1 mn:

| bucket | plain discount `IR\|<ois>` | plain projection = basis | RUONIA | of which projection |
| --- | ---: | ---: | ---: | ---: |
| 6M | −3.5 | 0 | −6.9 | −3.4 |
| 1Y | 1.7 | −17.1 | −10.6 | −12.3 |
| 3Y | −6.8 | −89.2 | −93.6 | −86.8 |
| 5Y | −13.0 | −203.3 | −205.1 | −192.0 |
| 7Y | −339.4 | 29.4 | −302.0 | 37.4 |
| 10Y | −1.2 | 471.8 | 415.9 | 417.2 |
| 15Y | 0 | 246.8 | 274.1 | 274.1 |
| total | −365.9 | 404.8 | 36.9 | 402.9 |

The projection's RUONIA total is within 0.5 % of the basis total, the bucket split differs
as for OFZ (pillars, the 1M–6M exposure, the long end). Cost per bond after the job's shared
OIS rebuilds: about 0.08 s for the bumped 23 × 23 Hessian (the plain two-curve one: 0.06 s).

## Numbers (synthetic OIS shaped like the KBD of 2026-10-15)

`tests/ofz_basis_data.py`: OIS quotes 1W–10Y = KBD par + a basis (OIS above the KBD at the
short end, 40–60 bp below from 2Y). RUB/bp per 1 mn face, closed form:

| bond | RUONIA total | basis total | Δ total | Σ\|bucket Δ\| / total |
| --- | ---: | ---: | ---: | ---: |
| 14.6Y, 7.1 % | −313.37 | −314.32 | 0.31 % | 12.4 % |
| 9.4Y, 12 % | −390.68 | −391.94 | 0.32 % | 7.5 % |
| 4.9Y, 8.5 % | −264.42 | −265.27 | 0.32 % | 3.9 % |
| 2.4Y, 6.7 % | −165.08 | −165.47 | 0.23 % | 22.1 % |

The 14.6Y bond by bucket (RUONIA / basis): 6M −1.4 / 0, 1Y 2.2 / 0.2, 7Y 23.2 / 27.1,
10Y 40.1 / 46.9, 15Y −391.1 / −409.1, 20Y+ −1.1 / 1.1. The 2.4Y bond: 2Y −78.8 / −96.0,
3Y −90.7 / −74.6 (the v3 overlay is linear in zero, the OIS bootstrap log-linear in DF:
a cash flow between pillars is split differently). The same pattern as the research on
hub data (§11 of the proposal).

## `basis_view` now

`factors.basis_view` stays, explicitly an approximation (variant P): use it to compare, or
for a curve with no quotes to rebootstrap. It maps a KBD position's risk as RUONIA = basis
bucket by bucket, which hides bucket reallocations of 4–7 % of DV01 (≤ 10Y), up to ±10 %
for bonds between pillars, the RUONIA 1M–6M exposure, and the long end.

## Portfolio

`ValuationPolicy(ofz_basis=OfzBasisIds(kbd_curve_id, ois_curve_id, basis_id))`, omitted at
its default None (earlier policies, job IDs and results keep their bytes):

- every bond discounted on `kbd_curve_id`, and (issue #90) every floater discounted on
  `ois_curve_id` and projected off `kbd_curve_id`, reports `IR|<ois_curve_id>|*`,
  `IR|<basis_id>|*` and `CS` instead of `IR|<kbd_curve_id>|*` (and the floater's
  discount `IR|<ois_curve_id>|*`, now inside RUONIA); `curve_basis` reads
  `ruonia_ois_quote+kbd_par_basis` (`OFZ_BASIS_CURVE_BASIS`); v3 position models add the
  flag `ruonia_basis`. PV, spread, yields, G-spread, `spread_buckets_rub` and JTD are
  unchanged;
- closed form where `risk_method` / `second_order_method` ask for it and the bond has one;
  everything else bumped;
- the KBD must be `rub_ofz_kbd_v3` and the OIS curve RUONIA quotes
  (`CurveBootstrapRequest`); otherwise those bonds are `INVALID_TERMS` (other positions are
  valued as usual);
- scenarios: RUONIA moves by the OIS curve's shift (named in `curve_shifts_bp`, else the
  shock's — an unnamed parallel shock moves every curve, the basis held); the basis by
  `curve_shifts_bp` of `basis_id` (held when not named; 1M–6M zero or equal to 1Y); a
  floater projected off the OIS curve takes the OIS projection shift, and so does the RUONIA
  part of a KBD floater's composed projection (`curve_shift(ois, projection=True)`: the named
  OIS shift, else `projection_curve_shifts_bp`, else the shock's) while its discount takes the
  OIS curve's shift. Naming
  `kbd_curve_id` itself is `INVALID_TERMS` for these bonds: the KBD is not their factor;
- a KBD floater on another KBD ID, key-rate and RUONIA floaters, OIS swaps and every bond on
  other curves keep their 0.39 numbers; a KBD floater on the OIS curve is `INVALID_TERMS`
  when the view cannot be built (a KBD that is not v3, an OIS curve without quotes), as an OFZ;
- `horizon_portfolio` (carry, rolldown) does not use factors and is unchanged.

Cost (`benchmarks/portfolio_bench.py --positions 2000 --cases kbd3,ofzbasis,ofzbasis-bump`,
ms per position, a shared machine):

| mode | v3 KBD, closed form (`kbd3`) | RUONIA + basis, closed form | RUONIA + basis, bumped |
| --- | ---: | ---: | ---: |
| first order | 2.43 | 3.65 | 4.47 |
| with Hessian | 3.97 | 7.26 | 26.62 |
| with Hessian and 5 scenarios | 3.53 | 7.50 | 24.41 |

Use the closed form (`risk_method` and `second_order_method` `"analytic"`): the bumped
23 × 23 Hessian is about 1 060 repricings per bond.

## Open questions

- The tail rule lives in the core (an extended request built from the KBD). The monitor
  could instead pass long OIS quotes of its own; then no synthetic quote is added.
- OIS positions keep the plain request. If the desk wants OIS swaps to see the same
  extended curve (one OIS curve for everything), set the OIS entry to the extended
  request: their curves and keys are then shared, at the price of changing their numbers
  slightly near 10Y (payment lags past the last node read the 15Y node).
- `rub_ofz_kbd_v2` (annual-zero buckets) composes the same way; only v3 is accepted now.
- KBD floaters discounted on RUONIA (issue #90) read the plain request for the discount and
  the extended one inside the projection; if the desk sets OIS positions on the extended
  request (above), both legs read one curve.
