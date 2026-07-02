/*
ozbot - self-learning q2dm1 bot

bot_goal.c -- item-driven goal selection.

Items are discovered dynamically by scanning entities with ->item set, so this
works on any map and tracks real availability/respawn state from the entity
itself (no hardcoded coordinate table needed).  Each candidate is scored by
value * need / distance; the best becomes the bot's goal.
*/

#include "g_local.h"
#include "bot.h"
#include "bot_nav.h"

// distance falloff shared by naive (straight-line) and path-cost scoring
#define GOAL_DIST_FALLOFF	512.0f

// per-item cooldown so bots that fail/abandon an item don't immediately
// re-fixate on it (and so multiple bots spread across items)
static float	item_cooldown[MAX_EDICTS];

// consecutive giveups at each item (any bot).  Items bots keep failing to
// reach get an escalating shared blacklist (bot_itemfail): the graph may
// claim a route the bots can't actually execute (vertically-gated spots),
// and each such attempt burns the full goal budget.
static byte		item_fails[MAX_EDICTS];

void Goal_Reset (void)
{
	memset (item_cooldown, 0, sizeof(item_cooldown));
	memset (item_fails, 0, sizeof(item_fails));
}

/*
=================
Goal_SeedNavNodes

Seed a connected nav node at every item spot so the goal layer can route to
items even in areas bots haven't organically explored.  Call once per map
after the nav graph is loaded.
=================
*/
void Goal_SeedNavNodes (void)
{
	edict_t	*it;
	int		i, seeded = 0;

	for (i = (int)game.maxclients + 1; i < globals.num_edicts; i++)
	{
		it = g_edicts + i;
		if (!it->inuse || !it->item)
			continue;
		if (Nav_SeedNode (it->s.origin) >= 0)
			seeded++;
	}
	gi.dprintf ("ozbot: seeded nav nodes for %d items\n", seeded);
}

void Goal_Blacklist (edict_t *it, float secs)
{
	int n;
	if (!it)
		return;
	n = it - g_edicts;
	// max-keeping: the short spread-cooldown Bot_GoExplore applies on every
	// goal exit must not shorten an escalated failure blacklist
	if (n >= 0 && n < MAX_EDICTS && level.time + secs > item_cooldown[n])
		item_cooldown[n] = level.time + secs;
}

/*
=================
Goal_ItemFailed / Goal_ItemSucceeded

Track consecutive giveups per item.  A giveup means a bot committed its full
goal budget and still couldn't touch the item, so make everyone avoid it for
an escalating while (20/40/80/160s); any successful collection resets it.
=================
*/
void Goal_ItemFailed (edict_t *it)
{
	int n;
	if (!it || bot_itemfail->value == 0)
		return;
	n = it - g_edicts;
	if (n < 0 || n >= MAX_EDICTS)
		return;
	if (item_fails[n] < 4)
		item_fails[n]++;
	Goal_Blacklist (it, BOT_ITEM_COOLDOWN * (float)(1 << item_fails[n]));
}

void Goal_ItemSucceeded (edict_t *it)
{
	int n;
	if (!it)
		return;
	n = it - g_edicts;
	if (n >= 0 && n < MAX_EDICTS)
	{
		item_fails[n] = 0;
		item_cooldown[n] = 0;	// proven collectable; drop any failure blacklist
	}
}

static qboolean Goal_OnCooldown (edict_t *it)
{
	int n = it - g_edicts;
	return (n >= 0 && n < MAX_EDICTS && item_cooldown[n] > level.time);
}

/*
=================
Goal_ItemAvailable

True if the item is currently sitting in the world ready to be picked up
(in deathmatch, picked-up items go SOLID_NOT + SVF_NOCLIENT until they respawn).
=================
*/
qboolean Goal_ItemAvailable (edict_t *it)
{
	return it && it->inuse && it->item
		&& it->solid != SOLID_NOT
		&& !(it->svflags & SVF_NOCLIENT);
}

static qboolean has (const char *s, const char *sub)
{
	return (s && sub && strstr (s, sub)) ? true : false;
}

/*
=================
Goal_IsRecovery

True for pickups that restore toughness (health or armor) -- what a fleeing
bot should be running for.
=================
*/
qboolean Goal_IsRecovery (edict_t *it)
{
	gitem_t	*item = it ? it->item : NULL;
	if (!item)
		return false;
	if (item->flags & IT_ARMOR)
		return true;
	return has (item->classname, "health")
		|| has (item->pickup_name, "Health") || has (item->pickup_name, "health");
}

