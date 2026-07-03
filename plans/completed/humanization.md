# Plan: humanization — make the bots walk, look, and fight like humans

Status: **DONE** (executed 2026-07-03; all six behaviors validated and default ON)

## Results (final binary, post-review fixes; see PLAN.md Phase 18 for the full record)

- **Humanness**: mean KS distance across the 8 profiled features 0.333 → **0.206**
  (−38%). Pitch KS 0.67→0.12, view-vs-travel offset 0.22→0.09, yaw autocorrelation
  → 0.52 (human 0.57; stock is white noise ≈0), the snap tail collapsed (yaw-rate
  W1 123 → 28 deg/s), jumps 4→12.9/min (human 15.4), time-still 25%→21%.
  One feature regressed: strafe_interval 0.12→0.23 (hop landing jitter in the
  view-frame metric — documented in KNOWN_ISSUES).
- **Strength budget**: full stack on the standard rig, 10 pooled seeds:
  frags **−4.6%**, ITEM **+2.0pt**, pickups +3.6% — inside the ≤10%/≤3pt budget.
  Asymmetric id-parity (humanized vs stock in one match, final binary, 16+16
  seeds): kill ratio 0.818 vs 0.946 control ≈ **−13.5% relative** — the honest
  price of losing 360° vision, paid deliberately (Phase 3).
- Per-phase acceptance: P1 wash (+4.3% frags); P2 parity 0.906 vs 0.892 after
  retuning (first cut cost 38% — correlated error needs ~0.45x the white-noise
  magnitude, and reversal overshoot must be rate-limited or hopping targets
  trigger it every few ticks); P3 −0.3% frags symmetric after adding hearing
  (weapon noise <700u acquires through the cone); P4 parity 1.171
  (strength-POSITIVE, as predicted); P5 −0.5pt ITEM.
- Map spot checks: q2dm3 ITEM +2.4pt, q2dm8 +2.2pt, q2dm5 +3.2pt. On open maps
  the stack lowers combat *tempo* (q2dm5 frags −40%, deaths −28%, K/D flat):
  FOV'd, bouncing bots simply engage less on long sightlines. Style, not defect;
  noted in KNOWN_ISSUES.
- Phase 0 profiler shipped as `tools/humanness.py` (+ `dm2parse.py` viewangles/
  health/serverframe extraction, `pitch` in tick telemetry). Corpus caches under
  `demos/derived/humanness/`. The measured ranking rewrote the plan's hand-ranked
  tell table: pitch lock and stillness were the loudest tells, strafe metronome
  near the bottom.
- Post-validation adversarial review found and fixed 7 real defects: weapon-noise
  memory stuck open across map changes; an RNG-order violation vs stock (random()
  in ResetNavState); respawned bots re-acquiring their killer cone-free with no
  reaction (b->enemy survived death); the reversal-overshoot detector seeded with
  the bot's own facing; telemetry quantizing angular rates (%.1f → %.2f); the
  profiler's view-snap filter censoring the stock bot's genuine 170–180° snaps
  (removing it *worsened* the stock baseline honestly); and a one-frame lag bug
  in the wander re-pick restructure that broke stock parity. Same-binary
  determinism re-verified byte-for-byte afterward; all headline numbers above
  re-measured on the final binary.
- The eyeball test (validation ladder step 3) is handed to the user: spectate
  with `play_spectate.bat` / play against the default build.
Target: the bot's *observable behavior distributions* (gaze, turning, aim texture,
combat rhythm) move measurably toward human demo distributions, under an explicit
strength budget (below). This is a **style** goal, not a strength goal.
Prior art: PLAN.md Phases 16–17, memories `ozbot-demo-import-finding`,
`ozbot-demo-combat-calibration`, `ozbot-lift-win`.

## Why this is not the failed demo experiments again

Both prior demo transfers moved **capability** and failed for the same reason:
pro data bakes in execution skill the bot doesn't have (route import — movement
mismatch; weapon-priority calibration — dead tie). This plan transfers **style**:
distributions of observable behavior (where you look, how fast you turn, when you
jump), applied *within* the bot's own execution limits. Nothing here asks the bot
to strafe-jump.

