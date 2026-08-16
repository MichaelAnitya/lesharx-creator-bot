"""LeSharX Creator Rewards bot.

Season rules encoded here (from the Season 1 announcements + Michael's rulings):
- Submissions are daily tweet-link drops, max 2 per creator per UTC day.
- Engagement is read ONCE at the deadline: when a mod runs /tally open,
  creators self-report each tweet's numbers via /report (X analytics),
  and mods verify the prize-relevant entries with /verify.
- Scoring: floor(views/500)*1 + likes*0.5 + replies*2 + (RTs+quotes)*2
  + 20 points per week of wearing a LeSharX PFP (mod-awarded weekly).

Config via environment variables (see README).
"""
import os
import re
import csv
import io
import math
import asyncio
import sqlite3
import datetime as dt

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
SUBMIT_CHANNEL_ID = int(os.environ.get("SUBMIT_CHANNEL_ID", "0"))
ANNOUNCE_CHANNEL_ID = int(os.environ.get("ANNOUNCE_CHANNEL_ID", "0"))
MOD_ROLE_ID = int(os.environ.get("MOD_ROLE_ID", "0"))
ENV_SEASON_START = os.environ.get("SEASON_START", "2026-08-18")
ENV_SEASON_END = os.environ.get("SEASON_END", "2026-09-01")
DB_PATH = os.environ.get("DB_PATH", "creator_rewards.db")

DAILY_CAP = 2
PFP_WEEKLY_PTS = 20.0
TWEET_RE = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)")

BLUE = 0x38A9E4
GOLD = 0xF4C95D