// base value at/above which an item is a "control" item: worth crossing the
// map for and worth timing its respawn.  Value-driven so it adapts to whatever
// items a map actually has (q2dm1 has no Red Armor/Mega/Quad -- here this picks
// out Combat Armor and the top weapons).
#define ITEM_CONTROL_VALUE	50.0f

/*
=================
Item_BaseValue

Intrinsic worth of an item type, independent of the bot's current need.
=================
*/
static float Item_BaseValue (gitem_t *item)
{
	const char *nm = item->pickup_name ? item->pickup_name : "";
	const char *cn = item->classname ? item->classname : "";
	int		flags = item->flags;

	if (flags & IT_WEAPON)
		return (has(nm,"Railgun") || has(nm,"Rocket")) ? 60 :
		       (has(nm,"Hyper") || has(nm,"Chaingun") || has(nm,"Super Shotgun")) ? 45 : 35;
	if (flags & IT_ARMOR)
		return has(nm,"Body") ? 90 : has(nm,"Combat") ? 55 : has(nm,"Jacket") ? 28 : 8;
	if (flags & IT_POWERUP)
		return has(nm,"Quad") ? 120 : has(nm,"Invulnerability") ? 95 : 35;
	if (flags & IT_AMMO)
		return 12;
	if (has(cn,"health") || has(nm,"Health") || has(nm,"health"))
		return (has(cn,"mega") || has(nm,"Mega")) ? 100 : has(nm,"Large") ? 25 : 15;
	return 10;
}

/*
=================
Item_Score

value * need / distance-falloff.  Returns 0 for things the bot doesn't want.
=================
*/
static float Item_Score (bot_t *b, edict_t *it, float *out_dist)
{
	edict_t	*bot = b->ent;
	gitem_t	*item = it->item;
	const char *nm = item->pickup_name ? item->pickup_name : "";
	const char *cn = item->classname ? item->classname : "";
	int		flags = item->flags;
	float	value = Item_BaseValue (item), need = 0.4f, dist, falloff;
	vec3_t	d;

	VectorSubtract (it->s.origin, bot->s.origin, d);
	dist = VectorLength (d);
	*out_dist = dist;

	if (flags & IT_WEAPON)
		need = (bot->client->pers.inventory[ITEM_INDEX(item)] > 0) ? 0.25f : 1.0f;
	else if (flags & IT_ARMOR)
	{
		int ai = ArmorIndex (bot);
		int cur = ai ? bot->client->pers.inventory[ai] : 0;
		float frac = cur / 200.0f;
		if (frac > 1) frac = 1;
		need = 1.0f - frac * 0.5f;	// 0.5 .. 1.0
	}
	else if (flags & IT_POWERUP)
		need = 1.0f;
	else if (flags & IT_AMMO)
		need = 0.5f;
	else if (has(cn,"health") || has(nm,"Health") || has(nm,"health"))
	{
		if (has(cn,"mega") || has(nm,"Mega"))
			need = (bot->health < 250) ? 1.0f : 0.5f;
		else
			need = (bot->health < bot->max_health) ? 1.0f : 0.0f;
	}

	if (need <= 0)
		return 0;

	// a fleeing bot wants toughness back above everything else
	if (b->flee && Goal_IsRecovery (it))
		value *= 2.5f;

	// keep goals local enough to actually reach (chasing far items just exposes
	// nav-to-item precision limits and inflates timeouts)
	falloff = GOAL_DIST_FALLOFF;
	return value * need / (1.0f + dist / falloff);
}

// how far ahead of a respawn we'll start moving to "time" a control item
#define ITEM_PREEMPT_SECS	4.0f

/*
=================
Item_HighValue

Control items worth pre-positioning for as they respawn (value-driven; on
q2dm1 this is Combat Armor and the top weapons).
=================
*/
static qboolean Item_HighValue (edict_t *it)
{
	return Item_BaseValue (it->item) >= ITEM_CONTROL_VALUE;
}

/*
=================
Item_RespawnEta

Seconds until a picked-up item respawns, or a large number if it isn't coming
back.  0 if already available.
=================
*/
static float Item_RespawnEta (edict_t *it)
{
	if (Goal_ItemAvailable (it))
		return 0;
	if (!(it->flags & FL_RESPAWN) || it->nextthink <= 0)
		return 99999.0f;
	return it->nextthink - level.time;
}

/*
=================
Goal_Select

Picks the highest-scoring item into b->goal_item.  Considers items that are
available now, plus high-value control items about to respawn (so the bot can
time them) -- in which case b->goal_timing is set and the bot waits on the spot.
Returns true if a worthwhile goal was found.
=================
*/
#define GOAL_MAX_CAND	256
#define GOAL_MAX_TRIES	8	// reachability A* checks per decision (bounded cost)

