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
import sqlite3
import datetime as dt

import discord
from discord import app_commands
from discord.ext import tasks

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ["GUILD_ID"])
SUBMIT_CHANNEL_ID = int(os.environ.get("SUBMIT_CHANNEL_ID", "0"))
ANNOUNCE_CHANNEL_ID = int(os.environ.get("ANNOUNCE_CHANNEL_ID", "0"))
MOD_ROLE_ID = int(os.environ.get("MOD_ROLE_ID", "0"))
SEASON_START = dt.date.fromisoformat(os.environ.get("SEASON_START", "2026-08-18"))
SEASON_END = dt.date.fromisoformat(os.environ.get("SEASON_END", "2026-09-01"))
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


def meta_get(key, default=""):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(key, value):
    db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


# ---------- season helpers ----------

def today_utc():
    return dt.datetime.now(dt.timezone.utc).date()


def season_active():
    return SEASON_START <= today_utc() <= SEASON_END and meta_get("finalized") != "1"


def tally_open():
    return meta_get("tally") == "open" and meta_get("finalized") != "1"


def current_week():
    return max(0, (today_utc() - SEASON_START).days // 7)


def tweet_points(views, likes, replies, rtq):
    return (views // 500) * 1.0 + likes * 0.5 + replies * 2.0 + rtq * 2.0


def user_points(user_id):
    rows = db.execute("SELECT views, likes, replies, rtq FROM submissions WHERE user_id=? AND reported_at IS NOT NULL", (user_id,)).fetchall()
    pts = sum(tweet_points(r["views"], r["likes"], r["replies"], r["rtq"]) for r in rows)
    pfp_weeks = db.execute("SELECT COUNT(*) AS c FROM pfp_awards WHERE user_id=?", (user_id,)).fetchone()["c"]
    return pts + pfp_weeks * PFP_WEEKLY_PTS, pfp_weeks


def leaderboard_rows():
    users = db.execute("SELECT DISTINCT user_id, user_name FROM submissions").fetchall()
    out = []
    for u in users:
        pts, pfp_weeks = user_points(u["user_id"])
        n_total = db.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=?", (u["user_id"],)).fetchone()["c"]
        n_rep = db.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND reported_at IS NOT NULL", (u["user_id"],)).fetchone()["c"]
        n_ver = db.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND verified=1", (u["user_id"],)).fetchone()["c"]
        out.append({"user_id": u["user_id"], "name": u["user_name"], "pts": pts,
                    "pfp_weeks": pfp_weeks, "total": n_total, "reported": n_rep, "verified": n_ver})
    out.sort(key=lambda r: r["pts"], reverse=True)
    return out


def fmt_pts(p):
    return f"{p:g}" if p == int(p) else f"{p:.1f}"


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
    count = db.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND day=?", (inter.user.id, day)).fetchone()["c"]
    if count >= DAILY_CAP:
        await inter.response.send_message(
            f"You've already submitted {DAILY_CAP} tweets today (UTC). Use `/withdraw` to swap one out.", ephemeral=True)
        return
    db.execute("INSERT INTO submissions (user_id, user_name, url, tweet_id, day, created_at) VALUES (?,?,?,?,?,?)",
               (inter.user.id, str(inter.user), canonical, tweet_id, day,
                dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
    db.commit()
    left = DAILY_CAP - count - 1
    await inter.response.send_message(
        f"🦈 Submission {count + 1}/{DAILY_CAP} logged for today: {canonical}\n"
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
    cur = db.execute("DELETE FROM submissions WHERE user_id=? AND tweet_id=? AND day=? AND reported_at IS NULL",
                     (inter.user.id, m.group(2), day))
    db.commit()
    if cur.rowcount:
        await inter.response.send_message("Withdrawn. You can `/submit` a replacement today.", ephemeral=True)
    else:
        await inter.response.send_message("Couldn't find that link among your submissions from today (UTC).", ephemeral=True)


@tree.command(name="mytweets", description="See your submissions and points this season", guild=guild_obj)
async def mytweets(inter: discord.Interaction):
    rows = db.execute("SELECT * FROM submissions WHERE user_id=? ORDER BY day", (inter.user.id,)).fetchall()
    if not rows:
        await inter.response.send_message("No submissions yet this season. `/submit` your first tweet!", ephemeral=True)
        return
    pts, pfp_weeks = user_points(inter.user.id)
    lines = []
    for r in rows[-12:]:
        if r["reported_at"]:
            p = tweet_points(r["views"], r["likes"], r["replies"], r["rtq"])
            state = f"{fmt_pts(p)} pts" + (" ✅" if r["verified"] else "")
        else:
            state = "awaiting metrics" if tally_open() else "—"
        lines.append(f"`{r['day']}` <{r['url']}> · {state}")
    e = discord.Embed(title="Your season", colour=BLUE, description="\n".join(lines))
    e.add_field(name="Total points", value=fmt_pts(pts))
    e.add_field(name="PFP weeks", value=f"{pfp_weeks} (+{fmt_pts(pfp_weeks * PFP_WEEKLY_PTS)})")
    e.add_field(name="Submissions", value=str(len(rows)))
    await inter.response.send_message(embed=e, ephemeral=True)


class MetricsModal(discord.ui.Modal):
    def __init__(self, sub_id, url):
        super().__init__(title="Tweet metrics (from your X analytics)")
        self.sub_id = sub_id
        self.views = discord.ui.TextInput(label="Views", placeholder="e.g. 3400")
        self.likes = discord.ui.TextInput(label="Likes", placeholder="e.g. 42")
        self.replies = discord.ui.TextInput(label="Replies", placeholder="e.g. 7")
        self.rtq = discord.ui.TextInput(label="Retweets + Quotes", placeholder="e.g. 11")
        for item in (self.views, self.likes, self.replies, self.rtq):
            self.add_item(item)

    async def on_submit(self, inter: discord.Interaction):
        try:
            vals = [int(str(x.value).replace(",", "").strip()) for x in (self.views, self.likes, self.replies, self.rtq)]
            if any(v < 0 for v in vals):
                raise ValueError
        except ValueError:
            await inter.response.send_message("Numbers only, please — try `/report` again.", ephemeral=True)
            return
        db.execute("UPDATE submissions SET views=?, likes=?, replies=?, rtq=?, reported_at=?, verified=0 WHERE id=?",
                   (*vals, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), self.sub_id))
        db.commit()
        p = tweet_points(*vals)
        await inter.response.send_message(f"Recorded: **{fmt_pts(p)} points** for that tweet. Run `/report` again for your next one.", ephemeral=True)


class ReportPicker(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=300)
        rows = db.execute(
            "SELECT id, url, day FROM submissions WHERE user_id=? AND reported_at IS NULL ORDER BY day LIMIT 25",
            (user_id,)).fetchall()
        options = [discord.SelectOption(label=f"{r['day']} — …{r['url'][-24:]}", value=str(r["id"])) for r in rows]
        self.select = discord.ui.Select(placeholder="Pick a tweet to enter metrics for", options=options)
        self.select.callback = self.picked
        self.add_item(self.select)
        self.urls = {str(r["id"]): r["url"] for r in rows}

    async def picked(self, inter: discord.Interaction):
        sid = self.select.values[0]
        await inter.response.send_modal(MetricsModal(int(sid), self.urls[sid]))


@tree.command(name="report", description="Enter your tweets' final metrics (deadline only)", guild=guild_obj)
async def report(inter: discord.Interaction):
    if not tally_open():
        await inter.response.send_message("The tally isn't open yet — mods open it at the deadline. Hold your numbers until then!", ephemeral=True)
        return
    pending = db.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND reported_at IS NULL", (inter.user.id,)).fetchone()["c"]
    if not pending:
        await inter.response.send_message("All your submissions have metrics. You're done — leaderboard shows the rest. 🦈", ephemeral=True)
        return
    await inter.response.send_message(
        f"{pending} tweet{'s' if pending != 1 else ''} awaiting metrics. Open each tweet's analytics on X and copy the numbers as of now.",
        view=ReportPicker(inter.user.id), ephemeral=True)


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
        e.set_footer(text=f"Season {SEASON_START} → {SEASON_END} · metrics snapshot at deadline · ✅ = mod-verified")
    return e


# ---------- mod commands ----------

@tree.command(name="tally", description="Mod: open or close the metrics-reporting window", guild=guild_obj)
@app_commands.describe(state="open or close")
@app_commands.choices(state=[app_commands.Choice(name="open", value="open"), app_commands.Choice(name="close", value="close")])
@app_commands.check(mod_check)
async def tally(inter: discord.Interaction, state: app_commands.Choice[str]):
    meta_set("tally", state.value)
    if state.value == "open":
        msg = ("📊 **The tally is OPEN!** The season is over — open each of your submitted tweets' analytics on X and "
               "run `/report` here to enter views, likes, replies, and RTs+quotes for every tweet. "
               "Mods will verify the top entries before prizes.")
    else:
        msg = "The tally window is closed."
    await inter.response.send_message(f"Tally is now **{state.value}**.", ephemeral=True)
    ch = client.get_channel(ANNOUNCE_CHANNEL_ID or SUBMIT_CHANNEL_ID)
    if ch:
        await ch.send(msg)


@tree.command(name="pfp_award", description="Mod: award this week's +20 PFP bonus to a member", guild=guild_obj)
@app_commands.describe(member="Member wearing a LeSharX PFP", week="Week number (0-based, default: current)")
@app_commands.check(mod_check)
async def pfp_award(inter: discord.Interaction, member: discord.Member, week: int = -1):
    w = current_week() if week < 0 else week
    try:
        db.execute("INSERT INTO pfp_awards (user_id, week) VALUES (?, ?)", (member.id, w))
        db.commit()
        await inter.response.send_message(f"+{fmt_pts(PFP_WEEKLY_PTS)} PFP bonus to {member.mention} for week {w + 1}. 🦈", ephemeral=True)
    except sqlite3.IntegrityError:
        await inter.response.send_message(f"{member.mention} already has the week {w + 1} bonus.", ephemeral=True)


@tree.command(name="pfp_revoke", description="Mod: remove a PFP bonus", guild=guild_obj)
@app_commands.check(mod_check)
async def pfp_revoke(inter: discord.Interaction, member: discord.Member, week: int):
    cur = db.execute("DELETE FROM pfp_awards WHERE user_id=? AND week=?", (member.id, week))
    db.commit()
    await inter.response.send_message("Removed." if cur.rowcount else "No such award.", ephemeral=True)


@tree.command(name="verify", description="Mod: mark a member's reported metrics as checked", guild=guild_obj)
@app_commands.check(mod_check)
async def verify(inter: discord.Interaction, member: discord.Member):
    cur = db.execute("UPDATE submissions SET verified=1 WHERE user_id=? AND reported_at IS NOT NULL", (member.id,))
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
    unreported = db.execute("SELECT COUNT(*) AS c FROM submissions WHERE reported_at IS NULL").fetchone()["c"]
    unverified_top = [r for r in leaderboard_rows()[:5] if r["reported"] and r["verified"] < r["reported"]]
    warnings = []
    if unreported:
        warnings.append(f"{unreported} submission(s) still have no metrics (they score 0)")
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


@tree.command(name="export", description="Mod: export all season data as CSV", guild=guild_obj)
@app_commands.check(mod_check)
async def export(inter: discord.Interaction):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user", "user_id", "day", "url", "views", "likes", "replies", "rt_quotes", "tweet_pts", "reported_at", "verified"])
    for r in db.execute("SELECT * FROM submissions ORDER BY user_name, day").fetchall():
        pts = tweet_points(r["views"], r["likes"], r["replies"], r["rtq"]) if r["reported_at"] else ""
        w.writerow([r["user_name"], r["user_id"], r["day"], r["url"], r["views"], r["likes"], r["replies"], r["rtq"],
                    pts, r["reported_at"], r["verified"]])
    w.writerow([])
    w.writerow(["user_id", "pfp_week"])
    for r in db.execute("SELECT * FROM pfp_awards").fetchall():
        w.writerow([r["user_id"], r["week"]])
    buf.seek(0)
    await inter.response.send_message(
        file=discord.File(io.BytesIO(buf.getvalue().encode()), filename="creator_rewards.csv"), ephemeral=True)


@tree.error
async def on_app_error(inter: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await inter.response.send_message("Mod-only command.", ephemeral=True)
    else:
        raise error


# ---------- scheduled reminder ----------

@tasks.loop(time=dt.time(hour=18, minute=0, tzinfo=dt.timezone.utc))
async def daily_reminder():
    if not season_active() or not (ANNOUNCE_CHANNEL_ID or SUBMIT_CHANNEL_ID):
        return
    ch = client.get_channel(ANNOUNCE_CHANNEL_ID or SUBMIT_CHANNEL_ID)
    if not ch:
        return
    days_left = (SEASON_END - today_utc()).days
    if days_left == 0:
        await ch.send("⏰ **LAST DAY of the Creator Rewards season!** Get your final `/submit` in before midnight UTC. 🦈")
    elif days_left % 3 == 0:
        await ch.send(f"🦈 Creator Rewards: **{days_left} days left**. `/submit` today's best tweets — max 2, quality > quantity!")


@client.event
async def on_ready():
    await tree.sync(guild=guild_obj)
    if not daily_reminder.is_running():
        daily_reminder.start()
    print(f"Logged in as {client.user} · season {SEASON_START} → {SEASON_END}")


client.run(TOKEN)
