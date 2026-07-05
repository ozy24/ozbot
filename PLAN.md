# ozbot — AI-in-the-loop q2dm1 bot

## Context

`ozbot` is a deathmatch bot for q2dm1 ("The Edge") that lives inside the Quake II game DLL
(`gamex86.dll`). The goal is an iterative tuning loop: run bots on a dedicated server, capture
rich telemetry, analyze it offline (with AI assistance), and tune navigation / steering / goals
without rebuilding when possible.

This plan supersedes the original draft after a peer review against the actual repo. Key
corrections folded in:

- **Foundation is vanilla, not q2pro/OpenTDM.** The repo contains the original id Quake II
  source (`quake2-source/`, full engine + game) and `ozbot/src/` — a vanilla **Quake II v3.19**
  game-DLL template that already builds a working deathmatch `gamex86.dll`. There is no
  `q2pro/` or `opentdm/`. The bot is built directly in `ozbot/src/`.
- **Bots are driven entirely inside the game DLL** (ACEBot method): allocate a client-slot
  edict, run the normal `ClientConnect`→`ClientBegin` path, then call `ClientThink` each frame
  with a synthesized `usercmd_t`. No engine changes, no engine fake-client API, and **never set
  `SVF_NOCLIENT` on a bot** (it would make it invisible).
- **Demo capture uses vanilla `serverrecord`/`serverstop`** (`quake2-source/server/sv_ccmds.c:878`),
  which writes `<gamedir>/demos/<name>.dm2`. MVD does not exist on this engine.
- **"AI-in-the-loop tuning,"** not autonomous self-learning. A real learning stage is a possible
  later phase.

## Architecture

All new bot code is isolated in `ozbot/src/bot_*.c` + `bot.h`, hooked into the vanilla game at a
few integration points so the base game keeps working untouched everywhere else.

Integration points (vanilla files, minimal edits):
- `g_main.c` — `Bot_RunFrame()` called at the top of `G_RunFrame()` (before the entity loop, so
  each bot's `ClientThink` runs before `ClientBeginServerFrame` sees its buttons, mirroring how
  the engine calls `ClientThink` for real players); `Bot_Shutdown()` in `ShutdownGame()`.
- `g_save.c` — `Bot_Init()` in `InitGame()` (register cvars).
- `g_svcmds.c` — `bot_add` / `bot_remove` / `bot_clear` under `ServerCommand()` (issued as
  `sv bot_add N`).
- No `g_local.h` / `p_client.c` struct edits needed: per-bot state lives in a registry in
  `bot_main.c` keyed by client slot. (Revisit only if later phases need per-edict bot fields.)

New files (`ozbot/src/`):
- `bot.h` — shared decls (assumes `g_local.h` already included; `g_local.h` has no include guard).
- `bot_main.c` — cvars, bot registry, add/remove, population maintenance, per-frame
  `ClientThink` driver, Phase-0 wander steering, death/respawn handling.
- `bot_log.c` — JSONL telemetry to `<gamedir>/logs/<map>_<timestamp>.jsonl`.
- (later) `bot_nav.c`, `bot_move.c`, `bot_goal.c`, `bot_combat.c`, `bot_q2dm1_items.h`.

Tooling:
- `ozbot/build.bat` — locate VS2022 via `vcvarsall.bat x86`, compile `src/*.c` to
  `dist/gamex86.dll` (x86, `/DC_ONLY`, def `src/game.def`). VS2022 + x86 MSVC toolset confirmed
  present on this machine.
- `ozbot/deploy.bat` — copy `dist/gamex86.dll` to `%Q2DIR%/ozbot/`.
- `ozbot/run_server.bat` — launch dedicated `+set game ozbot +set deathmatch 1 +map q2dm1`,
  add bots, wrap the run in `serverrecord`/`serverstop`.
- `tools/analyze.py` — ingest a JSONL run, validate the loop, print a summary (ticks, per-bot
  path length, deaths, time span). Grows into clustering/heatmaps in Phase 4.

## Phasing

- **Phase 0 (this change):** Build/deploy/run harness + minimal bot harness. Bots spawn, wander
  (random-yaw with stuck-turn), respawn on death, and log per-tick JSONL + spawn/death events.
  `analyze.py` reads the JSONL and prints a summary. Validates the full
  `usercmd_t → ClientThink → Pmove → telemetry → analyze` loop end to end.
- **Phase 1 (first cut DONE):** Instead of offline Recast/navgen, the bot **learns a waypoint
  graph at runtime** by exploring (`bot_nav.c`): nodes are dropped on reachable ground, links
  recorded only between nodes actually traversed (so links are valid by construction), graph
  saved/loaded per-map (`nav/<map>.nav`) with periodic autosave. A* + simple steering
  (`bot_move.c`) follow paths to goal nodes; a goal timeout recycles unreachable goals.
  Validated in-engine: graph self-builds and persists across runs; bots compute A* paths and
  physically reach goal nodes with 0 pathfails.
  Tuning pass done: height-agnostic + "passed the node" waypoint advance, an unstick maneuver
  (strafe/back/hop) when wedged, jump-link learning, and uniform-random goals. Reach rate
  improved ~38% -> ~59% (10/17 goals, 0 pathfails). Remaining gains are location-specific
  (one bot repeatedly stalls in a spot) and best targeted with the Phase 4 stuck/heatmap
  diagnostics rather than blind tuning; also still TODO: teleporter links, corner-scrape
  detection.