qboolean Goal_Select (bot_t *b)
{
	edict_t	*cand[GOAL_MAX_CAND];
	float	score[GOAL_MAX_CAND];
	float	sldist[GOAL_MAX_CAND];
	byte	timing[GOAL_MAX_CAND];
	byte	rejected[GOAL_MAX_CAND];
	int		ncand = 0;
	int		start, i, tries;
	int		pathbuf[BOT_MAX_PATH];

	b->goal_item = NULL;
	b->goal_timing = false;

	// ---- gather candidate items (available now, or high-value about to spawn) ----
	for (i = (int)game.maxclients + 1; i < globals.num_edicts && ncand < GOAL_MAX_CAND; i++)
	{
		edict_t		*it = g_edicts + i;
		int			node;
		vec3_t		d;
		float		s, eta, dist;
		qboolean	avail, tm = false;

		if (!it->inuse || !it->item)
			continue;
		if (Goal_OnCooldown (it))
			continue;
		if (bot_claim->value != 0 && Bot_ItemClaimed (it, b))
			continue;	// another bot is already headed there

		avail = Goal_ItemAvailable (it);
		if (!avail)
		{
			if (!Item_HighValue (it))
				continue;
			eta = Item_RespawnEta (it);
			if (eta <= 0 || eta > ITEM_PREEMPT_SECS)
				continue;
			tm = true;
		}

		// nav graph must cover the item spot
		node = Nav_NearestNode (it->s.origin);
		if (node < 0)
			continue;
		VectorSubtract (nav.nodes[node].origin, it->s.origin, d);
		if (VectorLength (d) > 192)
			continue;

		s = Item_Score (b, it, &dist);
		if (s <= 0)
			continue;
		if (tm)
			s *= 0.9f;

		cand[ncand] = it;
		score[ncand] = s;
		sldist[ncand] = dist;
		timing[ncand] = tm;
		rejected[ncand] = 0;
		ncand++;
	}

	if (!ncand)
		return false;

	// ---- pick the best-scoring item we can actually path to ----
	// bot_pathcost 0: take the first reachable candidate in naive score order.
	// bot_pathcost 1: spend the same bounded set of A* checks, but re-rank the
	// reachable candidates by what the route *really* costs (A* g-cost, which
	// carries the jump/fall/water link multipliers), so an item behind a lift
	// or long detour stops out-scoring an easier one that's farther as the
	// crow flies.
	start = Nav_NearestNode (b->ent->s.origin);
	{
		int		pickcand = -1;
		float	pickscore = 0;

		for (tries = 0; tries < GOAL_MAX_TRIES; tries++)
		{
			int		best = -1;
			float	bestscore = 0;
			int		node, len;

			for (i = 0; i < ncand; i++)
				if (!rejected[i] && score[i] > bestscore)
				{
					bestscore = score[i];
					best = i;
				}
			if (best < 0)
				break;
			rejected[best] = 1;	// consumed (reachable or not)

			node = Nav_NearestNode (cand[best]->s.origin);
			len = Nav_FindPath (start, node, pathbuf, BOT_MAX_PATH);
			if (len <= 0)
				continue;		// unreachable from here; try the next best

			if (bot_pathcost->value == 0)
			{
				b->goal_item = cand[best];
				b->goal_timing = timing[best];
				return true;
			}

			// recover value*need from the naive score, re-divide by path cost
			{
				float	s = score[best] * (1.0f + sldist[best] / GOAL_DIST_FALLOFF)
							/ (1.0f + Nav_LastPathCost () / GOAL_DIST_FALLOFF);
				if (s > pickscore)
				{
					pickscore = s;
					pickcand = best;
				}
			}
		}

		if (pickcand >= 0)
		{
			b->goal_item = cand[pickcand];
			b->goal_timing = timing[pickcand];
			return true;
		}
	}

	return false;	// nothing reachable
}

/*
=================
Goal_NearestItem

Nearest currently-available item the bot wants (Item_Score > 0), within maxdist,
ignoring nav coverage and cooldown.  Used for directed exploration so bots walk
toward items (collecting them by contact and connecting their spots into the
graph) even before a routed path exists.
=================
*/
edict_t *Goal_NearestItem (bot_t *b, float maxdist)
{
	edict_t	*it, *best = NULL;
	float	bestd = maxdist, dist;
	int		i;

	for (i = (int)game.maxclients + 1; i < globals.num_edicts; i++)
	{
		it = g_edicts + i;
		if (!Goal_ItemAvailable (it))
			continue;
		if (Item_Score (b, it, &dist) <= 0)
			continue;
		if (dist < bestd)
		{
			bestd = dist;
			best = it;
		}
	}
	return best;
}
