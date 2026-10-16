# System context: where this library fits

As of 16 October 2026 (library 0.8.1). rub-fixed-income-core is the **calculation core** of the Trading Desk Monitor, a RUB fixed-income desk analytics system.

The full system architecture lives in the sibling repository **trading_desk_monitor**, folder `docs/architecture/`. With the usual side-by-side checkout it is at [`../../trading_desk_monitor/docs/architecture/README.md`](../../trading_desk_monitor/docs/architecture/README.md). Read its README first: it holds the component map, the interface catalogue and the reading order. These relative links resolve only in the local checkout, not on GitHub.

## Components

| Component | Repository | Relation to core |
|---|---|---|
| Desk backend + frontend | `trading_desk_monitor` | **The only runtime consumer.** It imports a pinned, tagged release of core in-process, builds core domain objects from market snapshots and private desk data, and calls pricing and risk |
| Market-data hub | `market-data-server` (package `mdhub`) | **No direct dependency in either direction.** The hub publishes snapshots; the backend translates them into core objects |
| PnL tool | `pnl_tool` (local) | None |

## What core must and must not do

Decisions are in `trading_desk_monitor/docs/architecture/adr/`.

| Rule | Source |
|---|---|
| Core is pure: no network, files, database or global market state. It accepts typed domain objects only | ADR-003 |
| Core never parses market-data-hub payloads and never imports hub code. The backend adapter maps the hub's wire format to core types. `public_schedule.bind_market_data_store_schedule` targets the retired market-data-store and is to be deprecated | ADR-003, 02 §1 |
| Core owns the domain model: `Instrument` (coupon/principal legs, floater formula, offers), `MarketSnapshot` (curves by ID, fixings, FX, calendars, evidence), `Position`, with `to_dict`/`from_dict` for its own types | 02 §4, [PRODUCT_ROADMAP](PRODUCT_ROADMAP.md) package 0 |
| Core owns the **curve-spec registry**, keyed by core spec IDs (for example `rub_ofz_kbd_v1`, `rub_ruonia_ois_moex_v2`). The backend maps hub `dataset` codes to these IDs | ADR-004, 02 §4 |
| Core contains the math for curve builds and floater projection. The backend orchestrates them and owns the persistent curve cache. Core reuses shocked curves within one call and accepts prebuilt curves | ADR-004 |
| Floaters are projected only when a usable formula exists. Otherwise the instrument is rejected, never priced on guessed coupons | ADR-004 §3 |
| Risk uses the desk factor vocabulary `IR\|<curve>\|<bucket>` (11 buckets), `CS`, `FX\|<ccy>`, with sparse Hessian entries (upper triangle) | 03 §4, PRODUCT_ROADMAP §2 |
| Unsupported products raise a typed rejection, never a zero. Until core supports a product, the backend imports its PV and risk from bank systems (`imported`) | 01 §3 |
| Desk conventions (funding base, OCP, dual FX basis, official PnL) are **not** core's job; they live in the backend | 04 §4 |
| Runtime target: **Windows, Python 3.11**, installed offline from a wheelhouse. Keep `requires-python >= 3.11` and test on Windows 3.11 | ADR-001, 06 §7 |

## What this repository holds

| Document | Purpose |
|---|---|
| [PRODUCT_ROADMAP.md](PRODUCT_ROADMAP.md) | **The development plan for this library**: package 0 (foundations), packages 1–9 (floaters, IRS, non-RUB, FX, futures, REPO, XCCY, CDS, special products), and acceptance criteria. Maintained here |
| [ARCHITECTURE.md](ARCHITECTURE.md), [CONVENTIONS.md](CONVENTIONS.md), [API.md](API.md), [risk.md](risk.md), [second-order-local-risk.md](second-order-local-risk.md), [local-worker.md](local-worker.md) | Internals of this library |
| [BACKLOG.md](BACKLOG.md) | Smaller fixes, including downstream items |

## Relevant system documents

All paths are under `trading_desk_monitor/docs/architecture/`:
- `02_MARKET_DATA_AND_CORE_CONTRACT.md`: how market data reaches core (wire contract → backend adapter → core domain).
- `03_CALCULATION_SPLIT.md`: which greeks core must emit and how they are used by the backend (exact) and the browser (Taylor).
- `04_BACKEND.md`: how the backend calls core, run lineage, the curve cache.
- `ROADMAP.md`: milestones. M1 needs packages 0.1–0.4, 0.6 and a minimal 0.5; M3 needs 0.7.
- `adr/ADR-003`, `adr/ADR-004`, `adr/ADR-005`.

When a change here alters the API the backend uses, update the interface catalogue in `trading_desk_monitor/docs/architecture/README.md` in the same release.
