# LeSharX Creator Rewards bot 🦈

Discord bot that runs the Creator Rewards seasons: takes tweet submissions
(max 2 per creator per UTC day), collects the one-time metrics snapshot at
the deadline, computes all points (engagement + weekly PFP bonuses), and
posts the leaderboard — replacing the manual tally.

## Scoring (as announced in Season 1)

| Metric | Points | How it's collected |
|---|---|---|
| Views | 1 pt per full 500 | creator enters once at the deadline |
| Like | 0.5 pt | **automatic** (daily, via X's public embed endpoint) |
| Reply | 2 pts | **automatic** (daily) |
| RT / Quote | 2 pts | creator enters once at the deadline |
| LeSharX PFP | +20 pts per week | mod-awarded weekly |

Likes and replies are fetched automatically every day at 17:00 UTC for every
submitted tweet (no X account or API key needed), so the leaderboard is live all
season. Views and RT/quote counts aren't exposed by the free endpoint — creators
add just those two numbers per tweet when the tally opens, and mods verify the
top 5. When `/tally open` runs, the bot takes a final likes/replies snapshot and
freezes it (the "read once at the deadline" rule). If a tweet is deleted
mid-season it keeps its last fetched numbers and gets flagged for mod review.

## Commands

**Creators**
- `/submit <link>` — submit a tweet (validates the link, enforces 2/day UTC, blocks duplicates)
- `/withdraw <link>` — swap out one of today's submissions
- `/mytweets` — your submissions, per-tweet points, and total
- `/report` — at the deadline: enter each tweet's final numbers from your X analytics
- `/leaderboard` — current standings (private view)

**Mods** (role-gated)
- `/tally open|close` — open the metrics-reporting window at the deadline
- `/pfp_award @member [week]` / `/pfp_revoke` — weekly +20 PFP bonuses
- `/verify @member` — mark a member's reported numbers as checked (do this for the top 5 before finalizing)
- `/post_leaderboard` — post standings publicly
- `/finalize` — lock the season and post final results (warns if metrics are missing or top-5 unverified)
- `/export` — full-season CSV for auditing

It also posts an automatic reminder every 3rd day at 18:00 UTC, and a last-day alert.

## Setup (one-time, ~10 minutes)

1. **Create the bot app:** https://discord.com/developers/applications → New Application →
   name it (e.g. "LeSharX Rewards") → Bot tab → Reset Token → copy the token (keep it secret).
   No privileged intents needed.
2. **Invite it to the server:** OAuth2 → URL Generator → scopes `bot` + `applications.commands`,
   bot permissions `Send Messages`, `Embed Links`, `Attach Files` → open the generated URL
   (needs someone with Manage Server — any admin/founder).
3. **Configure environment variables:**

   | Variable | What |
   |---|---|
   | `DISCORD_TOKEN` | the bot token |
   | `GUILD_ID` | the LeSharX server ID (right-click server → Copy Server ID, with Developer Mode on) |
   | `SUBMIT_CHANNEL_ID` | the submissions channel ID |
   | `ANNOUNCE_CHANNEL_ID` | channel for leaderboards/reminders (can be the same) |
   | `MOD_ROLE_ID` | the mod role ID (falls back to Manage Server permission if unset) |
   | `SEASON_START` / `SEASON_END` | ISO dates, e.g. `2026-08-18` / `2026-09-01` |

4. **Run it:**
   ```
   python -m pip install discord.py
   python bot.py
   ```

## Hosting

Any always-on box works. Recommended: **Railway** (the team already uses it for the
staking site) — new service from this repo, set the env vars above, done. The SQLite
database is a single file (`creator_rewards.db`); on Railway attach a volume so it
survives redeploys, or set `DB_PATH` to the volume mount.

## Season lifecycle

1. Set `SEASON_START`/`SEASON_END`, start the bot, announce the season.
2. Creators `/submit` daily; mods `/pfp_award` weekly (eyeball check of PFPs).
3. At the deadline a mod runs `/tally open` — creators `/report` their numbers once.
4. Mods `/verify` the top 5 against the actual tweets (visuals rule + numbers).
5. `/finalize` posts the results. `/export` archives the season.

## Upgrade path

If the team approves X API budget (Basic tier), the `/report` self-reporting step can be
replaced with automatic metric fetching at `/tally open` — nothing else changes.
