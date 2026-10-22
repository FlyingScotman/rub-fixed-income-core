# KBD-linked floater golden fixture (`ZR_YLD_CRV`, 0.34.0)

`kbd_floaters.json` (sha256 `7f9ebcdf…`) is built on the test side from two reads of
`marketdatahub.cc` on 2026-10-22, which republishes MOEX ISS data:

- static snapshot `39cace7efb0f2dc8c0baaf7ded5466e53e4aa49df2c3299c79d29bbcd681f321`
  (business date 2026-10-22): `coupon_periods` (rate `rate_pct`, amount
  `amount_per_bond`, face, status) and `floater_formulas` (benchmark `ZCYC`, the
  current margin `spread_bp`);
- `GET /v1/history/zcyc?from=2019-01-01&to=2026-10-22`: MOEX ZCYC parameters
  (`B1..B3`, `T1`, `G1..G9`) per publication, of which the end-of-day rows
  (`is_eod`).

Contents:

| Key | What |
|---|---|
| `zcyc_eod_dates` | every day with an end-of-day ZCYC publication, 2019-01-03..2026-10-21: the publication calendar of the KBD index |
| `zcyc_params` | `[B1, B2, B3, T1, G1..G9]` of the days around each fixing (the fixing day and two publication days either side) |
| `bonds` | ten bonds: tenor and fixing offset of the identified rule, the hub's current margin, and every coupon MOEX lists as `known` after the first (placement) coupon: start, payment, face, rate %, amount, and `margin_pct` = rate - round(KBD yield at the fixing, 2) |

Only coupons with status `known` are used (`current_published`, `projected_by_moex`
and `unknown` are MOEX projections or gaps). The margin column is data derived with the
rule; the tests check that the rule leaves one margin per offer period and that its
neighbours do not.
