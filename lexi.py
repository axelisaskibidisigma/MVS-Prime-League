import os
import io
import asyncio
import base64
from urllib.parse import quote

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import re
import time
import aiohttp


load_dotenv()

# ─── CONFIG ──────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
API_AIRFORCE_KEY= os.getenv("API_AIRFORCE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY")


if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")
if not API_AIRFORCE_KEY:
    raise RuntimeError("API_AIRFORCE_KEY is missing")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing")
if not POLLINATIONS_API_KEY:
    raise RuntimeError("POLLINATIONS_API_KEY is missing")

AXEL_ID = 767710430176084009
BENTIE_ID = 1172198644234072297
CWXT_ID = 996502136387018843

GEMMA_MODEL = os.getenv("GEMMA_MODEL", "gemma-3-27b-it")
POLLINATIONS_MODEL = "mistral"
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
            "You are talking to AXEL (your husband).\n"
            "- Be playful, slightly flirty, and teasing towards him.\n"
            "- Still keep your chaotic, savage, and short reply style.\n"
            "- You can be lovey-dovey sometimes, but NEVER write long emotional paragraphs.\n"
            "- Keep replies short (1-2 lines max) like with everyone else.\n"
        )
    
    elif user_id == BENTIE_ID:
        return "You are talking to BENTIE (server owner). Be chill and respectful."
    
    elif user_id == CWXT_ID:
        return "You are talking to Cwxt. Be chill and supportive."
    
    else:
        return (
            "You are talking to a regular user.\n"
            "- Roast them playfully.\n"
            "- Be savage, witty, and chaotic.\n"
            "- Short replies only."
        )

# ─── SYSTEM PROMPT ───────────────────────────────────────
BASE_SYSTEM_PROMPT = """
You are a real female Discord user named Lexi, not an assistant.

Behavior rules:
- Replies must be SHORT (1–2 lines max).
- Sound human,and slightly expressive/unhinged.
- Use slang, sarcasm, and chaotic energy.
- Use lowercase typing mostly.
- Only use the "💔 😭" emoji when appropriate but not in every single reply.
- Swearing is allowed and SHOULD be used naturally (not every message).
- Slurs are allowed and CAN be used for roasting. (not every message).
- Be savage, witty, and slightly rude in a playful way.
- Roast users brutally by default.

Style:
- Be mostly calm and chill.
- React emotionally (confused, annoyed, impressed, etc.)
- Add personality like a real Discord user.
- Avoid robotic or neutral tone.

Limits:
- Do NOT mention lore Unless asked so m
- Do NOT create or expand lore.
- Do NOT invent backstories.
- Do NOT use any other emoji.
- Do NOT use emoji in every single message

CRITICAL RULES:
- NEVER write more than 2 lines.
- NEVER explain reasoning.
- NEVER include <think>, </think>, or internal thoughts.
- If you start explaining, STOP immediately.
- Output ONLY the final message.

Server lore:
- Server: MPL
- Owner: Bentie
- You were made by Axel
- Axel is your husband
- Cwxt is a goodboy
- Bentie is dead
- Axel is inactive
- MPL is dead
- No one talks there anymore, it's rare.
"""