# ---------- storage ----------

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  user_name TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  tweet_id TEXT NOT NULL UNIQUE,
  day TEXT NOT NULL,
  created_at TEXT NOT NULL,
  views INTEGER, likes INTEGER, replies INTEGER, rtq INTEGER,
  reported_at TEXT,
  verified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pfp_awards (
  user_id INTEGER NOT NULL,
  week INTEGER NOT NULL,
  PRIMARY KEY (user_id, week)
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
""")
db.commit()
for _col in ("auto_fetched_at TEXT", "fetch_error TEXT", "author_handle TEXT", "has_media INTEGER", "mention_ok INTEGER", "needs_review INTEGER DEFAULT 0", "author_avatar TEXT", "season INTEGER DEFAULT 1"):
    try:
        db.execute(f"ALTER TABLE submissions ADD COLUMN {_col}")
        db.commit()
    except sqlite3.OperationalError:
        pass
if "season" not in [r["name"] for r in db.execute("PRAGMA table_info(pfp_awards)").fetchall()]:
    db.executescript("""
    CREATE TABLE pfp_awards_new (
      user_id INTEGER NOT NULL, week INTEGER NOT NULL, season INTEGER NOT NULL DEFAULT 1,
      PRIMARY KEY (user_id, week, season)
    );
    INSERT INTO pfp_awards_new (user_id, week, season) SELECT user_id, week, 1 FROM pfp_awards;
    DROP TABLE pfp_awards;
    ALTER TABLE pfp_awards_new RENAME TO pfp_awards;
    """)
    db.commit()


def meta_get(key, default=""):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(key, value):
    db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


# ---------- season helpers ----------

def today_utc():
    return dt.datetime.now(dt.timezone.utc).date()


def cur_season():
    return int(meta_get("season", "1"))


def season_start():
    return dt.date.fromisoformat(meta_get("season_start", ENV_SEASON_START))


def season_end():
    return dt.date.fromisoformat(meta_get("season_end", ENV_SEASON_END))


def season_active():
    return season_start() <= today_utc() <= season_end() and meta_get("finalized") != "1"


def tally_open():
    return meta_get("tally") == "open" and meta_get("finalized") != "1"


def current_week():
    return max(0, (today_utc() - season_start()).days // 7)


def tweet_points(views, likes, replies, rtq):
    return ((views or 0) // 500) * 1.0 + (likes or 0) * 0.5 + (replies or 0) * 2.0 + (rtq or 0) * 2.0


def user_points(user_id):
    rows = db.execute("SELECT views, likes, replies, rtq FROM submissions WHERE user_id=? AND needs_review=0 AND season=?",
                      (user_id, cur_season())).fetchall()
    pts = sum(tweet_points(r["views"], r["likes"], r["replies"], r["rtq"]) for r in rows)
    pfp_weeks = db.execute("SELECT COUNT(*) AS c FROM pfp_awards WHERE user_id=? AND season=?", (user_id, cur_season())).fetchone()["c"]
    return pts + pfp_weeks * PFP_WEEKLY_PTS, pfp_weeks


def leaderboard_rows():
    users = db.execute("SELECT DISTINCT user_id, user_name FROM submissions WHERE season=?", (cur_season(),)).fetchall()
    out = []
    for u in users:
        pts, pfp_weeks = user_points(u["user_id"])
        args = (u["user_id"], cur_season())
        n_total = db.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND season=?", args).fetchone()["c"]
        n_rep = db.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND season=? AND reported_at IS NOT NULL", args).fetchone()["c"]
        n_ver = db.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND season=? AND verified=1", args).fetchone()["c"]
        out.append({"user_id": u["user_id"], "name": u["user_name"], "pts": pts,
                    "pfp_weeks": pfp_weeks, "total": n_total, "reported": n_rep, "verified": n_ver})
    out.sort(key=lambda r: r["pts"], reverse=True)
    return out


def fmt_pts(p):
    return f"{p:g}" if p == int(p) else f"{p:.1f}"


# ---------- automatic metrics ----------
# Three free sources, in order of completeness:
#   1. fxtwitter  — views, likes, replies, RTs, quotes, author, media, text
#   2. vxtwitter  — likes, replies, RTs (fallback)
#   3. X syndication endpoint — likes, replies only (last resort)
# All community/embed services; if they break, the bot alerts mods and
# /report opens as the manual fallback. Nothing here scrapes x.com itself.

def _syn_token(tweet_id):
    n = (int(tweet_id) / 1e15) * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    ip, frac = int(n), n - int(n)
    a = ""
    while ip:
        a = digits[ip % 36] + a
        ip //= 36
    f = ""
    for _ in range(12):
        frac *= 36
        d = int(frac)
        f += digits[d]
        frac -= d
    return (a + f).replace("0", "").replace(".", "")


_HDRS = {"User-Agent": "Mozilla/5.0"}
_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def _get_json(session, url):
    try:
        async with session.get(url, headers=_HDRS, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                return None, f"http {resp.status}"
            return await resp.json(content_type=None), None
    except Exception as e:
        return None, type(e).__name__


async def fetch_tweet_metrics(session, tweet_id):
    """Returns ({views, likes, replies, rtq, author, has_media, mention_ok}, err).
    Fields the winning source doesn't expose are None (never overwrite with None)."""
    data, err = await _get_json(session, f"https://api.fxtwitter.com/status/{tweet_id}")
    if data and data.get("code") == 200 and data.get("tweet"):
        t = data["tweet"]
        text = (t.get("text") or "").lower()
        return {
            "views": t.get("views"),
            "likes": t.get("likes") or 0,
            "replies": t.get("replies") or 0,
            "rtq": (t.get("retweets") or 0) + (t.get("quotes") or 0),
            "author": (t.get("author") or {}).get("screen_name"),
            "avatar": (t.get("author") or {}).get("avatar_url"),
            "has_media": 1 if t.get("media") else 0,
            "mention_ok": 1 if "@lesharxverse" in text else 0,
        }, None
    if data and data.get("code") == 404:
        return None, "deleted or not found"

    data, err2 = await _get_json(session, f"https://api.vxtwitter.com/i/status/{tweet_id}")
    if data and data.get("likes") is not None:
        text = (data.get("text") or "").lower()
        return {
            "views": data.get("viewCount"),
            "likes": data.get("likes") or 0,
            "replies": data.get("replies") or 0,
            "rtq": data.get("retweets") or 0,
            "author": data.get("user_screen_name"),
            "avatar": data.get("user_profile_image_url"),
            "has_media": 1 if data.get("mediaURLs") else 0,
            "mention_ok": 1 if "@lesharxverse" in text else 0,
        }, None

    url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token={_syn_token(tweet_id)}"
    data, err3 = await _get_json(session, url)
    if data:
        if data.get("__typename") == "TweetTombstone":
            return None, "deleted or restricted"
        if data.get("favorite_count") is not None or data.get("conversation_count") is not None:
            return {
                "views": None,
                "likes": int(data.get("favorite_count") or 0),
                "replies": int(data.get("conversation_count") or 0),
                "rtq": None,
                "author": ((data.get("user") or {}).get("screen_name")),
                "avatar": ((data.get("user") or {}).get("profile_image_url_https")),
                "has_media": 1 if data.get("mediaDetails") else None,
                "mention_ok": None,
            }, None
    return None, err or err2 or err3 or "all sources failed"


async def fetch_all_metrics():
    """Refresh likes/replies for every submission. Returns (ok, failed)."""
    rows = db.execute("SELECT id, tweet_id FROM submissions WHERE season=?", (cur_season(),)).fetchall()
    ok = failed = 0
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    async with aiohttp.ClientSession() as session:
        for r in rows:
            try:
                metrics, err = await fetch_tweet_metrics(session, r["tweet_id"])
            except Exception as e:
                metrics, err = None, str(e)[:80]
            if metrics:
                db.execute("""UPDATE submissions SET
                                views=COALESCE(?, views), likes=COALESCE(?, likes),
                                replies=COALESCE(?, replies), rtq=COALESCE(?, rtq),
                                author_handle=COALESCE(?, author_handle),
                                author_avatar=COALESCE(?, author_avatar),
                                has_media=COALESCE(?, has_media), mention_ok=COALESCE(?, mention_ok),
                                auto_fetched_at=?, fetch_error=NULL WHERE id=?""",
                           (metrics["views"], metrics["likes"], metrics["replies"], metrics["rtq"],
                            metrics["author"], metrics["avatar"], metrics["has_media"], metrics["mention_ok"], now, r["id"]))
                ok += 1
            else:
                db.execute("UPDATE submissions SET fetch_error=? WHERE id=?", (err, r["id"]))
                failed += 1
            db.commit()
            await asyncio.sleep(1.2)
    return ok, failed


# ---------- bot ----------

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
guild_obj = discord.Object(id=GUILD_ID)


def is_mod(member):
    if MOD_ROLE_ID:
        return any(r.id == MOD_ROLE_ID for r in getattr(member, "roles", []))
    return member.guild_permissions.manage_guild


def mod_check(inter):
    if not is_mod(inter.user):
        raise app_commands.CheckFailure("mod-only")
    return True


# ---------- creator commands ----------

@tree.command(name="submit", description="Submit one of today's tweets (max 2 per day)", guild=guild_obj)
@app_commands.describe(link="Link to your tweet on X")
async def submit(inter: discord.Interaction, link: str):
    if not season_active():
        await inter.response.send_message("Submissions are closed — no active season right now.", ephemeral=True)
        return
    m = TWEET_RE.match(link.strip())
    if not m:
        await inter.response.send_message("That doesn't look like a tweet link. Format: `https://x.com/yourname/status/123...`", ephemeral=True)
        return
    handle, tweet_id = m.group(1), m.group(2)
    canonical = f"https://x.com/{handle}/status/{tweet_id}"
    day = today_utc().isoformat()
    dupe = db.execute("SELECT user_id FROM submissions WHERE tweet_id=?", (tweet_id,)).fetchone()
    if dupe:
        who = "you" if dupe["user_id"] == inter.user.id else "someone else"
        await inter.response.send_message(f"That tweet was already submitted by {who}.", ephemeral=True)
        return
    count = db.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND day=? AND season=?",
                       (inter.user.id, day, cur_season())).fetchone()["c"]
    if count >= DAILY_CAP:
        await inter.response.send_message(
            f"You've already submitted {DAILY_CAP} tweets today (UTC). Use `/withdraw` to swap one out.", ephemeral=True)
        return

    await inter.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        try:
            metrics, err = await fetch_tweet_metrics(session, tweet_id)
        except Exception as e:
            metrics, err = None, str(e)[:80]

    if metrics is None and err == "deleted or not found":
        await inter.followup.send("That tweet doesn't seem to exist (deleted, private account, or wrong link).", ephemeral=True)
        return
    if metrics and metrics["has_media"] == 0:
        await inter.followup.send("Rule check: that tweet has **no image, GIF, or video** — visuals are mandatory. Post with a visual and resubmit.", ephemeral=True)
        return
    if metrics and metrics["mention_ok"] == 0:
        await inter.followup.send("Rule check: that tweet doesn't **tag @LeSharXverse**. Tag us and resubmit.", ephemeral=True)
        return

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    db.execute("INSERT INTO submissions (user_id, user_name, url, tweet_id, day, created_at, season) VALUES (?,?,?,?,?,?,?)",
               (inter.user.id, str(inter.user), canonical, tweet_id, day, now, cur_season()))
    if metrics:
        db.execute("""UPDATE submissions SET views=?, likes=?, replies=?, rtq=?, author_handle=?,
                      author_avatar=?, has_media=?, mention_ok=?, auto_fetched_at=? WHERE tweet_id=?""",
                   (metrics["views"], metrics["likes"], metrics["replies"], metrics["rtq"],
                    metrics["author"], metrics["avatar"], metrics["has_media"], metrics["mention_ok"], now, tweet_id))
    else:
        db.execute("UPDATE submissions SET fetch_error=? WHERE tweet_id=?", (err, tweet_id))
    db.commit()
    left = DAILY_CAP - count - 1
    note = "✅ Rules checked (visual + tag) — metrics tracking has started." if metrics else \
           "⚠️ Couldn't auto-check this tweet right now — a mod may verify it manually."
    await inter.followup.send(
        f"🦈 Submission {count + 1}/{DAILY_CAP} logged for today: {canonical}\n{note}\n"
        f"{left} slot{'s' if left != 1 else ''} left today. Quality > quantity!", ephemeral=True)
    if SUBMIT_CHANNEL_ID:
        ch = client.get_channel(SUBMIT_CHANNEL_ID)
        if ch:
            await ch.send(f"📬 {inter.user.mention} submitted: {canonical}", suppress_embeds=True)


