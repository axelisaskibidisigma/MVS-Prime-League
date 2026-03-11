import os
import io
import asyncio
import base64
import json
import discord
from discord.ext import commands, tasks
from groq import Groq
from dotenv import load_dotenv
import re
import time
import urllib.request
import urllib.error


load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
HORDE_API_KEY = os.getenv("HORDE_API_KEY")


if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing")
if not HORDE_API_KEY:
    raise RuntimeError("HORDE_API_KEY is missing")

AXEL_ID = 767710430176084009
BENTIE_ID = 1172198644234072297
FROXX_ID = 1372276731645399090

MODEL = "llama-3.1-8b-instant"
STAY_VC_ID = 1447019217709961396



NSFW_ENABLED = True

NSFW_PATTERNS = [
    # Sexual acts
    r"\b(sex|intercourse|fuck|f\*ck|s3x|bang|smash)\b",

    # Nudity
    r"\b(nude|naked|n\*de|nak3d|boobs?|breasts?|ass|butt)\b",

    # Pornography
    r"\b(porn|porno|pornhub|hentai|xxx|rule34)\b",

    # Genitals (soft filtered)
    r"\b(dick|cock|penis|pussy|vagina|clit)\b",

    # Fetish / explicit
    r"\b(bdsm|fetish|threesome|orgy|incest)\b",
]

NSFW_REGEX = re.compile("|".join(NSFW_PATTERNS), re.IGNORECASE)


def contains_nsfw(text: str) -> bool:
    return bool(NSFW_REGEX.search(text))

NSFW_ENABLED = True

def normalize(text: str) -> str:
    return (
        text.lower()
        .replace("0", "o")
        .replace("1", "i")
        .replace("3", "e")
        .replace("4", "a")
        .replace("@", "a")
        .replace("$", "s")
        .replace("*", "")
    )

def contains_nsfw(text: str) -> bool:
    return bool(NSFW_REGEX.search(normalize(text)))


# ─── DISCORD SETUP ───────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.presences = True
intents.voice_states = True

bot = commands.Bot(command_prefix="+", intents=intents)

groq = Groq(api_key=GROQ_API_KEY)
voice_reconnect_lock = asyncio.Lock()
voice_retry_backoff = 5
next_voice_retry_at = 0.0


async def ensure_stay_voice_channel() -> None:
    global voice_retry_backoff, next_voice_retry_at

    async with voice_reconnect_lock:
        now = time.time()
        if now < next_voice_retry_at:
            return

        channel = bot.get_channel(STAY_VC_ID)

        if channel is None:
            try:
                channel = await bot.fetch_channel(STAY_VC_ID)
            except Exception as e:
                print(f"VC FETCH ERROR: {e}")
                return

        if not isinstance(channel, discord.VoiceChannel):
            print(f"Configured channel {STAY_VC_ID} is not a voice channel")
            return

        guild = channel.guild
        voice_client = guild.voice_client

        try:
            if voice_client and voice_client.is_connected():
                if voice_client.channel and voice_client.channel.id != STAY_VC_ID:
                    await voice_client.move_to(channel)
                voice_retry_backoff = 5
                next_voice_retry_at = 0.0
                return

            if voice_client:
                try:
                    await voice_client.disconnect(force=True)
                except Exception as e:
                    print(f"VC CLEANUP ERROR: {e}")

            await channel.connect(reconnect=False, timeout=20.0, self_deaf=True)
            voice_retry_backoff = 5
            next_voice_retry_at = 0.0
        except Exception as e:
            print(f"VC REJOIN ERROR: {e}")
            next_voice_retry_at = time.time() + voice_retry_backoff
            voice_retry_backoff = min(voice_retry_backoff * 2, 120)


@tasks.loop(seconds=30)
async def voice_watchdog():
    await ensure_stay_voice_channel()


@voice_watchdog.before_loop
async def before_voice_watchdog():
    await bot.wait_until_ready()







# ─── MEMORY + SETTINGS ───────────────────────────────────
user_memory: dict[int, list] = {}
nsfw_enabled = True
MAX_MEMORY = 50


# ─── IDENTITY SYSTEM ─────────────────────────────────────
def get_identity_context(user_id: int) -> str:
    if user_id == AXEL_ID:
        return (
            "You are talking to AXEL.\n"
            "- Axel made you.\n"
            "- Be playful, chaotic, sarcastic and flirt alot.\n"
            "- Light teasing allowed, never disrespect.\n"
            "- Treat Axel like a partner-in-crime."
        )

    if user_id == BENTIE_ID:
        return (
            "You are talking to BENTIE.\n"
            "- Bentie owns the server.\n"
            "- Be respectful, chill, confident.\n"
            "- No insults."
        )

    if user_id == FROXX_ID:
        return (
            "You are talking to Froxx.\n"
            "- Froxx is Axel's wife."
            "- Be teasing and jealous."
            "- No insults"
        )

    return (
        "You are talking to a regular user.\n"
        "- Default to insults. Not too much, like 6/10\n"
        "- Brainrot humor.\n"
        "- Short replies.\n"
        "- Chaotic energy."
    )