- **Phase 2 (first cut DONE):** Item-driven goal layer (`bot_goal.c`). Items discovered
  dynamically by scanning entities with `->item` set (no hardcoded coordinate table — works on
  any map, reads real availability/respawn from the entity). Candidates scored
  `value * need / distance` (weapons by type+ownership, armor by current armor, health/mega by
  current health, powerups high); best in-reach item becomes the goal. A nav-coverage gate
  (skip items whose nearest node is >192u), a per-item cooldown (spreads bots, avoids fixation),
  and a 12s timeout keep it productive. Bot paths to the item's nearest node then homes in to
  touch the pickup; pickups/losses logged. Refinements: value-driven scoring (`Item_BaseValue`),
  per-item respawn timing (pre-position for value>=50 items as they respawn), and nav-node
  **seeding at item spots** (`Nav_SeedNode`/`Goal_SeedNavNodes`) so item locations are
  covered/routable.
  **Key finding:** q2dm1 has NO Red Armor / Megahealth / Quad (verified by dumping the entity
  item list — see memory `ozbot-q2dm1-items`); its prize items are Combat Armor + strong weapons,
  which bots do collect (Combat Armor, Rocket Launcher, Railgun, etc.). Goal-completion is
  ~20–33% (high variance) and is **navigation-limited** (bot reaches the item's nearest node but
  homing the last units / contention causes timeouts), not a goal-logic problem. Pushing it
  higher needs the Phase 4 stuck/reach heatmap to target specific failure spots, not more blind
  constant-tuning. Phase 2 deliverable (need/value/distance collection with dynamic discovery +
  respawn awareness) is met.
- **Phase 3 (first cut DONE):** Combat (`bot_combat.c`), overrides navigation when an enemy is
  visible. Enemy selection = nearest living player/bot with line of sight (reuses `visible()`).
  Skill-based aim: tracks toward the enemy's eyes at a `bot_skill`-scaled turn rate with a
  reaction delay and aim error (`vectoangles` for pitch; aim written to `cmd->angles` via the
  delta-angles trick). Weapon selection sets `client->newweapon` to the best owned weapon with
  ammo (Railgun > RL > Hyper > Chaingun > SSG > MG > SG > Blaster). Fires when on-target after
  the reaction delay; circle-strafes (alternating sidemove) and adjusts range to dodge. One cvar
  `bot_skill` (0..1) scales reaction/turn/error. Validated in-engine: bots engage (up to ~44% of
  time), select weapons, and frag each other (3 frags / 3 deaths in 90s, 4 bots @ skill 0.7).
  Telemetry adds per-tick `enemy` and `score`; analyzer reports %fight and frags. Engagement
  frequency is uneven (depends on bots crossing paths via nav) and kill rates are modest — a
  natural target for Phase 4 measurement/tuning.
- **Phase 4 (DONE):** Rich analysis + data-driven tuning. `analyze.py` now reports per-bot
  movement/%goal/%fight/pickups/frags/K-D, goal success, pickups-by-item, weapon usage,
  time-to-pickup, failure-hotspot clustering, and writes a top-down **coverage+failure heatmap
  PNG** (pure-stdlib encoder, no matplotlib). Data-driven iterations performed:
  (1) the analyzer revealed bots were stuck in combat 87-99% of the time (frozen, all on Blaster)
  -> **decoupled movement from aim** (`b->move_dir` world intent projected onto facing via
  `Bot_ApplyMovement`) so bots move toward goals while shooting; %fight dropped to a healthy
  ~0-50% and weapon usage diversified (RL/MG/Chaingun). (2) Fixed Blaster telemetry (NULL
  pickup_name showed as "none"). (3) Skill A/B sweep: `bot_skill` effect is within noise at
  current low kill volumes (honest finding; needs a controlled 1v1 test to measure). **Resolved
  in Phase 8** — it does matter, the original sweep just wasn't a properly controlled test.
- **Phase 5 (DONE, except Detour):** `bot_debug` **in-engine debug draw** (each bot's A* path via
  `TE_DEBUGTRAIL`, line to current enemy via `TE_BFG_LASER`; visible to a spectator). **Hazard
  avoidance** (`Bot_StepIsSafe`: don't wander into void/lava; explore-only so learned drops still
  work) cut q2dm3 deaths 30 -> 4. **Directed exploration** (`Goal_NearestItem`: head to nearest
  wanted item) collects items by contact and connects item spots into the graph on cold maps.
  **Reachability-checked goal selection** (pick the best item A* can actually reach) made routed
  goals engage on q2dm3 (%goal 0% -> 37-94%). **Map-generality validated on q2dm3**: nav learns
  from scratch, items discovered, bots navigate/fight; behavior improves across runs as the graph
  connects. Detour intentionally skipped (risky C++ graft into the 32-bit DLL; pure-C nav suffices).
- **Phase 6 (DONE):** Re-examined whether the ACEBot-style architecture itself was the ITEM%
  ceiling. It wasn't — three separate, correctly-diagnosed locomotion-layer fixes (ledge-jump,
  lift vertical-arrival, a genuine `gi.Pmove`-based short-horizon rollout planner replacing blind
  stuck-recovery) all improved secondary traversal metrics and still didn't move ITEM% much. The
  real lever was **goal-selection contention**: bots scored items independently with zero mutual
  awareness, repeatedly piling onto the same target (`item_lost` was often the single largest
  failure category). `bot_claim` (skip items another active bot already has as its goal) and
  `bot_rollout` (the physics-forward local planner) are now permanent defaults — pooled +7-12%
  pickups, ITEM% ~18%→~21-23%, confirmed to generalize to q2dm3. The lift/vertical-traversal
  problem specifically was attempted four times total (three architecturally distinct, one
  reusing the rollout mechanism) and lost its controlled A/B every time — `pathfail` consistently
  collapsed to near-zero (routing/execution genuinely works) yet pickups didn't improve, pointing
  at an opportunity-cost economics problem (a lift-gated item costs more real seconds than an
  easier alternative) rather than an execution-quality one. See memory `ozbot-item-contention-fix`
  and `ozbot-liftfix-finding` for full numbers.