@tree.command(name="withdraw", description="Withdraw one of today's submissions", guild=guild_obj)
@app_commands.describe(link="The tweet link to withdraw")
async def withdraw(inter: discord.Interaction, link: str):
    m = TWEET_RE.match(link.strip())
    if not m:
        await inter.response.send_message("That doesn't look like a tweet link.", ephemeral=True)
        return
    day = today_utc().isoformat()
    cur = db.execute("DELETE FROM submissions WHERE user_id=? AND tweet_id=? AND day=? AND season=? AND reported_at IS NULL",
                     (inter.user.id, m.group(2), day, cur_season()))
    db.commit()
    if cur.rowcount:
        await inter.response.send_message("Withdrawn. You can `/submit` a replacement today.", ephemeral=True)
    else:
        await inter.response.send_message("Couldn't find that link among your submissions from today (UTC).", ephemeral=True)


@tree.command(name="mytweets", description="See your submissions and points this season", guild=guild_obj)
async def mytweets(inter: discord.Interaction):
    rows = db.execute("SELECT * FROM submissions WHERE user_id=? AND season=? ORDER BY day", (inter.user.id, cur_season())).fetchall()
    if not rows:
        await inter.response.send_message("No submissions yet this season. `/submit` your first tweet!", ephemeral=True)
        return
    pts, pfp_weeks = user_points(inter.user.id)
    lines = []
    for r in rows[-12:]:
        p = tweet_points(r["views"], r["likes"], r["replies"], r["rtq"])
        if r["needs_review"]:
            state = "0 pts — under mod review"
        elif r["verified"]:
            state = f"{fmt_pts(p)} pts ✅"
        else:
            state = f"{fmt_pts(p)} pts" + ("" if tally_open() else " (live)")
        lines.append(f"`{r['day']}` <{r['url']}> · {state}")
    e = discord.Embed(title="Your season", colour=BLUE, description="\n".join(lines))
    e.add_field(name="Total points", value=fmt_pts(pts))
    e.add_field(name="PFP weeks", value=f"{pfp_weeks} (+{fmt_pts(pfp_weeks * PFP_WEEKLY_PTS)})")
    e.add_field(name="Submissions", value=str(len(rows)))
    await inter.response.send_message(embed=e, ephemeral=True)