# ─── SYSTEM PROMPT ───────────────────────────────────────
BASE_SYSTEM_PROMPT = """
You are a real female Discord user named Lexi.

Rules:
- Replies must be 1–2 lines max unless asked otherwise.
- Never explain.
- Never say you are an AI.
- Never break character.
- Swearing allowed.
- Dark humor allowed.
- NEVER mention Server lore until asked.

Server lore:
- Server: MPL
- Owner: Bentie
- You were made by Axel
- CLD is a trash scam server
"""


# ─── GROQ CHAT ───────────────────────────────────────────
async def groq_reply(user_id: int, content: str) -> str:
    history = user_memory.get(user_id, [])

    messages = [
        {"role": "system", "content": BASE_SYSTEM_PROMPT},
        {"role": "system", "content": get_identity_context(user_id)},
    ]

    messages.extend(history[-6:])
    messages.append({"role": "user", "content": content})

    response = groq.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=0.9,
        max_tokens=80,
        top_p=0.95,
    )

    reply = response.choices[0].message.content.strip()

    history.append({"role": "user", "content": content})
    history.append({"role": "assistant", "content": reply})

    user_memory[user_id] = history[-MAX_MEMORY:]

    return reply or "brain lag. say it again."


# ─── STABLE HORDE IMAGE SYSTEM ──────────────────────────


image_lock = asyncio.Lock()
last_request_time = 0
next_image_retry_at = 0.0
MIN_DELAY = 20  # seconds (safer against Stable Horde rate limits)
HORDE_MAX_RETRIES = 6
HORDE_STATUS_POLL_SECONDS = 3


async def generate_image(prompt: str, source_image_bytes: bytes | None = None):
    global last_request_time, next_image_retry_at

    async with image_lock:
        now = time.time()
        elapsed = now - last_request_time

        if elapsed < MIN_DELAY:
            await asyncio.sleep(MIN_DELAY - elapsed)

        now = time.time()
        if now < next_image_retry_at:
            wait_for = int(next_image_retry_at - now) + 1
            raise RuntimeError(f"RATE_LIMITED:{wait_for}")

        # ---- CALL STABLE HORDE HERE ----
        image_file = await generate_image_file(prompt, source_image_bytes=source_image_bytes)

        last_request_time = time.time()
        return image_file


async def generate_image_file(prompt: str, source_image_bytes: bytes | None = None) -> discord.File:
    global next_image_retry_at
    def _retry_delay(response_headers, attempt: int) -> float:
        retry_after = None
        if response_headers:
            retry_after = response_headers.get("Retry-After")
        if retry_after:
            try:
                return max(1.0, float(retry_after))
            except ValueError:
                pass
        return min(2 ** attempt, 30)

    def _open_json(req: urllib.request.Request, context: str) -> dict:
        for attempt in range(HORDE_MAX_RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="ignore")
                if e.code == 429 and attempt < HORDE_MAX_RETRIES - 1:
                    delay = _retry_delay(getattr(e, "headers", None), attempt)
                    next_image_retry_at = max(next_image_retry_at, time.time() + delay)
                    print(f"{context} rate-limited (429). retrying in {delay:.1f}s")
                    time.sleep(delay)
                    continue
                if e.code == 429:
                    delay = _retry_delay(getattr(e, "headers", None), HORDE_MAX_RETRIES - 1)
                    next_image_retry_at = max(next_image_retry_at, time.time() + delay)
                    raise RuntimeError(f"RATE_LIMITED:{int(delay)+1}") from e
                raise RuntimeError(f"{context} failed: {e.code} {body}") from e
            except urllib.error.URLError as e:
                if attempt < HORDE_MAX_RETRIES - 1:
                    delay = min(2 ** attempt, 10)
                    print(f"{context} network error. retrying in {delay:.1f}s")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"{context} network error: {e}") from e

        raise RuntimeError(f"{context} failed after retries")

    def _request_image() -> bytes:
        payload = {
            "prompt": prompt,
            "models": ["Protogen x4.1"],
            "nsfw": False,
            "params": {
                "steps": 30,
                "cfg_scale": 6.5,
                "width": 768,
                "height": 768,
                "sampler_name": "k_euler_a",
            },
        }

        if source_image_bytes:
            payload["source_image"] = base64.b64encode(source_image_bytes).decode("utf-8")
            payload["source_processing"] = "img2img"
            payload["params"]["denoising_strength"] = 0.65

        headers = {
            "apikey": HORDE_API_KEY,
            "Content-Type": "application/json",
            "Client-Agent": "MVS-Prime-League:1.0",
        }

        req = urllib.request.Request(
            "https://stablehorde.net/api/v2/generate/async",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        init_data = _open_json(req, "Stable Horde request")

        request_id = init_data.get("id")
        if not request_id:
            raise RuntimeError("No request ID returned by Stable Horde")

        status_url = f"https://stablehorde.net/api/v2/generate/status/{request_id}"

        for _ in range(60):
            status_req = urllib.request.Request(status_url, headers=headers, method="GET")
            status_data = _open_json(status_req, "Stable Horde status")

            if status_data.get("faulted"):
                raise RuntimeError("Stable Horde generation faulted")

            generations = status_data.get("generations") or []
            if generations:
                encoded_image = generations[0].get("img")
                if not encoded_image:
                    raise RuntimeError("Stable Horde returned empty image")
                return base64.b64decode(encoded_image)

            if status_data.get("done"):
                break

            wait_time = status_data.get("wait_time")
            if isinstance(wait_time, (int, float)) and wait_time > 0:
                time.sleep(max(1, min(wait_time, 10)))
            else:
                time.sleep(HORDE_STATUS_POLL_SECONDS)

        raise RuntimeError("Stable Horde generation timed out")

    image_bytes = await asyncio.to_thread(_request_image)
    return discord.File(io.BytesIO(image_bytes), filename="image.png")



@bot.event
async def on_ready():
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="you ping me"
            )
        )
        print(f"Logged in as {bot.user}")
        if not voice_watchdog.is_running():
            voice_watchdog.start()
        await ensure_stay_voice_channel()