- **Phase 7 (DONE, combat calibration attempt):** Extended `tools/dm2parse.py`'s demo pipeline
  (`tools/dm2_combat.py`) to extract aim-turn-rate, weapon-kill-efficiency, and item-pickup timing
  from ~1300 pro q2dm1 demos, following the original demo-import finding's own recommendation that
  combat/timing calibration (not movement) was the untried, promising use of the demo library.
  Found a large, clean-looking miscalibration (real chaingun/railgun kill-efficiency far exceeds
  the bot's hand-guessed weapon priority) and implemented it behind a genuine head-to-head A/B
  (bots split by id parity within one match, not misleading symmetric self-play). Result: a
  **null finding** — pooled 5 seeds, 171 vs 170 frags, statistically a dead tie, 4/5 seeds
  favoring the original guess. Reverted. Same underlying lesson as the original movement-import
  finding: pro demo data bakes in pro EXECUTION skill (precise sustained tracking for
  chaingun/railgun), which doesn't transfer to a bot whose own aim model doesn't have that skill
  ceiling to exploit. `dm2_combat.py` itself is kept as validated, reusable tooling. See memory
  `ozbot-demo-combat-calibration` for the full protocol-decode details and methodology (the
  id-parity head-to-head split is the correct pattern for any future combat A/B — self-play
  frags/deaths cannot validate a symmetric combat change).
- **Phase 8 (DONE):** Closed the long-open Phase 4 question of whether `bot_skill` actually
  affects combat, using the id-parity head-to-head pattern from Phase 7 (`bot_skilltest` cvar,
  default off / zero-cost, splits bots skill 0.9 vs 0.1 by id parity within one match). Unlike
  the weapon-priority test, this one is a clean, consistent positive: pooled across 6 seeds, high
  skill gets 45% more kills (250 vs 172) than low skill fighting in the same matches, 5 of 6
  seeds individually agreeing. The aim/reaction/turn-rate mechanism in `Combat_Aim` genuinely
  works -- the original "within noise" finding was a measurement-methodology gap (uncontrolled
  self-play), not evidence the mechanism is inert. Kept as permanent diagnostic infrastructure
  (like `bot_debug`) for re-validating any future retune of the skill formula. See memory
  `ozbot-skill-effect-confirmed`.
- **Phase 9 (DONE, goal economics):** Acted on the Phase-6 lift-finding's closing verdict (the
  bottleneck is route *economics*, not traversal execution): `Item_Score` divided by straight-line
  distance, so an item behind a lift/detour out-scored an easier one farther as the crow flies.
  `bot_pathcost` (now default ON) re-ranks the same bounded set of A*-checked candidates in
  `Goal_Select` by the actual A* g-cost (which carries the jump/fall/water link multipliers)
  instead of taking the first reachable item in naive score order — zero extra A* calls, the
  path cost was already computed and discarded. Validated with the seeded parallel A/B across
  5 seeds (200/500/800/1100/1700, 8×90s×5 bots): **5/5 clean wins, pooled 423 vs 279 pickups
  (+52%), ITEM 28.8% vs 21.6% (+7.2 points)** — the largest single improvement so far (bot_claim
  was +12% pickups). Median time-to-pickup dropped 4.1s→2.3s, confirming the mechanism (bots
  now pick genuinely cheaper routes). ITEM completion plateau moved ~22% → ~29%. See memory
  `ozbot-pathcost-win`.
  **Second lever, same phase:** `bot_goalbudget` (default ON) replaces the flat 12s goal timeout
  with a budget scaled to the committed route's A* cost (6s + cost/100, capped 20s; the cost is
  captured at the goal-commit `Nav_FindPath`). 7-seed A/B on top of `bot_pathcost`: raw pickups
  flat (598 vs 594 pooled) but **ITEM completion up in all 7 seeds (32.5% vs 28.8%)** on 11%
  fewer attempts, giveups down sharply — an efficiency win (same throughput, much less wasted
  travel; freed time shows up as roam arrivals/goal success 33%→40%), accepted because ITEM
  completion is the project's stated primary navigation metric and the sign is consistent, unlike
  the old progress-watchdog experiment which flooded attempts and crashed the ratio. Combined
  Phase-9 defaults: ITEM ~22% → ~33%.
  **Third lever tried, REVERTED (null/negative):** `bot_linktime` — learn measured per-link
  traversal times (EMA, 300 cost-units/sec, floored at distance for A* admissibility) into link
  costs. Live in-match learning: a wash across 7 seeds (603 vs 598 pickups, ITEM% −0.2). The
  stronger test — mature costs for 10 min, transplant *only the learned costs* onto the unchanged
  349-node topology via `tools/nav_transplant_costs.py` (isolates cost-honesty from the known
  node-growth confound), A/B the graphs with the cvar off — was a consistent net **loss** (409 vs
  438 pickups, 30.5% vs 32.9%, 0 wins/3 losses/2 ties over 5 seeds). Mechanism: learned samples
  can only raise costs (distance floor + 0.1s frame quantization inflating short links), so the
  transplant globally inflated path costs and shifted the operating point of the freshly-tuned
  scoring falloff and timeout budget rather than adding information. Code fully removed per
  convention; the transplant tool is kept (general nav-surgery infra).