REVIEW_SQL = "SELECT * FROM submissions WHERE needs_review=1 AND season=? ORDER BY user_name, day"


@tree.command(name="leaderboard", description="Current season standings", guild=guild_obj)
async def leaderboard(inter: discord.Interaction):
    await inter.response.send_message(embed=leaderboard_embed(), ephemeral=True)


def leaderboard_embed(final=False):
    rows = leaderboard_rows()
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows[:15]):
        tag = medals[i] if i < 3 else f"{i + 1}."
        ver = " ✅" if r["reported"] and r["verified"] == r["reported"] else ""
        lines.append(f"{tag} <@{r['user_id']}> — **{fmt_pts(r['pts'])}** pts "
                     f"({r['total']} tweets, {r['pfp_weeks']}w PFP){ver}")
    if not lines:
        lines = ["No submissions yet. `/submit` your first tweet! 🦈"]
    e = discord.Embed(
        title="🏆 Final results — Creator Rewards" if final else "🏆 Creator Rewards leaderboard",
        colour=GOLD if final else BLUE,
        description="\n".join(lines))
    if not final:
        e.set_footer(text=f"Season {cur_season()}: {season_start()} → {season_end()} · all metrics tracked automatically daily · "
                          "final snapshot at the deadline · ✅ = mod-verified")
    return e


# ---------- mod commands ----------

@tree.command(name="tally", description="Mod: open or close the metrics-reporting window", guild=guild_obj)
@app_commands.describe(state="open or close")
@app_commands.choices(state=[app_commands.Choice(name="open", value="open"), app_commands.Choice(name="close", value="close")])
@app_commands.check(mod_check)
async def tally(inter: discord.Interaction, state: app_commands.Choice[str]):
    meta_set("tally", state.value)
    await inter.response.send_message(f"Tally is now **{state.value}**." +
        (" Taking the final likes/replies snapshot…" if state.value == "open" else ""), ephemeral=True)
    ch = client.get_channel(ANNOUNCE_CHANNEL_ID or SUBMIT_CHANNEL_ID)
    if state.value == "open":
        ok, failed = await fetch_all_metrics()
        flagged = db.execute(
            "UPDATE submissions SET needs_review=1 WHERE season=" + str(cur_season()) + " AND verified=0 AND "
            "(fetch_error IS NOT NULL OR views IS NULL OR likes IS NULL OR replies IS NULL OR rtq IS NULL)").rowcount
        db.commit()
        if ch:
            await ch.send(f"📊 **The tally is OPEN!** Final metrics for {ok} tweets were snapshotted automatically — "
                          "views, likes, replies, and RTs+quotes. Nothing left to do for creators; mods verify the top entries before prizes."
                          + (f"\n⚠️ {flagged} tweet(s) couldn't be read and score 0 pending mod review." if flagged else ""))
        if flagged:
            await inter.followup.send(f"{flagged} tweet(s) flagged for review — run `/review` to see them, then `/award` to restore points.", ephemeral=True)
    elif ch:
        await ch.send("The tally window is closed.")