Mechanical fit: demos record the player at the same 10Hz server framerate as our
telemetry, so bot-vs-human feature comparisons are like-for-like by construction —
whatever differences exist at 10Hz are exactly the observable ones.

**Corpus**: `../demos/sorted/` — 5000+ demos sorted by map (q2dm1 alone has 800+),
protocol 34. `tools/dm2parse.py` already extracts the recorder's trajectory; it
currently *skips* `PS_VIEWANGLES` (exposing view angles is a few-line extension).

## The tells (ranked by how loudly each screams "bot"; from code inspection)

| # | tell | where |
|---|---|---|
| 1 | **360° vision** — enemy acquisition has no field-of-view check; bots react instantly to enemies directly behind them | `Combat_FindEnemy`, bot_combat.c |
| 2 | **View bolted to velocity** — out of combat, facing = move direction exactly, pitch locked 0; never sweeps corners, glances at items, or leads turns | `Bot_Think` (`facing_yaw = b->move_yaw`), bot_main.c |
| 3 | **Instant turns** — outside combat the desired yaw is applied in one 0.1s tick (180° snaps); no mouse dynamics | `Bot_Think` angle write, bot_main.c |
| 4 | **Robotic aim texture** — constant-rate linear tracking + *per-frame white noise* (`crandom()*err`) = 10Hz vibration around the target; human error is autocorrelated (pursuit lag, overshoot on reversals, correction) | `Combat_Aim`, bot_combat.c |
| 5 | **Metronome combat movement** — strafe re-picked uniformly every 0.5–1.1s at constant full speed; jumps only from a 3%/frame dice roll; half-navigates while fighting | `Combat_Aim` blend + dodge, bot_combat.c |
| 6 | **Uniform locomotion** — always exactly full speed, straight node-to-node polylines, pivot corners, statue-stillness while waiting (item timing, lift WAIT) | bot_move.c |

## Acceptance rule (user-approved 2026-07-03: strength MAY be traded for humanness)

- **Humanness metric** (defined by Phase 0) must improve for the feature the change
  targets, measured on the standard rig's telemetry vs the demo corpus.
- **Strength budget**: each individual behavior ≤5% relative pooled-frags loss and
  ≤1.5pt ITEM loss on the standard 5-seed rig; the **full stack ≤10% frags / ≤3pt
  ITEM** vs the pre-humanization baseline. Combat-affecting changes additionally get
  an id-parity read (`bot_aimtest` pattern) since self-play totals hide asymmetries.
- `bot_skill` remains the difficulty lever; humanization must not be a stealth
  difficulty change beyond the budget.

## Phase 0 — the humanness profiler (measure first; NO bot behavior changes)

1. `dm2parse.py`: extract `PS_VIEWANGLES` (and keep origins as today). Derive
   velocity by differencing origins.
2. Telemetry: add view **pitch** to tick records (log field only, no behavior).
3. New `tools/humanness.py`: identical feature extraction from (a) a demo corpus
   (per map) and (b) bot telemetry JSONL:
   - view-vs-velocity yaw offset distribution (ozbot: a spike at 0 out of combat)
   - yaw angular-velocity distribution + autocorrelation (turn dynamics)
   - pitch distribution and pitch activity
   - jump rate, conditioned on moving vs fighting (fighting proxy: high angular
     velocity window / weapon firing where derivable)
   - strafe-reversal interval distribution (lateral velocity sign changes)
   - speed histogram; stillness-episode lengths
   - per-feature statistical distance (KS or Wasserstein) bot↔human = the
     **humanness score**, tracked per feature like ITEM completion
4. Deliverable: a ranked report of the worst tells **with numbers**, which becomes
   the roadmap. If the measured ranking contradicts the table above, follow the
   measurements.

## Phase 1 — gaze layer + turn dynamics (`bot_gaze`, `bot_turnrate`)

- Out-of-combat facing decoupled from movement (the architecture already supports
  this — movement/aim decoupling makes `facing_yaw` a free channel): look ahead
  down the path and *lead* upcoming corners; glance at items/openings passed;
  occasional shoulder checks; pitch follows target/slope instead of locked 0.