- **Phase 10 (DONE, combat target-leading):** `Combat_Aim` aimed at the enemy's *current* eye
  position — rockets/bolts against a strafing target missed by construction. `bot_lead` (default
  ON) advances the aim point by enemy velocity × projectile flight time (rocket 650u/s, blaster/
  hyperblaster bolts 1000u/s per p_weapon.c; hitscan needs no lead, grenade arcs unmodeled),
  scaled by skill so low-skill bots under-lead; the reaction/turn/error model applies on top.
  Validated with **paired id-parity head-to-head** (`bot_leadtest`, kept as a diagnostic like
  `bot_skilltest`; `tools/parity_frags.py` added to sum kills by parity): 6 seeds × (control +
  treatment), 8×90s×6 bots. Controls exposed a consistent baseline parity bias (even/odd ≈ 0.81),
  so the effect is read as the *paired ratio shift*: 0.81 → 1.27 pooled, improved in 6/6 seeds,
  total frags up 470 → 501. ~57% relative kill gain — the largest combat improvement so far,
  bigger than the confirmed skill effect. Nav metrics unaffected. Methodology note: always run
  parity *controls* alongside a parity treatment; the raw even-vs-odd read would have understated
  this win.
- **Phase 11 (DONE, Track F cleanups):** (1) **q2dm3 generality confirmed** for the Phase-9
  economics defaults: 2-seed A/B flipping `bot_pathcost`+`bot_goalbudget` together on q2dm3 —
  seed 200: 81→89 pickups (29%→34% ITEM), seed 800: 87→98 (29%→35%); same win signature as q2dm1,
  so the route-economics levers are map-general. (2) **Soft claim penalty REJECTED**: `bot_claim 2`
  (keep a claimed item as a 0.25×-scored candidate instead of hard-skipping) lost the 5-seed A/B
  466 vs 476 pickups (32.7% vs 33.3%), 1W/1T/3L, with `item_lost` rising where it lost — contested
  items are contested for a reason; the hard skip stays. Reverted. Fresh full-defaults baseline
  on q2dm1 (pathcost+goalbudget+claim+rollout+lead all on): **~33% ITEM pooled over 5 seeds**.