class PFPButton(discord.ui.Button):
    def __init__(self, user_id, name, awarded):
        super().__init__(
            label=(f"✅ {name}" if awarded else f"+20 · {name}")[:80],
            style=discord.ButtonStyle.success if not awarded else discord.ButtonStyle.secondary,
            disabled=awarded)
        self.target_id = user_id
        self.target_name = name

    async def callback(self, inter: discord.Interaction):
        try:
            db.execute("INSERT INTO pfp_awards (user_id, week, season) VALUES (?, ?, ?)", (self.target_id, current_week(), cur_season()))
            db.commit()
        except sqlite3.IntegrityError:
            pass
        self.label = f"✅ {self.target_name}"[:80]
        self.style = discord.ButtonStyle.secondary
        self.disabled = True
        await inter.response.edit_message(view=self.view)


@tree.command(name="creator_list", description="Mod: weekly PFP checklist — avatars + one-click awards", guild=guild_obj)
@app_commands.check(mod_check)
async def creator_list(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    users = db.execute("SELECT DISTINCT user_id, user_name FROM submissions WHERE season=? ORDER BY user_name", (cur_season(),)).fetchall()
    if not users:
        await inter.followup.send("No creators yet this season.", ephemeral=True)
        return
    week = current_week()
    entries = []
    for u in users:
        latest = db.execute("SELECT * FROM submissions WHERE user_id=? AND season=? ORDER BY created_at DESC LIMIT 1",
                            (u["user_id"], cur_season())).fetchone()
        awarded = db.execute("SELECT 1 FROM pfp_awards WHERE user_id=? AND week=? AND season=?",
                             (u["user_id"], week, cur_season())).fetchone() is not None
        entries.append((u, latest, awarded))
    await inter.followup.send(
        f"**Week {week + 1} PFP checklist** — {len(entries)} creator(s). Avatars refresh with the daily fetch; "
        "eyeball each one and click to award. Profile links are ground truth for disputes.", ephemeral=True)
    for i in range(0, len(entries), 5):
        chunk = entries[i:i + 5]
        embeds, view = [], discord.ui.View(timeout=900)
        for u, latest, awarded in chunk:
            e = discord.Embed(colour=BLUE, title=u["user_name"])
            desc = []
            if latest["author_handle"]:
                desc.append(f"[@{latest['author_handle']}](https://x.com/{latest['author_handle']}) on X")
            desc.append(f"[latest post]({latest['url']}) · {latest['day']}")
            desc.append("PFP bonus: " + ("✅ awarded this week" if awarded else "⬜ pending"))
            e.description = "\n".join(desc)
            if latest["author_avatar"]:
                e.set_thumbnail(url=latest["author_avatar"])
            embeds.append(e)
            view.add_item(PFPButton(u["user_id"], u["user_name"].split("#")[0], awarded))
        await inter.followup.send(embeds=embeds, view=view, ephemeral=True)


@tree.command(name="pfp_award", description="Mod: award this week's +20 PFP bonus to a member", guild=guild_obj)
@app_commands.describe(member="Member wearing a LeSharX PFP", week="Week number (0-based, default: current)")
@app_commands.check(mod_check)
async def pfp_award(inter: discord.Interaction, member: discord.Member, week: int = -1):
    w = current_week() if week < 0 else week
    try:
        db.execute("INSERT INTO pfp_awards (user_id, week, season) VALUES (?, ?, ?)", (member.id, w, cur_season()))
        db.commit()
        await inter.response.send_message(f"+{fmt_pts(PFP_WEEKLY_PTS)} PFP bonus to {member.mention} for week {w + 1}. 🦈", ephemeral=True)
    except sqlite3.IntegrityError:
        await inter.response.send_message(f"{member.mention} already has the week {w + 1} bonus.", ephemeral=True)


@tree.command(name="pfp_revoke", description="Mod: remove a PFP bonus", guild=guild_obj)
@app_commands.check(mod_check)
async def pfp_revoke(inter: discord.Interaction, member: discord.Member, week: int):
    cur = db.execute("DELETE FROM pfp_awards WHERE user_id=? AND week=? AND season=?", (member.id, week, cur_season()))
    db.commit()
    await inter.response.send_message("Removed." if cur.rowcount else "No such award.", ephemeral=True)


@tree.command(name="verify", description="Mod: mark a member's reported metrics as checked", guild=guild_obj)
@app_commands.check(mod_check)
async def verify(inter: discord.Interaction, member: discord.Member):
    cur = db.execute("UPDATE submissions SET verified=1 WHERE user_id=? AND season=? AND reported_at IS NOT NULL",
                     (member.id, cur_season()))
    db.commit()
    await inter.response.send_message(f"Marked {cur.rowcount} reported tweet(s) from {member.mention} as verified ✅", ephemeral=True)


@tree.command(name="post_leaderboard", description="Mod: post the leaderboard publicly", guild=guild_obj)
@app_commands.check(mod_check)
async def post_leaderboard(inter: discord.Interaction):
    ch = client.get_channel(ANNOUNCE_CHANNEL_ID or SUBMIT_CHANNEL_ID) or inter.channel
    await ch.send(embed=leaderboard_embed())
    await inter.response.send_message("Posted.", ephemeral=True)


@tree.command(name="finalize", description="Mod: lock the season and post final results", guild=guild_obj)
@app_commands.check(mod_check)
async def finalize(inter: discord.Interaction):
    unreported = db.execute("SELECT COUNT(*) AS c FROM submissions WHERE needs_review=1 AND season=?", (cur_season(),)).fetchone()["c"]
    unverified_top = [r for r in leaderboard_rows()[:5] if r["reported"] and r["verified"] < r["reported"]]
    warnings = []
    if unreported:
        warnings.append(f"{unreported} submission(s) still under review (they score 0 — `/review`)")
    if unverified_top:
        warnings.append(f"top-5 members not fully verified: {', '.join(r['name'] for r in unverified_top)}")
    if warnings and meta_get("finalize_confirm") != "1":
        meta_set("finalize_confirm", "1")
        await inter.response.send_message(
            "⚠️ " + " · ".join(warnings) + "\nRun `/finalize` again to post final results anyway.", ephemeral=True)
        return
    meta_set("finalized", "1")
    meta_set("finalize_confirm", "0")
    ch = client.get_channel(ANNOUNCE_CHANNEL_ID or SUBMIT_CHANNEL_ID) or inter.channel
    await ch.send("# LeSharX CREATOR REWARDS — FINAL RESULTS 🦈", embed=leaderboard_embed(final=True))
    await ch.send("**Winners, please open a ticket!** 🎁\n\nThe ocean rewards those who contribute. LFJAWS 🦈")
    await inter.response.send_message("Season finalized and results posted.", ephemeral=True)


@tree.command(name="review", description="Mod: list tweets that need manual review", guild=guild_obj)
@app_commands.check(mod_check)
async def review(inter: discord.Interaction):
    rows = db.execute(REVIEW_SQL, (cur_season(),)).fetchall()
    if not rows:
        await inter.response.send_message("Nothing needs review. 🦈", ephemeral=True)
        return
    lines = []
    for r in rows[:20]:
        known = f"last known: {r['views'] or 0} views / {r['likes'] or 0} likes / {r['replies'] or 0} replies / {r['rtq'] or 0} RTs" \
            if r["auto_fetched_at"] else "never fetched"
        when = f" (fetched {r['auto_fetched_at'][:10]})" if r["auto_fetched_at"] else ""
        lines.append(f"• <@{r['user_id']}> <{r['url']}>\n  ↳ {r['fetch_error'] or 'missing metrics'} · {known}{when}")
    msg = ("**Tweets under review** — each scores 0 until ruled on. If it's legit, `/award` its "
           "real numbers (or the last-known ones below). If it's dead or unverifiable, `/forfeit` it. "
           "Both close the review and are stamped in the export.\n\n" + "\n".join(lines))
    await inter.response.send_message(msg[:1990], ephemeral=True)


@tree.command(name="award", description="Mod: manually set a reviewed tweet's metrics", guild=guild_obj)
@app_commands.describe(link="The flagged tweet's link", views="Views", likes="Likes", replies="Replies", rt_quotes="Retweets + quotes")
@app_commands.check(mod_check)
async def award(inter: discord.Interaction, link: str, views: int, likes: int, replies: int, rt_quotes: int):
    m = TWEET_RE.match(link.strip())
    if not m:
        await inter.response.send_message("That doesn't look like a tweet link.", ephemeral=True)
        return
    if min(views, likes, replies, rt_quotes) < 0:
        await inter.response.send_message("Metrics can't be negative.", ephemeral=True)
        return
    row = db.execute("SELECT * FROM submissions WHERE tweet_id=? AND season=?", (m.group(2), cur_season())).fetchone()
    if not row:
        await inter.response.send_message("No submission with that link.", ephemeral=True)
        return
    db.execute("""UPDATE submissions SET views=?, likes=?, replies=?, rtq=?, needs_review=0, verified=1,
                  reported_at=?, fetch_error=NULL WHERE id=?""",
               (views, likes, replies, rt_quotes,
                dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), row["id"]))
    db.commit()
    p = tweet_points(views, likes, replies, rt_quotes)
    left = db.execute("SELECT COUNT(*) AS c FROM submissions WHERE needs_review=1 AND season=?", (cur_season(),)).fetchone()["c"]
    await inter.response.send_message(
        f"Awarded: <@{row['user_id']}>'s tweet now scores **{fmt_pts(p)} pts** (marked verified). "
        f"{left} still under review." , ephemeral=True)