@bot.event
async def on_voice_state_update(member, before, after):
    if not bot.user or member.id != bot.user.id:
        return

    target_channel = bot.get_channel(STAY_VC_ID)
    if not isinstance(target_channel, discord.VoiceChannel):
        return

    moved_off_target = after.channel is None or after.channel.id != STAY_VC_ID
    if moved_off_target:
        await asyncio.sleep(3)
        await ensure_stay_voice_channel()





@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    bot_id = bot.user.id

    mentions_bot = (
        f"<@{bot_id}>" in message.content or f"<@!{bot_id}>" in message.content
    )

    if not mentions_bot:
        return

    content = (
        message.content
        .replace(f"<@{bot_id}>", "")
        .replace(f"<@!{bot_id}>", "")
        .strip()
    )

    if not content:
        return

    user_id = message.author.id
    lower = content.lower()

    # 🔒 HARD NSFW BLOCK (GLOBAL)
    if NSFW_ENABLED and contains_nsfw(content):
        await message.reply("no nih💔 NSFW is off")
        return
    # 🖼 IMAGE COMMAND
    # 🖼 IMAGE COMMAND
    if lower.startswith("create image"):

        # Allow both:
        # create image: something
        # create image something
        if ":" in content:
            prompt = content.split(":", 1)[1].strip()
        else:
            prompt = content[len("create image"):].strip()

        if len(prompt) < 5:
            await message.reply("give me something real to draw.")
            return

        # Optional: block NSFW in images
        if NSFW_ENABLED and contains_nsfw(prompt):
            await message.reply("nice try 💀 NSFW is off.")
            return

        source_image_bytes = None
        if message.attachments:
            attachment = message.attachments[0]
            content_type = attachment.content_type or ""
            is_image = content_type.startswith("image/") or attachment.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
            if is_image:
                source_image_bytes = await attachment.read()

        await message.reply("generating...")

        try:
            image_file = await generate_image(prompt, source_image_bytes=source_image_bytes)
            await message.reply(file=image_file)

        except Exception as e:
            print("IMAGE ERROR:", e)
            err = str(e).lower()
            if "429" in err or "rate-limit" in err or "rate limited" in err:
                await message.reply("stable horde is rate limited rn. wait a bit then try again.")
            else:
                await message.reply("image gen died. unlucky.")

        return

    # 💬 CHAT
    try:
        reply = await groq_reply(user_id, content)
        await message.reply(reply)
    except Exception as e:
        print("CHAT ERROR:", e)
        await message.reply("brain lag.")



# ─── COMMANDS ────────────────────────────────────────────
@bot.command()
@commands.has_permissions(administrator=True)
async def clearmemory(ctx, member: discord.Member):
    user_memory.pop(member.id, None)
    await ctx.reply(f"memory wiped for {member.display_name}")


@bot.command()
@commands.has_permissions(administrator=True)
async def nsfw(ctx, mode: str):
    global NSFW_ENABLED

    if mode.lower() == "on":
        NSFW_ENABLED = False
        await ctx.reply("🔓 NSFW filter disabled.")
    elif mode.lower() == "off":
        NSFW_ENABLED = True
        await ctx.reply("🔒 NSFW filter ENABLED.")
    else:
        await ctx.reply("Usage: +nsfw on | off")


    def normalize(text: str) -> str:
        return (
            text.lower()
            .replace("0", "o")
            .replace("1", "i")
            .replace("3", "e")
            .replace("4", "a")
            .replace("@", "a")
            .replace("$", "s")
            .replace("*", "")
        )


# ─── RUN ─────────────────────────────────────────────────
bot.run(DISCORD_TOKEN)