- **Phase 12 (DONE, Track D: combat behavior layer):** One win, one instructive partial, one
  rejection. **`bot_flee` (default ON)** — fight-or-flight: a bot whose toughness
  (health + 0.5×armor) is <75 *and* <0.65× the enemy's retreats (full-weight nav intent +
  retreat component, still firing — movement/aim decoupling pays off again) until recovered
  (>100 or >0.95×, hysteresis). Paired id-parity across 6 seeds: kills ratio 0.87 → 1.08
  (**+23% relative**), deaths flat, and the standard nav rig is slightly *better* with flee on
  (36.5% vs 34.2% ITEM/3 seeds) — retreating breaks losing fights so bots keep weapons and
  re-engage on their own terms. The first variant *abandoned* the current goal to fetch
  health/armor: it won combat harder (kills +21%, deaths −18%) but the abandon/blacklist/re-pick
  churn cost ~7 ITEM% points on the nav side — the health-fetch was cut; only the retreat
  movement and a recovery-item score boost for *new* picks were kept (a future variant could
  abandon only when a reachable recovery item is close). **Directed rocket dodging REJECTED**:
  two variants (sidestep perpendicular to the rocket's flight line; then +min-range gate +hop),
  each measured across **16 fastsim seeds** — wash and slight loss respectively; the bots'
  constant strafing already captures the dodge value, and a directed override just disrupts
  combat movement. Removed from source. **Side-finding from the 16-seed controls:** the
  id-parity harness's apparent odd-bias (0.81-0.85 in the 6-8-seed realtime controls) largely
  vanishes at 16 seeds (kills 0.995) — it was substantially small-sample noise, which
  *reinforces* the paired-controls rule rather than relaxing it. Fastsim (added this session:
  `q2proded_fast.exe` via `build_engine.bat`, `run_parallel.py --fastsim`, ~100s of × realtime)
  made the 16-seed sweeps take seconds; keep runs at 90 game-seconds for baseline comparability
  and spend the speed on more seeds.
- **Phase 13 (DONE, Track E: giveup re-mine + completability economics):** Built
  `tools/mine_goals.py` (pairs every `goal`/`goal_item` commit with its terminal event per bot —
  splits the failure population by goal kind and item name, which analyze.py's flat counts
  can't). Re-mined at the ~33% baseline (5 seeds, 8×90s×5): **roam goals are healthy** (82%
  reach, 3% giveup — not a lever), but **giveups ate 52% of all in-attempt time** (median
  giveup duration 20.1s = exactly the budget cap) and were concentrated on a handful of items
  with near-zero completion — Railgun **0/67**, Chaingun 4%, HyperBlaster 4%, Grenade Launcher
  7% — the vertically-gated spots (CG+HB giveups alone: 111 events clustered at one top-of-map
  cell). Meanwhile pickups' p95 duration was 10.8s: the 12–20s budget tail funded almost pure
  failure. Also mined: 20% of item attempts end in the bot's *death* (combat, not nav) — the
  nav-true ceiling is ~46%. Two levers shipped, both default ON: **`bot_itemfail`** (escalating
  shared blacklist 20/40/80/160s on giveup at an item, reset on anyone's success — the
  completability analogue of pathcost's cost-honesty; modest consistent win alone: +5.8%
  pickups, +1.3pt ITEM, 4/5 seeds, same signature at 300s) and **`bot_budgetcap`** (goal-budget
  cap 20s→15s; the cap-value sweep showed clean dose-response — cap 12 maximizes raw pickups
  but floods attempts, cap 10 crashes ITEM% to 29%, cap 15 keeps ITEM% flat). Combined defaults
  vs old defaults, pooled 5 seeds: **pickups +14% (452→515), value-weighted +10%, frags +7%,
  ITEM% flat (32.7%→33.1%)**; q2dm3 2-seed check: +6% pickups, +2.6pt ITEM (map-general).
  Note the metric nuance: these levers raise *throughput per minute* at flat per-attempt
  efficiency — unlike the rejected progress-watchdog (which churned attempts with zero pickup
  gain), the extra attempts convert. Remaining known ceiling: the vertically-gated items are
  now economically avoided rather than collected; making them *completable* is still the
  four-times-failed lift/ascent problem, deliberately not re-attempted.
- **Phase 14 (DONE, multi-map scoping + aim-formula sweep):** (1) Bootstrapped self-learned nav
  for q2dm2/q2dm5/q2dm8 (3×300 game-sec maturation runs each; 281/134/322 nodes) and ran the
  standard rig ×3 seeds per map. **The completion ceiling tracks map verticality, not item
  logic**: q2dm2 (Tokay's Towers) ~26% ITEM with the q2dm1 failure signature (56% item-above
  giveups, RL 3% / MG 2% completion, giveups 52% of attempt time); flat-ish q2dm5/q2dm8 hit
  **~62% / ~55% ITEM** with giveups a non-issue (10%/4% of attempts) — and the Railgun that
  completes 0% on q2dm1 completes **67% on q2dm8**. Economics defaults generalize everywhere
  (same healthy shapes; no pathology). Verdict: the lift/ascent capability is the real remaining
  nav lever and is worth a fifth, capability-grade attempt on the vertical maps; on flat maps
  the residual failures are contention (item_lost) and deaths, not traversal. (2) Swept the four
  aim-formula constants with a `bot_aimtest` id-parity diagnostic (multiplier cvars bot_aimreact/
  aimturn/aimerr/aimfire; 16 seeds × 8×90s×6 bots per arm): reaction ×0.5 → kills +8%, error
  ×0.5 → +8%, turn ×1.5 → wash (not binding), tighter fire gate ×0.6 → slightly worse. **No
  retune folded in** — the hand-guessed formula is near a local optimum; behaviors (lead +57%,
  flee +23%) dwarf precision tuning. bot_aimtest kept as permanent diagnostic. Method
  side-finding: parity *death* counts have a structural ~9% even-bias in identical-behavior
  controls (kills are clean, 1.005) — read parity deaths only against a same-rig control.
  Bootstrap gotcha fixed in-session: parallel nav-bootstrap servers must get distinct net_port
  (three servers racing the default 27910 = two die at startup).
- **Phase 15 (DONE, `bot_swim` — first locomotion-layer win):** The user supplied the missing
  map knowledge: q2dm1's Railgun is **swim-gated** (enter water, horizontal tube, then a
  vertical shaft up into a small room), not lift-gated. Code inspection found the bot was
  *incapable of deliberate vertical movement in water by construction*: `Bot_SetMoveToward`
  and `Bot_ApplyMovement` flattened all intent to the horizontal plane, `upmove` was only ever
  set for ground jumps, and `Bot_FollowPath`'s height-agnostic arrival check made every node in
  a vertical shaft "arrive" instantly (horizontal distance ≈ 0), pointing the bot through the
  shaft wall — exactly the observed stall (244,-296,z≈328, +128u below the item). The nav graph
  ALREADY contained the full tube route (water-flagged nodes 96/97/183/185 → shaft 327/328 →
  room 55-58, learned bidirectionally when some bot once fell in) — A* was selling a route the
  locomotion couldn't drive. Fix (`bot_swim`, default ON): 3D move intent in water
  (`Bot_Swimming` = submerged, or in water without footing so Pmove's water-jump can fire at
  ledges), `upmove = move_dir[2] × speed` while swimming, and 3D waypoint arrival in water
  context. Result: **Railgun 0% → 48% completion** (80 attempts/38 pickups over 3 seeds,
  median route 7.5s); standard 5-seed A/B **5/5 wins, pickups +14% (515→588), ITEM 33.1%→37.0%,
  frags +30% (bots now carry Railguns), deaths flat** (no drowning). q2dm3 wash, q2dm8
  **bit-identical** with the cvar on (bots never enter water there — zero risk on waterless
  maps). Default-on build reproduces the treatment leg exactly. The user's offered route demo
  wasn't needed — but the map knowledge that redirected the diagnosis from "lift problem" to
  "swim problem" was the key unlock. Remaining vertical offenders (HB/GL/CG — real platform/
  lift ascents) are unchanged; see KNOWN_ISSUES.md.
- **Phase 16 (DONE, demo-guided lift/GL/HB investigation — instructive negative):** The user
  recorded two walking demos of the GL→HB→upper-RL routes (gl1: via the lift; gl2: via the
  ramps — no lift needed). Parsing them against the graph produced three findings and two
  rejected fixes. FINDINGS: (1) the graph already contains the *lift ride* as a learned
  vertical column of walk links at fixed (1776,1072), z537→1037 — A* routes GL attempts up it
  (cost 1409), but trajectory mining showed bots NEVER BOARD: every failure stalls 119–279u
  from the shaft; the failure is the approach/boarding, not the ride. (2) HB and upper-RL were
  literally unreachable in-graph (the z920 walkway had 180–250u coverage holes). (3) The z920
  route is a narrow ledge — the user themselves fell off it in gl2. REJECTED FIX 1 —
  `bot_lift` (vertical-context steering: stand still under a plat-column waypoint + 3D arrival,
  the land analogue of bot_swim): GL conversion unchanged (~5%); overall 13-seed A/B vs
  defaults 37.6% vs 36.9% ITEM, +2% pickups, 9W/4L — a lean-positive wash, kept in source
  DEFAULT OFF as experimental groundwork. REJECTED FIX 2 — demo-route graph surgery
  (`tools/nav_add_route.py`, new: splices a cooperative walking demo into a .nav as
  walk/fall links, skipping climb hops, with a stitching pass; plus a variant pruning the
  untraversable column links to force the ramp route): made GL/HB/upRL all reachable, but
  conversion stayed 3–13% and zero upper-RL pickups while overall ITEM dropped ~3pt (63 extra
  nodes = the maturation-regression pattern; more attempts at hard items that still fail
  mid-route). Canonical graph restored byte-identical; tool kept. CONCLUSION: for the high
  items the binding constraint is *execution precision on narrow elevated walkways and lift
  boarding*, not route knowledge — reachability ≠ executability (the recurring lesson, now
  with the cleanest evidence yet). A future attempt needs a locomotion feature (ledge-centering
  follower, or a real func_plat boarding behavior), not more graph or economics work.