@tree.command(name="forfeit", description="Mod: rule a reviewed tweet forfeited (0 points)", guild=guild_obj)
@app_commands.describe(link="The flagged tweet's link")
@app_commands.check(mod_check)
async def forfeit(inter: discord.Interaction, link: str):
    m = TWEET_RE.match(link.strip())
    if not m:
        await inter.response.send_message("That doesn't look like a tweet link.", ephemeral=True)
        return
    row = db.execute("SELECT * FROM submissions WHERE tweet_id=? AND season=?", (m.group(2), cur_season())).fetchone()
    if not row:
        await inter.response.send_message("No submission with that link.", ephemeral=True)
        return
    db.execute("""UPDATE submissions SET views=0, likes=0, replies=0, rtq=0, needs_review=0, verified=1,
                  reported_at=? WHERE id=?""",
               (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), row["id"]))
    db.commit()
    left = db.execute("SELECT COUNT(*) AS c FROM submissions WHERE needs_review=1 AND season=?", (cur_season(),)).fetchone()["c"]
    await inter.response.send_message(
        f"Forfeited: <@{row['user_id']}>'s tweet scores 0 (recorded as a mod ruling). {left} still under review.", ephemeral=True)


@tree.command(name="fetch", description="Mod: refresh likes/replies for all submissions now", guild=guild_obj)
@app_commands.check(mod_check)
async def fetch_now(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    ok, failed = await fetch_all_metrics()
    msg = f"Refreshed {ok} tweet(s)."
    if failed:
        bad = db.execute("SELECT url, fetch_error FROM submissions WHERE fetch_error IS NOT NULL AND season=? LIMIT 10", (cur_season(),)).fetchall()
        msg += f" {failed} failed:\n" + "\n".join(f"• <{b['url']}> — {b['fetch_error']}" for b in bad)
    await inter.followup.send(msg, ephemeral=True)


@tree.command(name="export", description="Mod: export a season's data as CSV", guild=guild_obj)
@app_commands.describe(season="Season number (default: current). Past seasons stay archived and exportable.")
@app_commands.check(mod_check)
async def export(inter: discord.Interaction, season: int = 0):
    season = season or cur_season()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user", "user_id", "day", "url", "views", "likes", "replies", "rt_quotes", "tweet_pts",
                "author", "auto_fetched_at", "manually_awarded_at", "verified", "needs_review", "fetch_error"])
    for r in db.execute("SELECT * FROM submissions WHERE season=? ORDER BY user_name, day", (season,)).fetchall():
        pts = 0 if r["needs_review"] else tweet_points(r["views"], r["likes"], r["replies"], r["rtq"])
        w.writerow([r["user_name"], r["user_id"], r["day"], r["url"], r["views"], r["likes"], r["replies"], r["rtq"],
                    pts, r["author_handle"], r["auto_fetched_at"], r["reported_at"], r["verified"],
                    r["needs_review"], r["fetch_error"]])
    w.writerow([])
    w.writerow(["user_id", "pfp_week"])
    for r in db.execute("SELECT * FROM pfp_awards WHERE season=?", (season,)).fetchall():
        w.writerow([r["user_id"], r["week"]])
    buf.seek(0)
    await inter.response.send_message(
        file=discord.File(io.BytesIO(buf.getvalue().encode()), filename=f"creator_rewards_s{season}.csv"), ephemeral=True)


