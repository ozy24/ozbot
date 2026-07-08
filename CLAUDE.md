# CLAUDE.md — ozbot (10Hz / x86, q2pro rig)

This file provides guidance to Claude Code (claude.ai/code) when working in this repository.
This is the **original** ozbot bot. Its 40Hz/x64 sibling lives in `../ozbot-re` (own CLAUDE.md),
and the **shared runtime + corpus + engine sources** are documented in the umbrella `../CLAUDE.md`
— read that once per session for the cross-repo layout, revert hygiene, and shared-infra rules.

## What this is

**ozbot** is a self-learning deathmatch bot for **Quake 2 / q2dm1 ("The Edge")**. The bot lives
*inside* the Quake 2 game DLL (`gamex86.dll`); there are no engine changes. All bot logic is in
`src/bot_*.c` + `bot.h`/`bot_nav.h`, hooked into a vanilla Quake 2 game at a few points.

The design, phase history, and key findings live in `PLAN.md` (this repo), in `plans/` (per-arc
design docs + measured results: `completed/`, `in progress/` — code comments reference them by
path), and in the persistent memory at
`C:\Users\chris\.claude\projects\E--code-projects-ozbot\memory\` — read those first; they contain
non-obvious decisions and dead-ends already explored. `../CLAUDE_CODE_BEST_PRACTICES.md` has
process guidance for working in this tree — token-efficiency habits (never read raw
telemetry/`.nav` binaries), model-selection tiers, and the git/process safety rules from recorded
incidents; read it once per session too.

**Version control scope:** this `ozbot/` directory is the git repository and now includes its own
`tools/` (analysis + sim tooling). The **shared, unversioned** surface lives at the umbrella root:
`../engine/` (runtime), `../demos/` (corpus), `../q2pro/`, `../quake2-source/`, and the root
process docs. Mind the revert-hygiene memory: diff before overwriting anything outside a git repo,
since there is no safety net there.

## Hard constraint: 32-bit

The engine is **32-bit**, so `gamex86.dll` **must be built x86**. `build.bat` uses the MSVC
**x86** toolchain via `vcvarsall.bat x86` (VS2022 is installed). Do not build x64 — the engine
silently won't load it. Verify a built DLL's PE machine is `0x14c` if in doubt.

## Repository layout

- `src/` — vanilla Quake 2 v3.19 game source **+** the new `bot_*` files. The C build is
  self-contained: no `#include` escapes this repo (`quake2-source/` is a provenance reference only).
- `dist/gamex86.dll` — build output. `build.bat` / `deploy.bat` / `run_server.bat` / `play.bat`.
- `tools/` — Python (stdlib only) analysis & demo tooling (see below).
- `plans/` — per-arc design docs + measured results. `baselines/` — pinned matured `q2dm1.nav`.
- Shared at the umbrella root (see `../CLAUDE.md`): `../engine/` (32-bit q2pro runtime —
  `q2pro.exe` + the fastsim server `q2proded_fast.exe` + `baseq2` paks + the `ozbot/` gamedir where
  the DLL deploys and `nav/`, `logs/` are written), `../demos/`, `../q2pro/` (fastsim source).

## Build / deploy / run

All commands run from this `ozbot/` dir (Windows). `%Q2DIR%` defaults to `..\engine`.

```bat
build.bat          :: compile src/*.c -> dist/gamex86.dll (x86, MSVC)
deploy.bat         :: copy dist/gamex86.dll -> %Q2DIR%/ozbot/
run_server.bat     :: build + deploy + launch a dedicated server with bots
play.bat           :: launch a listen server you can play IN against bots
                   ::   (as a spectator you can chase-cam bots; Use toggles first-person
                   ::    eyecam on the chased bot -- the way to *watch* bot behavior live)
record_inputs.bat  :: play q2dm1 with bot_inputlog on + a synced demo, to capture YOUR inputs
build_engine.bat   :: build ../q2pro -> %Q2DIR%/q2proded_fast.exe (the fastsim engine; rarely needed)
```