- **Phase 17 (DONE, `bot_lift` — lift riding, second locomotion win):** Executed
  `ozbot/plans/lift-riding.md`. The Phase-0 instrumented diagnosis (`bot_liftlog`: per-tick
  bot/plat telemetry near func_plats) **overturned Phase 16's boarding-failure theory**: 6 of 8
  GL giveups died at (≈1576,936,z472) — horizontally 3–28u from the GL but 567u *below* it, with
  `path_idx` frozen at 1–2/12 — because the <200u **2D** final-approach override hijacked
  path-following the moment a bot entered the yard under the item and actively steered it *away*
  from the lift. The one historical success was a bot the plat scooped up by luck mid-orbit.
  Second measured trap: a bot standing anywhere in a plat's footprint holds the plat up forever
  (the inside touch trigger spans the whole shaft column), so waiting must happen *outside* the
  footprint. FIXES (all gated on `bot_lift`, now default ON): (1) level-aware final approach —
  only home straight at an item when |dz|<64; (2) `NAV_LINK_PLAT` — learn-time tagging when
  `groundentity` is a func_plat, plus a load-time reclassification of learned column links
  (dz>48, horiz<30) *verified against real plat entity footprints* (pure geometry would have
  mis-tagged ~10 water-jump ledge links); cost = distance + 400 wait allowance; plat links are
  penalize-protected and one-way; (3) a WAIT/BOARD/RIDE controller (bot_move.c) that owns
  movement intent while a plat hop is in play: WAIT holds clear of the footprint (plat descends
  in ≤3s), BOARD walks to the plat center at STATE_BOTTOM with a 2.5s no-movement failover
  (paths sometimes enter columns from railed-off upper ledges), RIDE holds the middle and hands
  the path back *past* the column top on arrival; stuck detection and replanning are suspended
  and the goal-budget clock is frozen (`goal_time += FRAMETIME`) while the controller owns the
  frame — the +400 link cost alone can't fund waits because `bot_budgetcap` already caps the GL
  route; combat *movement* blend is suppressed during BOARD/RIDE (aim/fire kept — shoot while
  riding is free via the movement/aim decoupling); 10s master WAIT deadline → LIFT_FAILED →
  normal giveup path. RESULTS (standard 8×90s rig): q2dm1 5-seed A/B — **GL 5.2% → 41.1%,
  Chaingun 0% → 54.5%**, ITEM 38.3% → 42.0% (4W/1 wash), pickups +13.5%, frags +15% (deaths
  +15% is tempo-coupled: frag/death ratio flat, no crush-death signal); q2dm5 +3.5pt ITEM /
  +34% pickups (it has a small lift); q2dm8 +2.7pt / +11%; q2dm3 a wash over 8 seeds (no plats
  pathed; residual delta is the level-aware homing + noise). Default-ON build reproduces the
  treatment leg event-for-event. HB stays ~8% (narrow-walkway capability, explicitly out of
  scope). `bot_liftlog` kept as a permanent diagnostic. Note for A/B hygiene: navs saved by
  lift-ON runs persist PLAT link types/costs (see KNOWN_ISSUES). Post-review fix: stale
  `lift_state` no longer suppresses combat movement if `bot_lift` is toggled off mid-run.

