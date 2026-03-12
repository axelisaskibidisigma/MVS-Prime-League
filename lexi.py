import os
import io
import asyncio
import discord
from discord.ext import commands, tasks
from groq import Groq
from dotenv import load_dotenv
import re
import time
import aiohttp
from urllib.parse import quote_plus


load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY")


if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing")
if not POLLINATIONS_API_KEY:
    raise RuntimeError("POLLINATIONS_API_KEY is missing")

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


async def get_stay_voice_channel() -> discord.VoiceChannel | None:
    channel = bot.get_channel(STAY_VC_ID)

    if channel is None:
        try:
            channel = await bot.fetch_channel(STAY_VC_ID)
        except Exception as e:
            print(f"VC FETCH ERROR: {e}")
            return None

    if not isinstance(channel, discord.VoiceChannel):
        print(f"Configured channel {STAY_VC_ID} is not a voice channel")
        return None

    return channel


async def ensure_stay_voice_channel() -> None:
    async with voice_reconnect_lock:
        channel = await get_stay_voice_channel()
        if channel is None:
            return

        guild = channel.guild
        voice_client = guild.voice_client

        try:
            if voice_client and voice_client.is_connected():
                if voice_client.channel and voice_client.channel.id != STAY_VC_ID:
                    await voice_client.move_to(channel)
                return

            if voice_client:
                try:
                    await voice_client.disconnect(force=True)
                except Exception:
                    pass

            await channel.connect(reconnect=True, self_deaf=True)
        except Exception as e:
            print(f"VC REJOIN ERROR: {e}")


@tasks.loop(seconds=15)
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


# ─── POLLINATIONS IMAGE SYSTEM ──────────────────────────


image_lock = asyncio.Lock()
last_request_time = 0
MIN_DELAY = 15  # seconds (safe for 5 RPM)


async def generate_image(prompt):
    global last_request_time

    async with image_lock:
        now = time.time()
        elapsed = now - last_request_time

        if elapsed < MIN_DELAY:
            await asyncio.sleep(MIN_DELAY - elapsed)

        # ---- CALL POLLINATIONS HERE ----
        image_file = await generate_image_file(prompt)

        last_request_time = time.time()
        return image_file


async def generate_image_file(prompt: str) -> discord.File:
    headers = {
        "Authorization": f"Bearer {POLLINATIONS_API_KEY}",
        "X-API-Key": POLLINATIONS_API_KEY,
    }

    encoded_prompt = quote_plus(prompt)
    image_url = (
        f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        "?model=flux&nologo=true&private=true&width=768&height=768"
    )

    async with aiohttp.ClientSession() as session:
        async with session.get(image_url, headers=headers, timeout=60) as response:
            if response.status >= 400:
                error_body = await response.text()
                raise RuntimeError(f"Pollinations generation failed ({response.status}): {error_body}")

            img_bytes = await response.read()

    if len(img_bytes) < 1000:
        raise RuntimeError("Pollinations returned an unexpectedly small image")

    print("Pollinations model used: flux")
    return discord.File(io.BytesIO(img_bytes), filename="image.png")




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
async def on_disconnect():
    # Gateway disconnects can drop VC; watchdog/on_ready will recover, but
    # we also trigger an immediate best-effort reconnect when possible.
    if bot.is_ready():
        await ensure_stay_voice_channel()


@bot.event
async def on_voice_state_update(member, before, after):
    if not bot.user or member.id != bot.user.id:
        return

    target_channel = await get_stay_voice_channel()
    if target_channel is None:
        return

    moved_off_target = after.channel is None or after.channel.id != STAY_VC_ID
    if moved_off_target:
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

        await message.reply("generating...")

        try:
            image_file = await generate_image(prompt)
            await message.reply(file=image_file)

        except Exception as e:
            print("IMAGE ERROR:", e)
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