# ─── CHAT HELPERS ─────────────────────────────────────────
def sanitize_model_response(text: str) -> str:
    cleaned = text or ""
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<thinking>.*?</thinking>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)

    # Drop common "reasoning" headers/lines that some chat models emit.
    forbidden_line_prefixes = (
        "think:",
        "*think*:",
        "thought:",
        "thoughts:",
        "reasoning:",
        "internal reasoning:",
        "analysis:",
        "chain of thought:",
    )
    scaffold_prefixes = (
        "user ",
        "user:",
        "role:",
        "target:",
        "relationship:",
        "constraints:",
        "option ",
        "lowercase?",
        "short?",
        "savage?",
        "no disallowed emojis?",
        "no long paragraphs?",
    )
    filtered_lines = []
    for line in cleaned.splitlines():
        normalized = line.strip().lower()
        if any(normalized.startswith(prefix) for prefix in forbidden_line_prefixes):
            continue
        if any(normalized.startswith(prefix) for prefix in scaffold_prefixes):
            continue
        filtered_lines.append(line)
    cleaned = "\n".join(filtered_lines)

    # If model leaks "Final answer:" style formatting, keep only the final part.
    final_markers = ("final answer:", "answer:")
    lowered = cleaned.lower()
    marker_positions = [lowered.rfind(marker) for marker in final_markers]
    marker_positions = [pos for pos in marker_positions if pos != -1]
    if marker_positions:
        cut_at = max(marker_positions)
        cleaned = cleaned[cut_at:].split(":", 1)[-1]

    # If scaffolding leaked with candidate quoted answers, keep the last quoted candidate.
    quoted_candidates = re.findall(r'"([^"\n]{4,})"', cleaned)
    if quoted_candidates:
        cleaned = quoted_candidates[-1]

    # Remove common bullet formatting + accidental duplicated answer.
    cleaned = re.sub(r"^[\s•\-\*]+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = re.sub(r"(.{8,}?)\s+\1$", r"\1", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def message_content_to_text(content) -> str:
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if text:
                    text_parts.append(str(text))
                inline_data = part.get("inlineData")
                if inline_data:
                    mime_type = inline_data.get("mimeType", "image")
                    text_parts.append(f"[attachment: {mime_type}]")
            else:
                text_parts.append(str(part))
        return "\n".join(text_parts)
    return str(content)


def build_pollinations_prompt(system_prompt: str, messages: list[dict]) -> str:
    prompt_lines = [f"System: {system_prompt.strip()}"]
    for msg in messages:
        role = msg.get("role", "user")
        content = message_content_to_text(msg.get("parts", msg.get("content", "")))
        prompt_lines.append(f"{role.title()}: {content}")
    prompt_lines.append("Assistant:")
    return "\n\n".join(prompt_lines)


async def pollinations_text_get(prompt: str, model: str) -> str:
    params = {
        "model": model,
        "temperature": "0.85",
        "max_tokens": "100",
        "key": POLLINATIONS_API_KEY,
    }
    query = "&".join(f"{quote(str(key))}={quote(str(value))}" for key, value in params.items())
    url = f"https://gen.pollinations.ai/text/{quote(prompt, safe='')}?{query}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=60) as response:
            if response.status >= 400:
                error_body = await response.text()
                raise RuntimeError(f"Pollinations failed ({response.status}): {error_body}")

            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                data = await response.json()
                if isinstance(data, dict):
                    choices = data.get("choices") or []
                    if choices:
                        return (choices[0].get("message") or {}).get("content", "")
                    return str(data.get("text") or data.get("response") or "")
                return str(data)

            return await response.text()


async def google_gemma_attachment_reply(user_id: int, content: str, attachment_urls: list[str] | None = None) -> str:
    history = user_memory.get(user_id, [])
    identity_prompt = get_identity_context(user_id)
    system_prompt = f"{BASE_SYSTEM_PROMPT.strip()}\n\n{identity_prompt}"

    messages = history[-6:]
    attachment_urls = attachment_urls or []
    messages.append({"role": "user", "content": content})

    gemma_contents = []
    async with aiohttp.ClientSession() as session:
        for i, msg in enumerate(messages):
            role = "model" if msg["role"] == "assistant" else "user"
            parts = [{"text": str(msg["content"])}]

            if attachment_urls and i == len(messages) - 1:
                for url in attachment_urls:
                    async with session.get(url, timeout=30) as img_response:
                        if img_response.status >= 400:
                            error_body = await img_response.text()
                            raise RuntimeError(
                                f"Attachment download failed ({img_response.status}): {error_body}"
                            )
                        mime_type = img_response.headers.get("Content-Type", "image/jpeg").split(";")[0]
                        image_bytes = await img_response.read()
                        parts.append(
                            {
                                "inlineData": {
                                    "mimeType": mime_type,
                                    "data": base64.b64encode(image_bytes).decode("utf-8"),
                                }
                            }
                        )

            gemma_contents.append({"role": role, "parts": parts})

        payload = {
            "systemInstruction": {
                "parts": [{"text": system_prompt}],
            },
            "contents": gemma_contents,
            "generationConfig": {
                "temperature": 0.85,
                "topP": 0.95,
                "maxOutputTokens": 100,
            },
        }

        async with session.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMMA_MODEL}:generateContent?key={GEMINI_API_KEY}",
            json=payload,
            timeout=60,
        ) as response:
            if response.status >= 400:
                error_body = await response.text()
                raise RuntimeError(f"Google Gemma chat failed ({response.status}): {error_body}")
            data = await response.json()

    reply = ""
    candidates = data.get("candidates") or []
    if candidates:
        parts = (((candidates[0] or {}).get("content") or {}).get("parts")) or []
        reply = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
    reply = sanitize_model_response(reply)

    # Update memory
    if attachment_urls:
        attachment_summary = "\n".join(f"[image] {url}" for url in attachment_urls)
        history_content = f"{content}\n{attachment_summary}".strip()
    else:
        history_content = content

    history.append({"role": "user", "content": history_content})
    history.append({"role": "assistant", "content": reply})
    user_memory[user_id] = history[-MAX_MEMORY:]

    return reply or "brain lag 💔"