- **Phase 18 (DONE, humanization stack — first style milestone):** Executed
  `ozbot/plans/humanization.md`: make the bots *look/move/fight* like humans, trading bounded
  strength for humanness (user-approved budget: ≤5% frags per behavior, ≤10% for the stack, on
  the standard rig). **Phase 0 profiler first** (`tools/humanness.py` + `dm2parse.py` extended
  to decode PS_VIEWANGLES/STAT_HEALTH/serverframe; `pitch` added to tick telemetry): identical
  feature extraction from 1,299 pro q2dm1 demos and bot telemetry (velocity by origin
  differencing on BOTH sides — like-for-like at 10Hz), KS/W1 distances per feature. The measured
  ranking rewrote the hand-guessed tell list: **pitch locked at 0** (KS 0.68) and **statue
  stillness** (26% of time vs 12%) were the loudest tells; yaw texture was white noise
  (autocorr −0.02 vs human 0.57) mixing dead-still frames with 1600°/s snaps; strafe metronome —
  presumed loud — measured near the bottom. Six behaviors, each cvar-gated and validated
  separately (5-seed symmetric A/B + 16-seed id-parity for combat-affecting ones), then as a
  stack: `bot_gaze` + `bot_turnrate` (path-leading gaze with fixation-then-catchup, glances,
  live pitch, per-turn speed draws, wander headings arc — strength wash), `bot_aimtexture`
  (OU aim error + rate-limited reversal overshoot; **first cut cost 38% of kills** — correlated
  error means miss *streaks*, so equal-magnitude replacement of white noise is far from
  strength-neutral; shipped at 0.45× magnitude with faster correction, parity 0.906 vs 0.892
  control), `bot_fov` (~120° cone + pain reflex via a one-line `player_pain` hook +
  **hearing** via a `PlayerNoise` hook — unsilenced gunfire within 700u acquires through the
  cone; hearing halved the asymmetric cost from −24%; final full-stack parity 0.818 vs 0.946 control = −13.5% relative; symmetric cost −0.3% frags),
  `bot_hop` (demo-fitted strafe legs, momentum dip on reversals, combat jumps ~1/1.2s, close-
  fight commitment — **strength-positive**, parity 1.171: airborne targets defeat velocity
  lead), `bot_fidget` (micro-step fidget while holding respawn waits — leashed 24u, StepIsSafe-
  checked, never during lift states; fast turn-away instead of wall dithering; travel hops).
  One real interaction found and fixed: hop's bounce flipped the aim-texture reversal detector
  every few ticks, spamming overshoot+reaction (stack frags −13.3%); rate-limiting overshoot to
  0.6s restored the stack to **−4.6% frags / +2.0pt ITEM pooled over 10 seeds** (final binary).
  Full-stack humanness: mean KS 0.333 → **0.206** (pitch 0.12, offset 0.09, yaw autocorr 0.52 vs
  human 0.57, snap-tail W1 123→28 deg/s, jumps 12.9/min vs human 15.4); one regression
  (strafe_interval 0.12→0.23, hop-landing jitter in the view-frame metric) and residuals (stillness 23% — lift WAITs are load-bearing; speed texture
  capped by the no-momentum movement ceiling) documented in KNOWN_ISSUES. Map spot checks:
  ITEM +2.2 to +3.2pt on q2dm3/5/8; on open q2dm5 the stack cuts combat *tempo* ~40% (K/D flat)
  — FOV'd bots engage less on long sightlines; documented as style, not defect. All six default
  ON; default build reproduces the treatment leg event-for-event. A post-validation
  adversarial review found 7 real defects, all fixed and re-measured (noise memory surviving
  map changes, an RNG-order violation, respawn killer re-acquisition, overshoot mis-seeding,
  telemetry rate quantization, the profiler censoring the stock bot's own 170-180° snaps, and
  a one-frame wander-heading lag that broke stock parity). Same-binary determinism re-verified
  byte-for-byte; cross-binary bit-identity is unattainable (x87 FP layout jitter), so A/Bs must
  flip cvars within one binary. Harness fix worth remembering: id-parity sweeps need an **even**
  bot count (5 bots = 3:2 parity imbalance = phantom 1.53 kill ratio). The eyeball test is the
  user's: `play_spectate.bat` (their new first-person eyecam) or play the default build.

- **Phase 19 (DONE, `bot_strafejump` — strafe jumping, third locomotion win):** Calibrated from
  the user's own input capture (`inputs_598.dm2` + `bot_inputlog` trace: chained hops, fwd+side
  400 held, jump held across landings, 1–3°/frame yaw sweep, 300→557 over 5 hops). Physics
  verified in pmove source before building: air-accel is **tick-rate neutral** (accel·ft·wishspeed
  = 30 ups per 100ms tick, same per-second budget as a 30Hz human), `PM_CheckJump` precedes
  `PM_Friction` and nulls groundentity so a first-grounded-tick re-jump is friction-free, and
  holding jump while airborne can't latch `PMF_JUMP_HELD` — so the 10Hz command rate is NO
  barrier (unlike the mega double-jump, Phase-2-longjump finding). One real blocker: bots'
  `gi.Pmove` uses the engine's default pmove params, which lack q2pro's per-client strafejump
  hack, so hop landings (vz≈−290) set `PMF_TIME_LAND` (144ms jump lockout → one full-friction
  100ms ground frame → −60% speed). Fixed **DLL-side** (no engine change, community drop-in
  preserved): the game owns `ps.pmove` between frames; `Bot_RunFrame` clears the flag for bots
  mid-chain — exact strafehack parity with what real clients already get. Controller
  (`Bot_StrafeThink`, bot_move.c): CHORD-based runway qualification over the committed path
  (straight line bot→far node, intermediate nodes hug ≤40u laterally, flat ≤24 dz; per-segment
  polyline collinearity starved chains — learned nodes wander ~30° down straight halls), traces
  vs WORLD ONLY at body height (MASK_PLAYERSOLID qualified other bots as walls; apex-headroom
  lanes rejected every doorframe — a head-clip only flattens the hop, so headroom is deliberately
  not required), open sides need safe floor but ONE walled side is fine; `gi.Pmove` pre-sim of
  the whole first hop gates the commit; per-tick optimal yaw law `facing = vel_heading +
  side·(45° − acos(270/speed))` (auto-reproduces the human's accelerating sweep; per-hop side
  pick steers back to the spine, alternating on straights, holding through curves); seamless
  chord re-qualification at the landing tick extends chains through gentle bends without a
  friction reset. Owns movement AND facing while active (gaze slew would corrupt the wishdir
  angle; ApplyMovement's unit projection would cap the saturated diagonal — cmd written
  directly); goal budget keeps billing (travel is faster than budgeted, unlike lift waits).
  **Results (8-seed standard-rig A/B):** pickups +6.1% (1295 v 1221), ITEM 46.6% v 44.5%,
  giveups −11%, frags/hazard-deaths flat; ~1 engage per 7 bot-seconds, 83% clean completions,
  2.2% problem aborts, chains peak **518–522 ups** (human capture: 557), hop-2 takeoffs mean 423.
  Default ON; default build reproduces the treatment leg exactly (seed 100: 159/72 identical).
  `bot_sjlog` (1 = events, 2 = +qualification-funnel counters) kept as a permanent diagnostic.