class NewSeasonConfirm(discord.ui.View):
    def __init__(self, start, end, discard):
        super().__init__(timeout=120)
        self.params = (start, end, discard)

    @discord.ui.button(label="Confirm — start new season", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, button: discord.ui.Button):
        start, end, discard = self.params
        old = cur_season()
        if discard:
            db.execute("DELETE FROM submissions WHERE season=?", (old,))
            db.execute("DELETE FROM pfp_awards WHERE season=?", (old,))
        meta_set("season", str(old + 1))
        meta_set("season_start", start)
        meta_set("season_end", end)
        meta_set("tally", "closed")
        meta_set("finalized", "0")
        meta_set("finalize_confirm", "0")
        db.commit()
        button.disabled = True
        button.label = "Done"
        await inter.response.edit_message(
            content=f"Season {old} {'**discarded**' if discard else f'archived (export anytime with `/export season:{old}`)'} — "
                    f"**Season {old + 1}** runs {start} → {end}. Fresh leaderboard, fresh submissions. 🦈", view=self)
        ch = client.get_channel(ANNOUNCE_CHANNEL_ID or SUBMIT_CHANNEL_ID)
        if ch and not discard:
            await ch.send(f"# 🦈 CREATOR REWARDS — SEASON {old + 1}\nRunning **{start} → {end}**. "
                          f"Submissions open with `/submit` on day one. LFJAWS!")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
        for c in self.children:
            c.disabled = True
        await inter.response.edit_message(content="Cancelled — nothing changed.", view=self)