Headless sim (what's used for iteration/measurement), run from `../engine/`:
```
q2pro.exe +set dedicated 1 +set game ozbot +set deathmatch 1 +set maxclients 16 \
          +set bot_count 5 +set bot_skill 0.5 +map q2dm1
```

**There is no unit-test suite.** The bot is validated empirically by the loop:
**build → deploy → run a timed sim → analyze the telemetry.** Telemetry JSONL is written to
`../engine/ozbot/logs/q2dm1_<timestamp>.jsonl`; analyze it with:
```
py tools/analyze.py <logfile.jsonl>      # per-bot stats, ITEM completion, failure hotspots, heatmap PNG
```

**Parallel sim harness + fastsim engine — the standard way to iterate/measure.**
`run_parallel.bat` does build → deploy → N parallel headless sims → merged analyze; it forwards all
args to `tools/run_parallel.py`. **Always pass `--fastsim`**: it runs the patched
`../engine/q2proded_fast.exe`, whose `fastsim` cvar makes the dedicated loop skip its per-tick sleep
and inject one game tick per iteration → CPU-bound sim at **~400× realtime** (the standard
8×90s rig completes in ~3 wall-seconds; the stock engine caps at ~2× no matter the timescale,
because `SV_Frame` runs one game frame per loop iteration). Fastsim is bit-exact — same seed
reproduces the realtime engine's telemetry byte-for-byte (verified 2026-07-02).
```bat
run_parallel.bat --fastsim --instances 8 --seconds 90 --bots 5
run_parallel.bat --fastsim --instances 8 --seconds 60 --seed 200 --cvar bot_ledgejump 1
```
With `--fastsim`, `--seconds` means **game seconds**: each server quits itself via the DLL's
`bot_quitafter` cvar (saving its nav first), so every seed simulates the same game time no matter
the CPU load; without `--fastsim` it is wall-clock and servers are killed. Each instance gets an
isolated worker gamedir (`../engine/ozbot_wN`, seeded with the deployed DLL + a copy of `<map>.nav`)
and its own `net_port`; logs are merged with a per-instance bot-id offset.
**Pass `--seed` for reproducible A/B measurements** (worker *i* gets `seed+i`); omit it for
independent pid-seeded samples. `--cvar NAME VALUE` (repeatable) passes any cvar through to every
server — the standard rig for A/B'ing a change behind a cvar gate. `--mod <dir>` changes the
source gamedir workers are seeded from (default `ozbot`) — use a scratch gamedir with a pinned
DLL + nav snapshot when the canonical dir may be live (see Gotchas). Single-seed results mislead;
always A/B across several seeds. Fastsim makes big samples cheap — prefer more seeds/longer runs
over trusting a noisy small one (but remember long runs mature the nav graph *within* the run;
90s stays comparable to historical baselines).

### Runtime knobs (cvars + server cmds)
`bot_count` (target population, auto-maintained), `bot_skill` (0..1: aim reaction/accuracy),
`bot_forwardspeed`, `bot_debug` (1 = draw nav paths via temp-entity beams), `bot_seed` (>0 =
deterministic RNG for reproducible runs; 0 = auto-seed from pid+time — the game DLL otherwise never
calls `srand()`, so runs would be byte-identical), `bot_quitafter` (>0 = quit the server after N
*game* seconds — how fastsim runs are timed), `bot_lift` (default 1: the lift capability — PLAT
links + wait/board/ride controller; Phase 17), `bot_liftlog` (1 = per-tick diagnosis telemetry
near func_plats), `bot_strafejump` (default 1: chained strafe-jump travel on trace-qualified
straight/gently-curving runways — hops reach ~440-520 ups vs the 300 run cap; Phase 19, calibrated
from a human input capture; +6% pickups / −11% giveups / frags flat over 8 seeds), `bot_sjlog`
(1 = strafe-jump engage/hop/done/abort events; 2 = also the qualification-funnel counters),
`bot_decisive` (default 1: prompt goal re-picks after pickup/giveup + sticky/blacklist-aware
explore steering — kills the 2-3s standing "which item?" A↔B re-decide loop; Phase 20: pickups
+45%, ITEM +6pts, 5/5 seeds; validate with `tools/mine_indecision.py`), `bot_inputlog` (1 = log
a real player's per-frame usercmd — see below), `bot_navmask` (default 0 and staying so:
capability-filtered A* — lost its value A/B because capability-off traversal is probabilistic,
not impossible; kept as oracle infrastructure — see `plans/in progress/nav-oracle.md`),
`bot_reachlog` (default 1: oracle sweep of every item's in-graph reachability at map load and
at quit — JSONL `reach` records + console lines; found the seeded-island defect), `bot_itemfail 2`
(opt-in: fast-track the blacklist when the giveup-time oracle says the route evaporated —
measured a practical no-op, 91-98% of giveups have a live route), `bot_goalnode` (default 0 and
staying so: 1 = resolve item goal nodes to connected nodes so exact-at-item orphan islands can't
shadow coverage, 2 = also skip budget-unfundable routes — the unlock is real but conversion isn't:
mode 1 +3% pickups / +40% giveups, mode 2 −17% pickups; needs last-leg execution work first).
Console:
`sv bot_add N` / `sv bot_remove N` / `sv bot_clear` / `sv nav_query <item substring>` (prints
each matching item's reachability verdict + what capability gates it).

### Capturing a human's inputs (`bot_inputlog` — movement/jump analysis)
A `.dm2` demo records position + view angles but **not** the raw inputs. With `bot_inputlog 1`, the
DLL (`Bot_LogInput` in `bot_log.c`, called from `ClientThink`, gated on `!Bot_IsClient`) writes one
`{"type":"input",...}` JSONL record per frame for each **real** player to the normal telemetry log
(`../engine/ozbot/logs/<map>_<ts>.jsonl`): forward/side/up move, jump (`up>0`), attack, per-frame
view yaw/pitch, origin, velocity, speed, onground, waterlevel. **`record_inputs.bat`** launches a
q2dm1 listen server with this on plus a synchronized demo (play, do the move, `quit` to flush the
demo). Analyse with **`py tools/input_view.py <log.jsonl> [slot]`** — it segments the trace into
jumps and prints each jump's key-hold timeline, view-yaw sweep, and speed curve. (Built to study the
q2dm1 Megahealth trick jump; that capability was shelved on this 10Hz rig — the human plays at
~30 Hz while the bot commands at 10 Hz, so a velocity-stacking double jump can't be reproduced. See
the `ozbot-longjump-10hz-finding` and `ozbot-input-logger` memories. The 40Hz `../ozbot-re` rig
does land it via playbooks.)

## Bot architecture (the big picture)

The bot is **driven entirely from inside the DLL** (the ACEBot approach): a bot is a normal
client-slot edict spawned via the real `ClientConnect`→`ClientBegin` path, then driven each frame
by synthesizing a `usercmd_t` and calling `ClientThink`. Bots are fully visible entities — never set
`SVF_NOCLIENT` on them.

**Integration hooks into the vanilla game (small, deliberate):**
- `g_main.c` — `Bot_RunFrame()` at the *top* of `G_RunFrame()` (so each bot's `ClientThink` runs
  before `ClientBeginServerFrame` sees its buttons, mirroring the engine's real-client ordering);
  `Bot_Shutdown()` in `ShutdownGame()`.
- `g_save.c` — `Bot_Init()` in `InitGame()`.
- `g_svcmds.c` — `Bot_ServerCommand()` dispatch.
- No `g_local.h`/`p_client.c` struct edits: per-bot state is a registry (`bot_t bots[]`) keyed by
  client slot in `bot_main.c`.

**Per-frame pipeline** (`Bot_Think` in `bot_main.c`) — note movement is **decoupled from aim**:
1. `Bot_Navigate` (bot_main + bot_move + bot_nav) decides *where to move*, setting a world-space
   intent `b->move_dir` / `b->move_yaw` / `b->want_jump`. It also **learns the nav graph** each
   frame and runs the goal state machine (explore ↔ goal).
2. `Combat_Aim` (bot_combat.c) sets the *facing* (aim) and the fire button if an enemy is visible,
   and blends a strafe/range component into `b->move_dir`.
3. `Bot_ApplyMovement` projects `move_dir` onto the chosen facing to produce `forwardmove`/`sidemove`.
   → This is why a bot can run toward an item while shooting an enemy elsewhere.

**Navigation is self-learned, not authored** (`bot_nav.c`):
- Nodes are dropped where bots stand; links are recorded only between nodes a bot actually
  traversed (so links are bot-traversable by construction). `Nav_PenalizeLink` prunes links bots
  keep failing on. A* (`Nav_FindPath`) + the follower in `bot_move.c` execute paths.
- The graph is saved per-map to `../engine/ozbot/nav/<map>.nav` (binary), autosaved every ~30s, and
  **matures across runs** — quality improves the more the bots play. To mature a fresh map, just
  run many bots on it for a while.
- The `.nav` binary format (if a tool needs to read/write it — see `tools/demo_to_nav.py`):
  header `int magic=0x56414E4F("ONAV"), int version=1, int num_nodes`; per node `3×float origin,
  byte flags, int num_links, num_links × {int to; byte type; 3 pad; float cost}` (12 bytes/link).
- `bot_goal.c` discovers items by scanning entities with `->item` (no hardcoded coords), scores
  `value × need / distance`, picks the best **reachable** item (A*-verified), and seeds a nav node
  at each item spot on map load.

## Python tooling (`tools/`, stdlib only — now versioned in this repo)

- `run_parallel.py` — the parallel-sim harness (behind `run_parallel.bat`). Resolves the shared
  `../engine` via a triple-dirname umbrella-root walk from `tools/`.
- `analyze.py` — the main telemetry analyzer; also writes a top-down coverage/failure heatmap PNG.
- `dm2parse.py` — protocol-34 `.dm2` demo parser → recording player's trajectory + map + names.
  Works by reading each length-prefixed block only up to `svc_frame`'s playerinfo, then skipping to
  the next block (avoids parsing sound/temp-entity messages entirely).
- `demo_coverage.py` — aggregate grounded demo positions into a walkable-footprint heatmap.
- `input_view.py` — reads a `bot_inputlog` JSONL trace, segments it into jumps, and prints each
  jump's key-hold timeline / view-yaw sweep / speed curve (see "Capturing a human's inputs" above).
- `mine_indecision.py` — mines telemetry for "standing re-decide" gaps between goal exit and the
  next commit (the bot_decisive validation metric: count, duration, yaw churn).
- `mine_goals.py` — reconstructs every goal attempt from telemetry (Track E giveup analysis);
  key attempts by (file, bot) when mining merged parallel logs.
- `parity_frags.py` — sums final frags by bot-id parity: the readout for `bot_*test`
  head-to-head A/Bs (kills are the clean metric; deaths have a parity bias — see memory).
- `humanness.py` — humanness profiler: distance of bot behavior distributions from a human
  input capture (KS stats; plans/completed/humanization.md).
- `dm2_combat.py` — combat/resource extraction from pro demos. `scan`: aim turn-rate /
  weapon-at-kill / pickup patterns (combat-execution transfer was a null result — see the
  demo-combat-calibration memory). `need`: per-category health/ammo/weapon status *at pickup*
  across all maps → `demos/derived/combat_need/thresholds.json` (the source of ozbot-re's
  demo-calibrated `bot_ammoneed`/`bot_wpnneed`; see the ozbot-re-resource-need-win memory).
- `nav_add_route.py` — targeted nav surgery: merge a demo-recorded route into a .nav graph
  (kept from the lift work; wholesale demo import remains rejected).
- `nav_transplant_costs.py` — transplant learned link costs between .nav graphs (from the
  reverted A2 link-times experiment).
- `extract_demos.py` — unpack `../demos/raw/*.zip` into `../demos/sorted/<map>/` with a resumable
  JSONL manifest; never modifies `../demos/raw/`. **ozbot is the single writer of the shared
  `../demos` corpus** (fetch/extract live here only; ozbot-re consumes it read-only).
- `demo_to_nav.py` — builds a `.nav` from demos. **Note:** importing pro demos as a nav graph was
  tested and makes the bot *worse* (movement mismatch — the bot can't reproduce pro jumps/momentum);
  kept for reference. See the `ozbot-demo-import-finding` memory.
- `fetch_demos.py` — resumable downloader for the demo archive (the site's TLS cert is expired, so
  it disables verification). Writes into `../demos`.

## Gotchas worth knowing

- **A server in the canonical gamedir invalidates same-seed comparisons.** Any q2dm1 session
  running with `game ozbot` (e.g. play.bat while an agent works) autosaves `../engine/ozbot/nav/q2dm1.nav`,
  and every A/B or bit-exact gate seeds workers from that file. If the canonical dir may be live,
  pin the rig: copy the DLL + nav snapshot into a scratch source gamedir and pass
  `run_parallel --mod <dir>` (this happened 2026-07-03 and cost a debugging detour).

- **q2dm1 has no Red Armor / Quad** — its prize items are Combat Armor + the strong weapons. It
  DOES have a **Megahealth** (`item_health_mega` @ `480 1376 912`, on a trick-jump ledge ~120u up
  that this 10Hz bot can't reach — the reason it was long assumed absent; see the
  `ozbot-q2dm1-items` memory). Don't tune item logic around items the map lacks (verify a map's
  actual BSP entity lump first — `../engine/baseq2/pak1.pak → maps/q2dm1.bsp`).
- Goal-success metrics are noisy per-run; measure over longer/aggregated runs. "ITEM completion"
  (`pickups / item-goal-attempts`) is the meaningful navigation-quality number.
- When chaining `cmd /c <bat>` with PowerShell `Remove-Item` in one command, a sandbox guard can
  misfire — run the build/deploy and the file cleanup as separate commands.
- `README.md` is a real, human-facing project README (rewritten 2026-07-02; keep it in sync when
  defaults/headline results change). `CHANGELOG.md` and `src/README.md` are still **stale generic
  Quake-2-mod-template boilerplate** predating the bot — don't rely on those. The working docs
  remain this file, `PLAN.md`, and the persistent memory.
- On Windows here, `python` may resolve to the Store stub — prefer the `py` launcher. Build via the
  absolute path (`cmd /c "E:\code\projects\ozbot\ozbot\build.bat"`) since the working dir doesn't
  always propagate to subshells.
