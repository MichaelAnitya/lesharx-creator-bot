# LeSharX Creator Rewards bot 🦈

Discord bot that runs the Creator Rewards seasons: takes tweet submissions
(max 2 per creator per UTC day), collects the one-time metrics snapshot at
the deadline, computes all points (engagement + weekly PFP bonuses), and
posts the leaderboard — replacing the manual tally.

## Scoring (as announced in Season 1)

| Metric | Points | How it's collected |
|---|---|---|
| Views | 1 pt per full 500 | **automatic** (daily) |
| Like | 0.5 pt | **automatic** (daily) |
| Reply | 2 pts | **automatic** (daily) |
| RT / Quote | 2 pts | **automatic** (daily) |
| LeSharX PFP | +20 pts per week | mod-awarded weekly |

All four engagement metrics are fetched automatically every day at 17:00 UTC for
every submitted tweet — no X account or API key needed — so the leaderboard is
live all season and creators never report numbers. Sources, in fallback order:
fxtwitter → vxtwitter → X's syndication endpoint (the same public services that
power tweet embeds; the bot never scrapes x.com itself). `/submit` also checks
the rules live: the tweet must exist, contain a visual, and tag @LeSharXverse —
non-compliant tweets are rejected on the spot with the reason.

When `/tally open` runs, the bot takes one final snapshot and freezes it (the
"read once at the deadline" rule). Tweets no source can read at the snapshot
(deleted, gone private, or endpoints down) are flagged: they **score 0** and
appear in `/review` with their last-known numbers. A mod checks each one and
either restores it with `/award` or leaves it forfeited. Creators never enter
metrics — every manual number comes from a mod and is stamped in the export.
The bot alerts mods if the daily fetch failure rate exceeds 20%.

## Commands

**Creators** (submit a link — that's the whole job)
- `/submit <link>` — submit a tweet (live rule checks: exists, has a visual, tags @LeSharXverse; enforces 2/day UTC; blocks duplicates)
- `/withdraw <link>` — swap out one of today's submissions
- `/mytweets` — your submissions, per-tweet points, and total
- `/leaderboard` — current standings (private view)

**Mods** (role-gated)
- `/creator_list` — the weekly PFP checklist: every creator with their current X avatar shown inline, profile + latest-post links, this week's bonus status, and a one-click "+20" award button each
- `/tally open|close` — take the final metrics snapshot at the deadline; unreadable tweets are flagged, score 0, and go to review
- `/review` — list flagged tweets with last-known numbers
- `/award <link> <views> <likes> <replies> <rt_quotes>` — manually set a reviewed tweet's metrics (points flow through the normal formula; marked verified). For deleted-but-legit tweets, use the last-known numbers from `/review`.
- `/forfeit <link>` — rule a reviewed tweet forfeited: 0 points, review closed, decision stamped in the export
- `/pfp_award @member [week]` / `/pfp_revoke` — weekly +20 PFP bonuses
- `/verify @member` — mark a member's reported numbers as checked (do this for the top 5 before finalizing)
- `/post_leaderboard` — post standings publicly
- `/finalize` — lock the season and post final results (warns if metrics are missing or top-5 unverified)
- `/export [season]` — CSV audit dump for the current or any archived season
- `/season_new start end [discard]` — start the next season (confirm-button gated). Default **archives** the
  finished season forever; `discard: true` permanently deletes the current season's data instead — use that
  to wipe test runs before a real launch. Season dates are set here, in Discord; the env vars only seed the
  first season.

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