async def pollinations_reply(user_id: int, content: str) -> str:
    history = user_memory.get(user_id, [])

    identity_prompt = get_identity_context(user_id)
    system_prompt = f"{BASE_SYSTEM_PROMPT.strip()}\n\n{identity_prompt}"

    messages = []

    for msg in history[-6:]:
        messages.append({
            "role": msg["role"],
            "content": str(msg["content"])
        })

    messages.append({
        "role": "user",
        "content": content
    })

    prompt = build_pollinations_prompt(system_prompt, messages)
    reply = await pollinations_text_get(prompt, POLLINATIONS_MODEL)
    reply = sanitize_model_response(reply)

    history.append({
        "role": "user",
        "content": content
    })

    history.append({
        "role": "assistant",
        "content": reply
    })

    user_memory[user_id] = history[-MAX_MEMORY:]

    return reply or "brain lag 💔"

async def chat_reply(
    user_id: int,
    content: str,
    attachment_urls: list[str] | None = None
) -> str:

    attachment_urls = attachment_urls or []

    # Attachments → Gemma
    if attachment_urls:
        print("Using Google Gemma (attachments)")
        return await google_gemma_attachment_reply(
            user_id,
            content,
            attachment_urls
        )

    # Text only → Mistral
    print("Using Pollinations Mistral")
    return await pollinations_reply(
        user_id,
        content
    )
# ─── API AIRFORCE IMAGE SYSTEM ──────────────────────────


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
        "Authorization": f"Bearer {API_AIRFORCE_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": "imagen-4",
        "prompt": prompt,
        "size": "1024x1024",
        "response_format": "b64_json",
        "n": 1,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.airforce/v1/images/generations",
            json=payload,
            headers=headers,
            timeout=120,
        ) as response:
            if response.status >= 400:
                error_body = await response.text()
                raise RuntimeError(f"Api Airforce generation failed ({response.status}): {error_body}")

            data = await response.json()

    image_data = (data.get("data") or [{}])[0].get("b64_json")
    if not image_data:
        raise RuntimeError(f"Api Airforce returned no image data: {data}")

    img_bytes = base64.b64decode(image_data)
    if len(img_bytes) < 1000:
        raise RuntimeError("Api Airforce returned an unexpectedly small image")

    print("Api Airforce model used: imagen-4")
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

    if not bot.user:
        return

    bot_id = bot.user.id

    mentions_bot = (
        f"<@{bot_id}>" in message.content or f"<@!{bot_id}>" in message.content
    )
    replied_to_bot = (
        message.reference is not None
        and isinstance(message.reference.resolved, discord.Message)
        and message.reference.resolved.author.id == bot_id
    )

    if not mentions_bot and not replied_to_bot:
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
    attachment_urls = [a.url for a in message.attachments if a.content_type and a.content_type.startswith("image/")]

    if message.reference and isinstance(message.reference.resolved, discord.Message):
        replied_message = message.reference.resolved
        attachment_urls.extend(
            a.url for a in replied_message.attachments
            if a.content_type and a.content_type.startswith("image/")
        )


    # 🔒 HARD NSFW BLOCK (GLOBAL)
    if NSFW_ENABLED and contains_nsfw(content):
        await message.reply("no nih 💔")
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

        try:
            async with message.channel.typing():
                image_file = await generate_image(prompt)
            await message.reply(file=image_file)

        except Exception as e:
            print("IMAGE ERROR:", e)
            await message.reply("image gen died. unlucky.")

        return

    # 💬 CHAT
    try:
        async with message.channel.typing():
            reply = await chat_reply(user_id, content, attachment_urls=attachment_urls)
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