- All facing changes slew-limited with an accel/decel envelope sampled from the
  demo yaw-velocity stats — kills the 180°-snap tell everywhere, combat included.
- Mostly cosmetic alone (guardrail should be a wash); it is the *enabler* for
  Phase 3.

## Phase 2 — combat aim texture (`bot_aimtexture`)

- Replace per-frame white noise with an autocorrelated error process (e.g.
  Ornstein–Uhlenbeck: smooth wander around the true aim point), overshoot +
  correction on target direction reversals, reaction delay on direction *changes*
  (today reaction only gates acquisition).
- `bot_skill` scales the process parameters (sigma / correction speed) so the
  difficulty lever survives. Validate strength with id-parity, texture with the
  angular-velocity autocorrelation feature.

## Phase 3 — human vision (`bot_fov`) — the big one, costs strength by design

- Enemy acquisition requires the target inside a ~120° view cone **or** a recent
  damage/pain event (getting shot turns you around — implement the turn-toward-
  attacker reflex with Phase 1's turn dynamics, not a snap).
- Ties directly into the gaze layer: scanning is what acquires targets now, so
  Phases 1+3 must be validated as a pair — FOV without gaze would tank combat far
  past the budget.
- This is where the approved strength trade is expected to be spent.

## Phase 4 — combat movement rhythm (`bot_hop`, dodge rhythm)

- Jump frequency in combat sampled from demo context stats (humans jump a LOT in
  Q2 fights — likely strength-*positive*: airborne targets are harder to hit).
- Strafe-reversal intervals sampled from the demo distribution instead of uniform
  0.5–1.1s; momentum-aware reversals (brief speed dip) instead of instant flips.
- Optional: fight commitment — reduce the nav blend at close range so bots stop
  half-jogging toward items mid-duel.

## Phase 5 — locomotion texture (lowest priority; only if Phase 0 ranks it high)

- Speed variation, corner arcs (steering look-ahead), idle fidget while waiting.
  Measure first; may not be worth code.

## Validation ladder

1. **Per behavior**: humanness feature moves toward human distribution; strength
   guardrail holds (5-seed rig; 13 seeds if borderline; id-parity for combat
   changes). One cvar per behavior, default OFF until validated.
2. **Full stack**: humanness score across all features; total strength within the
   budget; q2dm3/q2dm5/q2dm8 spot check (guardrail only — humanness profiles are
   map-conditioned where the corpus is large enough, else pooled).
3. **Eyeball test**: user spectates (`bot_debug` off) and/or plays; optionally a
   blinded A/B — mixed bot/human demo clips, can the user pick the bot? This is
   the actual goal; the metrics exist to make progress on it measurable.
4. Accepted behaviors flip default ON together as the "humanization stack";
   README/PLAN/KNOWN_ISSUES + memory as usual.

## Out of scope

- Movement *capability* (strafe-jump, bunny-hop, rocket-jump) — different problem,
  known trap.
- Inverse-dynamics trajectory replay of demos — the movement-mismatch trap again.
  **Sample distributions, never replay trajectories.**
- Chat/taunts, name/skin variety (cheap cosmetics, but not this plan).
- Sub-10Hz view smoothness — demos are 10Hz snapshots too; observers see both
  through the same sampling.

## Risks (measure, don't pre-engineer)

- **Pro-flavored corpus**: the archive skews strong duel play; gaze/turn/jump
  dynamics are human-universal, but rhythm stats carry "very good player" flavor.
  Note per-feature; if "average human" texture is wanted later, reweight or filter
  the corpus (per-player stats exist in the demos).
- **Feature interactions**: FOV without gaze, or gaze without turn-rate limits,
  each looks wrong or breaks the budget — validate the pairs named above together.
- **Guardrail vs goal tension**: the budget says how much strength we may spend;
  it does not require spending it. If a humanness feature can't fit the budget,
  it gets a cvar default OFF and a documented negative, per house rules.
- **Overfitting to q2dm1**: profile per map where n is large (q2dm1 800+ demos);
  validate the stack on at least one other corpus-rich map.