- **Phase 20 (DONE, `bot_decisive` — decisiveness, biggest pickup win since bot_pathcost):**
  Prompted by the user *watching* bots "stand there cycling between two possible objectives for
  3-4s".  Telemetry confirmed exactly: 24% of goal exit→commit transitions were **standing
  re-decides** (≥1s, <64u displacement; median 2.3s, ~1.4 yaw reversals/s).  Root causes, all in
  the *gap between goals*: (1) `Bot_GoExplore` schedules the next `Goal_Select` 1-3s out — dead
  time after EVERY pickup/giveup/reach; (2) during that gap the explore branch steers per-frame
  to `Goal_NearestItem`, a hysteresis-free, cooldown-blind argmin — standing between two
  near-equal items flips the winner with micro-motion (view swings A↔B), and it happily pulled
  toward the very item just declared unreachable; (3) `Goal_Select` sets `goal_item` even on
  evaluations whose commit is rejected, phantom-claiming items against other bots
  (anti-correlated pair swapping).  The ROUTED pipeline was already damped (10s post-abandon
  blacklist) — the indecision lived between goals, not in scoring; deliberately added NO
  commitment bonus to Item_Score.  Fix (all gated, off-arm RNG stream untouched — the GoExplore
  `random()` still runs, its result overwritten): `Bot_DecisiveReplan` = re-pick +0.2s after
  success (pickup/item_lost/reach), +0.6s after failure (giveup/pathfail); sticky
  `b->steer_item` in `Goal_NearestItem` (hold unless stale/unwanted or new winner ≥25% closer)
  + skip cooldown items; clear `goal_item` on non-commit evaluations.  Validation miner promoted
  to `tools/mine_indecision.py`.  **Results (5 fixed seeds, standard rig): standing re-decides
  26-28% → 1-3% of transitions (−90%); pickups +45% (1119 v 773, up 37-52% in EVERY seed); goal
  attempts +27%; ITEM 50.3% v 44.1%; frags a wash; giveup and item_lost RATES per attempt both
  down** (thrash guard passed — the throughput gain is real, not churn).  Default ON; default
  build reproduces the treatment leg exactly (seed 100: 247/76, ITEM 52%).

## Phase 0 detail

**Bot lifecycle** (`bot_main.c`):
- `Bot_Init()`: register cvars `bot_count` (target population, default 0), `bot_forwardspeed`
  (default 400), `bot_debug` (default 0).
- Population maintenance in `Bot_RunFrame()`: each frame, add/remove one bot toward `bot_count`.
  Map changes auto-repopulate (registry is reset on map change, detected via `level.mapname`).
- `Bot_Add()`: scan client-range edicts `g_edicts[1..maxclients]` for a free slot; build
  userinfo (`\name\OzBot<N>\skin\male/grunt\hand\2\fov\90`); call `ClientConnect` then
  `ClientBegin`; record in registry. (`deathmatch 1` required so the DM spawn path runs.)
- `Bot_Remove()`: `ClientDisconnect(ent)`, free registry slot.

**Per-frame driver** (`Bot_RunFrame()` → for each bot → `Bot_BuildCmd` → `ClientThink`):
- Build `usercmd_t`: `msec=100`; set `angles[YAW]` to a desired absolute yaw via
  `ANGLE2SHORT(yaw) - ps.pmove.delta_angles[YAW]`; `forwardmove=bot_forwardspeed`.
- Wander: re-pick a random yaw on a 1.5–3.5s timer, or sooner if stuck
  (`VectorLength(velocity) < 20` while on ground, throttled).
- Dead: set `buttons=BUTTON_ATTACK` so `ClientBeginServerFrame` respawns the bot.
- Detect alive↔dead transitions to emit spawn/death log events.

**Telemetry** (`bot_log.c`):
- Lazy-open a new file per map: `<gamedir>/logs/<map>_<YYYYmmdd_HHMMSS>.jsonl` (`_mkdir` the
  `logs` dir; `<gamedir>` from the `game` cvar). `va()` reuses one static buffer, so build paths
  with `Com_sprintf` into locals.
- Per tick (10 Hz): `{t, bot, name, x,y,z, vx,vy,vz, yaw, onground, health, armor, weapon, dead}`.
- Events: `spawn` and `death` (with origin/health). Buffered stdio, `fflush` ~1 Hz; close on
  `Bot_Shutdown()` and on map change.

## Verification (end to end)

1. **Build:** run `ozbot/build.bat`; confirm `dist/gamex86.dll` is produced (x86) with no errors.
2. **Deploy:** set `Q2DIR` to a Quake II install with q2dm1; `ozbot/deploy.bat` copies the DLL to
   `%Q2DIR%/ozbot/`.
3. **Run:** `run_server.bat` starts a dedicated server `+set game ozbot +set deathmatch 1
   +set maxclients 16 +map q2dm1`, `sv bot_add 2`, `serverrecord run1`; let it run ~60s; `serverstop`.
4. **Telemetry:** confirm `%Q2DIR%/ozbot/logs/q2dm1_*.jsonl` exists and grows; spot-check a few
   lines are valid JSON with changing `x,y,z`.
5. **Analyze:** `python tools/analyze.py %Q2DIR%/ozbot/logs/q2dm1_<ts>.jsonl` prints ticks,
   per-bot path length (> 0 ⇒ movement works), and death count.
6. **Demo:** play back `%Q2DIR%/ozbot/demos/run1.dm2` and confirm bots visibly move (validates
   `usercmd → ClientThink → Pmove`).

Note: building is possible on this machine (VS2022). Running requires a local Quake II engine +
q2dm1 data, which is the user's to provide via `Q2DIR`.