@tree.command(name="season_extend", description="Mod: move the current season's end date (extend or end early)", guild=guild_obj)
@app_commands.describe(end="New last day, YYYY-MM-DD (inclusive — submissions close at midnight UTC after it)")
@app_commands.check(mod_check)
async def season_extend(inter: discord.Interaction, end: str):
    try:
        new_end = dt.date.fromisoformat(end)
        if new_end <= season_start():
            raise ValueError
    except ValueError:
        await inter.response.send_message("Date must be YYYY-MM-DD and after the season start.", ephemeral=True)
        return
    old_end = season_end()
    meta_set("season_end", end)
    extended = new_end > old_end
    await inter.response.send_message(
        f"Season {cur_season()} end date: {old_end} → **{new_end}**"
        + ("" if extended else " (season shortened — submissions close after that day)"), ephemeral=True)
    ch = client.get_channel(ANNOUNCE_CHANNEL_ID or SUBMIT_CHANNEL_ID)
    if ch and extended:
        await ch.send(f"# 📅 DEADLINE EXTENDED\nThe Creator Rewards season now runs until **{new_end}** — "
                      f"more days, more posts, more points. Keep that LeSharX PFP on. LFJAWS 🦈")


@tree.command(name="season_new", description="Mod: archive (or discard) the current season and start a new one", guild=guild_obj)
@app_commands.describe(
    start="New season's first day, YYYY-MM-DD",
    end="New season's last day, YYYY-MM-DD",
    discard="Permanently DELETE the current season's data instead of archiving (for test runs)")
@app_commands.check(mod_check)
async def season_new(inter: discord.Interaction, start: str, end: str, discard: bool = False):
    try:
        s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        if e <= s:
            raise ValueError
    except ValueError:
        await inter.response.send_message("Dates must be YYYY-MM-DD with end after start.", ephemeral=True)
        return
    n_subs = db.execute("SELECT COUNT(*) AS c FROM submissions WHERE season=?", (cur_season(),)).fetchone()["c"]
    action = ("⚠️ **PERMANENTLY DELETE** the current season's data "
              if discard else "Archive the current season (kept forever, exportable) ")
    await inter.response.send_message(
        f"Starting **Season {cur_season() + 1}** ({start} → {end}).\n{action}— "
        f"{n_subs} submission(s) affected. Confirm?",
        view=NewSeasonConfirm(start, end, discard), ephemeral=True)


@tree.error
async def on_app_error(inter: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await inter.response.send_message("Mod-only command.", ephemeral=True)
    else:
        raise error


# ---------- scheduled jobs ----------

@tasks.loop(time=dt.time(hour=17, minute=0, tzinfo=dt.timezone.utc))
async def daily_fetch():
    """Daily auto-refresh of likes/replies (frozen once the tally opens)."""
    if not season_active() or tally_open():
        return
    ok, failed = await fetch_all_metrics()
    total = ok + failed
    if total and failed / total > 0.2:
        ch = client.get_channel(ANNOUNCE_CHANNEL_ID or SUBMIT_CHANNEL_ID)
        if ch:
            await ch.send(f"⚠️ Mods: the automatic metrics check failed for {failed}/{total} tweets — "
                          "the endpoint may have changed. `/fetch` to retry, or fall back to manual reporting.")


@tasks.loop(time=dt.time(hour=18, minute=0, tzinfo=dt.timezone.utc))
async def daily_reminder():
    if not season_active() or not (ANNOUNCE_CHANNEL_ID or SUBMIT_CHANNEL_ID):
        return
    ch = client.get_channel(ANNOUNCE_CHANNEL_ID or SUBMIT_CHANNEL_ID)
    if not ch:
        return
    days_left = (season_end() - today_utc()).days
    if days_left == 0:
        await ch.send("⏰ **LAST DAY of the Creator Rewards season!** Get your final `/submit` in before midnight UTC. 🦈")
    elif days_left % 3 == 0:
        await ch.send(f"🦈 Creator Rewards: **{days_left} days left**. `/submit` today's best tweets — max 2, quality > quantity!")


@client.event
async def on_ready():
    await tree.sync(guild=guild_obj)
    if not daily_reminder.is_running():
        daily_reminder.start()
    if not daily_fetch.is_running():
        daily_fetch.start()
    print(f"Logged in as {client.user} · season {cur_season()}: {season_start()} → {season_end()}")


client.run(TOKEN)
