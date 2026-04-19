# Danger Alert addon — findings

Analysis of `tome-danger-alert_10.teaa` (yutio888, 2019). A Lua addon that
augments enemy tooltips with threat scores and special-ability flags.

## Key insight: normalize threat to % of effective HP

Every threat score in the addon is expressed as
`expected_dam / (max_life - die_at) * 100`: the percentage of the player's
effective HP removed per hit or talent activation.

- `< 20`  Low
- `< 35`  Mediocre
- `< 70`  High
- `≥ 70`  Deadly (can plausibly remove ≥70% of your HP in one turn)

This unit is what we should adopt for our enemy panel and for the
marginal-gain math in the item scoring system.

## Canonical formulas (ported into `scoring/combat_math.py`)

**Hit rate.** `hit = clamp(50 + 2.5 * (atk - def), 0, 100); hit *= (100 - evasion) / 100`.

**Armor / hardiness split.** ToME armor is not a flat subtraction:
`hardiness%` of incoming damage is absorbed by armor (floored at 0), and
`(1 - hardiness%)` of the damage bypasses armor entirely. APR reduces
effective armor. Consequence: armor alone never zeroes out a big hit —
resistance is the only lever that scales to arbitrary damage.

**Probability stacking.** `sp_multiply(a, b) = 100 - (100 - a)(100 - b)/100`.
This is the correct way to compose independent chances (multiple resist
sources, evasion sources, CC immunities).

**Crit expected damage.** `dam_crit = dam * (crit% * crit_power + (1 - crit%))`.
The addon *doubles* crit chance when estimating incoming damage — a
deliberate pessimism so a spiky enemy is flagged before it one-shots you.

**Weapon threat shape.** `weapon_threat(entity) = expected_hit_damage * rank_scalar * speed_scalar / effective_hp * 100`.
When raw threat < 60 it is further scaled by hit rate; when ≥ 60, by
`min(hit * 2, 100)` — high-damage enemies are treated as more dangerous
even if their nominal hit rate is modest, because one connecting crit is
decisive.

## Talent damage introspection (`getTalentDamageTricky`)

The addon estimates talent damage without running the game by
monkey-patching `damDesc` and `combatTalentWeaponDamage` inside each
talent's `_info` function, running it under `pcall`, and capturing the
largest damage value and weapon multiplier written during description
rendering.

**Not directly portable** — we're not in-engine — but the idea applies to
build inference: we can scrape the `.team` archive (the same archive our
`game_data/talent_db.py` already reads) for "Scales with Spellpower" /
"damage = X" patterns at load time and cache them.

## `tactical.disable` auto-detection

Every talent definition in the archive can carry a
`tactical_imp` or `tactical` table with a `disable` sub-table keyed by
one or more of: `stun`, `confusion`, `disarm`, `pin`, `blind`, `silence`,
`sleep`, `teleport`. Walking an enemy's known talents and aggregating these
gives a free CC-threat list per enemy.

This table is in the `.team` archive — again, reachable via the same
static-analysis path we use for the talent cache.

## Curated special-ability talent IDs

Handful of talents worth flagging verbatim because they shift the
engagement rather than deal damage:

| Talent ID                                        | Flag                    |
|--------------------------------------------------|-------------------------|
| T_DEMON_PLANE                                    | Teleports you to a zone |
| T_INNER_DEMONS, T_SPLIT, T_SHADOW_SIMULACRUM     | Spawns a clone of you   |
| T_DIMENSIONAL_ANCHOR                             | Blocks your teleports   |
| T_BURROW, T_CULTS_OVERGROWTH                     | Breaks walls            |
| T_COMBINATION_KICK, T_DISOLVING_ACID, T_ENTROPY, T_DISPERSE_MAGIC, T_CORRUPTED_NEGATION, T_ACIDFIRE, T_SPINAL_BREAK, T_CORRUPTING_STRIKE, T_PRUGING_TRAP, T_RETCH, T_SWITCH, T_STATIC_SHOT | Dispels sustains/effects |
| `global_speed > 1`                               | Fast (extra actions)    |
| `combatSeeStealth() > 0`                         | Sees through stealth    |
| `combatSeeInvisible() > 0`                       | Sees invisible          |

## Limitations to be aware of (inherited vs. fixable)

- **Activated-only.** The addon only walks talents with `t.mode == "activated"`.
  Passive and sustain talents that deal damage are invisible to it.
  *Fixable:* walk sustains too, conditioned on whether they're currently active.
- **No cooldown / resource check.** Assumes the worst-case talent is always
  available. Acceptable for a danger flag; misleading for precise DPR.
- **Silent `pcall` swallow.** Some talent `_info` functions don't call
  `damDesc` at all (pure utility), or error out under the monkey-patch.
  Those report zero damage. *Fix:* treat as lower bound, not ground truth.
- **Max-mult caching for 10 turns.** Good enough for UI; don't use for
  anything real-time.
- **Crit doubling.** Intentional pessimism. Fine for a "can this one-shot
  me?" indicator; wrong if used for expected-over-many-hits math.

## Applicability to this project

**Enemy panel (existing, in `gui/enemy_panel.py`).**
Replace the current `_RANK_WEIGHT`-based danger heuristic with the
ported formulas once the player-side fields (`combat_armor_hardiness`,
`die_at`, `resists`, `resists_pen`, `combat.evasion` attr) are in the
memory reader's player snapshot. The new rating answers "can this enemy
remove 70%+ of my HP in one turn?" rather than "is this a rare/unique?".

**Combat advice.**
Given a threat report, solve the inverse: what is the smallest resist or
armor bump that brings `expected_damage` below effective HP? This is the
"increase fire resist by 2% to survive one hit" output.

**Item scoring system (planned).**
The same primitives (`hit_rate`, `armor_absorb`, `sp_multiply`,
`effective_resist`) are exactly the building blocks the marginal-gain
step needs. Stacking a new item's resist onto existing resist uses
`sp_multiply`; scoring armor on a new body slot uses `armor_absorb`
with the candidate vs equipped values; immunity scoring uses diminishing
returns against the existing immunity floor.

Prefer a single `scoring/` package that both the enemy panel and the
item rating system import from, rather than duplicating formulas.

## What does NOT translate

We are Python + `ReadProcessMemory`, not in-engine Lua. We can't call
`self:combatDefense()` live — but we already read the computed values
those functions return. So we port the *formulas* and feed them from
the memory snapshot. No gameplay hook is needed.

The `tactical.disable` introspection similarly requires archive-side
static analysis (already a solved pattern in `game_data/talent_db.py`).
