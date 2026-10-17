# Handoff: package 1 (floaters), state as of 17 October 2026

Notes for the next working session. Delete this file once package 1 is planned and approved.

## Where things stand

- Core 0.9.0–0.16.0 (roadmap waves M1 and 2) is merged to `main`
  (FiveLimaGolf/rub-fixed-income-core PR #9). The market-data-store pin was widened to
  `rub-fixed-income-core>=0.8.0,<1.0` (FiveLimaGolf/market-data-store PR #5, merged).
- Git tags `v0.4.0`…`v0.16.0` are **not** set. They need the owner's explicit confirmation.
- Next roadmap step: **package 1, floaters** (`docs/PRODUCT_ROADMAP.md` §3 package 1 and §4,
  order 3). A draft plan is below. The owner **rejected it without comment**, so ask what
  to change before writing code.
- Still open:
  - BACKLOG #37: `rub_ruonia_ois_moex_v2` needs the MOEX RUONIA OIS specification text;
  - BACKLOG #36: MOEX golden gaps (30/360 USD accrual, yield to a put offer).

## Working setup

- Python 3.11, uv. In the cloud container, run `export UV_SYSTEM_CERTS=1; unset UV_NATIVE_TLS`
  before uv.
- Checks before every push:
  - `uv run pytest -q`
  - `uv run ruff check .`
  - `uv run ruff format --check <new files>` (the repo is not fully format-clean)
  - `uv run mypy`
  - `uv build`
- Rules the owner set:
  - no breakage, only deprecations;
  - existing identifiers and hashes stay byte-identical (`tests/test_hash_stability.py`);
  - one package per minor release, each with CHANGELOG, API.md, CONVENTIONS.md and the
    roadmap status table updated;
  - answers to the owner in Russian.
- From the cloud container, the networks for cbr.ru and iss.moex.com are closed (checked
  17 October 2026).
- Sibling repos used for context: `market-data-hub`, `market-data-store`, `moex-live-bonds`,
  `trading-desk-monitor`.

## What already exists for floaters (reuse, do not rewrite)

- **`rub_fixed_income/floating.py`**
  - `FixingHistory(index, valuation_date, fixings, calendar, snapshot_id, max_unpublished_business_days=0, known_at_utc=None)`:
    - fixings are strictly before `valuation_date`;
    - RUONIA must be published every business day; the key rate stays in force until changed;
    - `rate()` raises a bare `LookupError` when a fixing is missing.
  - `FloatingCouponRule(index, averaging, margin, coupon_day_count, rate_day_count, lookback_business_days=0, rate_rounding_decimals=None, amount_rounding_decimals=None)`:
    - index `ruonia` or `key_rate`; averaging `compounded` or `arithmetic`;
    - `identifier` is a sha256 of every field.
  - `FloatingCouponPeriod(accrual_start, accrual_end, payment_date, notional_rub, published_coupon_rub=None)`.
  - `FloatingLeg(rule, periods, history).project(curve, settlement_date=)`:
    - takes the overnight forward from the DF ratio of whatever curve it is given;
    - coupons partially fixed by history are supported.
  - Not modelled: multiplier, floor/cap, calendar-day lag, a preceding-period window,
    a single fixing set before the start, index-ratio (`RUONIA_INDEX`), floater accrued interest.
- **market-data-store uses this API** (`fixed_income.py`, `curves.py`, tests). Keep:
  - positional field order;
  - the literals `ruonia`, `key_rate`, `compounded`, `arithmetic`;
  - the projection field names;
  - `FLOATING_MODEL_VERSION`.
- **`risk.BondRiskEngine`** with `BondRiskRequest(..., floating_leg=)` prices floaters on a
  single curve. That curve both projects and discounts. Known fixings stay frozen under bumps.
- **`domain.py`**
  - `FloatingCouponLeg(rule, periods, fixing_id)`.
  - `MarketSnapshot.fixings` / `.fixing(id)` with `FixingEntry`.
  - `Instrument.cashflows()` returns principal only for a floater, and
    `Instrument.accrued()` refuses a floater.
  - Nothing builds a `FloatingLeg` from a snapshot, and there is no point-in-time check
    (`known_at`).
- **`portfolio.py`**
  - `SUPPORTED_PRODUCTS = {FIXED_BOND}`.
  - `_supported_instrument` rejects floaters, both in `value_portfolio` and in
    `horizon_portfolio`.
  - `ValuationPolicy` has no projection curve.
  - `_build_curve` rejects `KEY_RATE_SWAP` specs.
- **`factors.fx_futures_factor_risk`** is the template for a two-curve factor mapper
  (2×11 buckets plus extras).
- **`key_rate.KeyRateForwardCurve`** has `rate_at` and `arithmetic_average`, but no
  `discount_factor`.

## Hub data and contract (facts found)

- **`FloaterFormula` is a contract name, not code.**
  - Defined in `trading-desk-monitor/docs/architecture/02_MARKET_DATA_AND_CORE_CONTRACT.md:91`.
  - Data lives in the hub's `ref.bond_rate_formula_resolved`.
  - Fields: `benchmark`, `spread_bp`, `multiplier`, `lag_days`, `averaging`, `floor_pct`,
    `cap_pct`, `fixing_day`, `status` (curated / inferred / partial).
  - The backend converts units to decimals.
  - Per ADR-004, core projects only formulas with status `curated` or `inferred`.
- **Inferred MOEX rows give only benchmark and spread.**
  - SU29010RMFS4: RUONIA + 160 bp.
  - SU29025RMFS2: RUONIA + 0 bp.
  - RU000A10A356: KEY_RATE + 195 bp, with a Cbonds floor of 0 and fixing day 3.
- **OFZ-PK coupon families** are documented only in
  `market-data-store/scripts/plot_ofz_pk_zspread_map.py`:
  - arithmetic, issues 29013+ — calendar days [start−6, end−7];
  - compounded, issues 29026+ — [start−7, end−7), equal to the RUONIA_INDEX ratio;
  - legacy, issues 29007–29010 — the preceding window ending at start−7.
- **Fixings in the hub's `tests/fixtures/official/`** cover only September–October 2026:
  - RUONIA 1–14 October;
  - RUONIA_INDEX 1–15 October;
  - key rate 14.00 % throughout;
  - CBR meeting calendar.
- **Possible golden values from MOEX on 16 October 2026** (settlement 19 October):

  | Bond | Golden value | Status |
  |---|---|---|
  | RU000A10A356 | `ACCRUEDINT` 12.24, which is 1000×15.95 %×28/365 | reproducible now |
  | SU29010RMFS4 | 56.36, which is 82.72×124/182 | reproducible now |
  | SU29025RMFS2 | 20.63 | needs RUONIA from about 20 August 2026 |
  | Current coupons, e.g. SU29010 at 16.59 % | | need RUONIA and key-rate history from December 2024 |

## Draft plan (rejected without comment; ask before using)

1. **0.17.0 — floater coupon terms and accrued interest.**
   - Add trailing fields with defaults to `FloatingCouponRule`:
     - `multiplier`;
     - `floor` / `cap`;
     - `lag_calendar_days`;
     - `observation_window` (in-period or preceding);
     - `fixing_days_before_start`;
     - averaging `index_ratio` with a `ruonia_index` history.
   - New fields enter `identifier` only when non-default, so old ids stay byte-identical.
   - Floor/cap on projected coupons uses intrinsic value on the forward, flagged as such.
   - Add `FloatingLeg.accrued()`.
   - Add `MissingFixing(MissingMarketData, LookupError)`.
   - Add `bind_floating_leg(instrument, snapshot, ...)` with point-in-time checks.
   - Tests:
     - the OFZ-PK families;
     - compounded RUONIA against the RUONIA_INDEX ratio on the CBR October data;
     - golden accrued values for RU000A10A356 and SU29010.
2. **0.18.0 — RUONIA floaters in `value_portfolio`.**
   - `ValuationPolicy.projection_curves` maps fixing_id to curve_id.
   - `BondRiskRequest.projection_curve`.
   - Separate `IR|discount|bucket` and `IR|projection|bucket` factors plus `CS`, with a
     Hessian of up to 23×23. They merge into one factor set when it is the same curve.
   - Fixed-bond results stay byte-identical.
   - Add a 2k-floater benchmark.
3. **0.19.0 — key-rate floaters.**
   - Projection from `KeyRateForwardCurve.rate_at`, with steps at CBR meetings.
   - A KRS curve is allowed as a projection curve only.
   - Risk from shocks to the key-rate swap quotes.
4. **0.20.0 — floaters at the horizon.**
   - Floaters in `horizon_valuation`, `explain_horizon_pnl` and `horizon_portfolio`.
   - Docs: `floating.md`, the mapping from `FloaterFormula` fields to the rule, CONSUMERS.
   - Close BACKLOG #29.

## Needed from the owner

The roadmap requires reproducing MOEX-published current coupons. That needs either:

- the data:
  - CBR RUONIA daily from December 2024, with publication dates;
  - RUONIA_INDEX;
  - the key rate from 2025;
  - or a hub `md.fixing` export;
- or network access to `cbr.ru` and `iss.moex.com`.

OFZ-PK issue terms, or the three curated formulas from the hub seed, are also wanted.
