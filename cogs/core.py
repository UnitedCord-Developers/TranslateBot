import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import aiohttp
import asyncio
from difflib import SequenceMatcher

DATA_PATH = "data/dictionaries/translate.json"
CHANNEL_CONFIG_PATH = "data/channel_links.json"

GOOGLE_TRANSLATE_API_KEY = "YOUR_GOOGLE_API_KEY"
GOOGLE_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"

SUPPORTED_LANGS = ["ja", "en", "ko", "zh"]

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

class Core(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.translate_db = load_json(DATA_PATH, {"meta": {}, "entries": {}})
        self.channel_links = load_json(CHANNEL_CONFIG_PATH, {})

    # =========================
    # /setchat
    # =========================
    @app_commands.command(name="setchat", description="このチャンネルを翻訳連携に追加します")
    @app_commands.describe(lang="チャンネルの言語")
    async def setchat(self, interaction: discord.Interaction, lang: str):
        if lang not in SUPPORTED_LANGS:
            await interaction.response.send_message("未対応言語です", ephemeral=True)
            return

        channel = interaction.channel
        webhook = await channel.create_webhook(name=f"UniversalBot-{lang}")

        self.channel_links[str(channel.id)] = {
            "lang": lang,
            "webhook": webhook.url
        }
        save_json(CHANNEL_CONFIG_PATH, self.channel_links)

        await interaction.response.send_message(
            f"✅ このチャンネルを `{lang}` として連携しました",
            ephemeral=True
        )

    # =========================
    # /deletechat
    # =========================
    @app_commands.command(name="deletechat", description="このチャンネルの翻訳連携を解除します")
    async def deletechat(self, interaction: discord.Interaction):
        cid = str(interaction.channel.id)

        if cid not in self.channel_links:
            await interaction.response.send_message("このチャンネルは未登録です", ephemeral=True)
            return

        del self.channel_links[cid]
        save_json(CHANNEL_CONFIG_PATH, self.channel_links)

        await interaction.response.send_message("🗑️ 連携を解除しました", ephemeral=True)

    # =========================
    # confidence 操作
    # =========================
    def adjust_confidence(self, entry, delta):
        entry["confidence"] = max(
            0.0,
            min(entry.get("confidence", 0.3) + delta, 1.0)
        )

    # =========================
    # メッセージ監視
    # =========================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        cid = str(message.channel.id)
        if cid not in self.channel_links:
            return

        source_lang = self.channel_links[cid]["lang"]
        content = message.content

        translated, entry = self.translate_from_json(content, source_lang)

        if translated:
            self.adjust_confidence(entry, +0.03)
        else:
            translated = await self.translate_api(content, source_lang)

        await self.broadcast(message, translated, source_lang)

    # =========================
    # JSON翻訳
    # =========================
    def translate_from_json(self, text, src_lang):
        for eid, entry in self.translate_db["entries"].items():
            if src_lang not in entry["languages"]:
                continue

            for phrase in entry["languages"][src_lang]:
                ratio = SequenceMatcher(None, text.lower(), phrase.lower()).ratio()

                if ratio > 0.9:
                    self.adjust_confidence(entry, +0.05 if ratio > 0.97 else +0.02)

                    results = {}
                    for lang, variants in entry["languages"].items():
                        results[lang] = variants[0]

                    save_json(DATA_PATH, self.translate_db)
                    return results, entry

        return None, None

    # =========================
    # Google API 翻訳
    # =========================
    async def translate_api(self, text, src_lang):
        results = {}
        async with aiohttp.ClientSession() as session:
            for target in SUPPORTED_LANGS:
                if target == src_lang:
                    continue
                payload = {
                    "q": text,
                    "source": src_lang,
                    "target": target,
                    "key": GOOGLE_TRANSLATE_API_KEY
                }
                async with session.post(GOOGLE_TRANSLATE_URL, json=payload) as resp:
                    data = await resp.json()
                    results[target] = data["data"]["translations"][0]["translatedText"]

        self.register_translation(text, src_lang, results)
        return results

    # =========================
    # JSON登録
    # =========================
    def register_translation(self, src_text, src_lang, translated):
        new_id = str(max(map(int, self.translate_db["entries"].keys()), default=1000) + 1)

        self.translate_db["entries"][new_id] = {
            "context": "unknown",
            "confidence": 0.3,
            "last_modified": time.time(),
            "languages": {
                src_lang: [src_text],
                **{lang: [txt] for lang, txt in translated.items()}
            }
        }
        save_json(DATA_PATH, self.translate_db)

    # =========================
    # ブロードキャスト
    # =========================
    async def broadcast(self, message, translated, src_lang):
        for cid, info in self.channel_links.items():
            if info["lang"] == src_lang:
                continue

            async with aiohttp.ClientSession() as session:
                webhook = discord.Webhook.from_url(info["webhook"], session=session)

                content = translated.get(info["lang"])
                if not content:
                    continue

                sent = await webhook.send(
                    content,
                    username=message.author.display_name,
                    avatar_url=message.author.display_avatar.url,
                    wait=True
                )

                await sent.add_reaction("❓")

    # =========================
    # ❓リアクション
    # =========================
    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        if user.bot or str(reaction.emoji) != "❓":
            return

        message = reaction.message

        for entry in self.translate_db["entries"].values():
            for variants in entry["languages"].values():
                if message.content in variants:
                    self.adjust_confidence(entry, -0.05)
                    save_json(DATA_PATH, self.translate_db)
                    break

        await message.channel.send_modal(
            TranslationFixModal(self, message)
        )

class TranslationFixModal(discord.ui.Modal, title="翻訳修正"):

    def __init__(self, cog, message):
        super().__init__()
        self.cog = cog
        self.message = message

        self.correct_text = discord.ui.TextInput(
            label="より自然な翻訳を入力してください",
            style=discord.TextStyle.long,
            required=True
        )
        self.add_item(self.correct_text)

    async def on_submit(self, interaction: discord.Interaction):
        fixed = self.correct_text.value.strip()

        for entry in self.cog.translate_db["entries"].values():
            for lang, variants in entry["languages"].items():
                if self.message.content in variants:
                    if fixed not in variants:
                        variants.append(fixed)
                        self.cog.adjust_confidence(entry, +0.12)
                        entry["last_modified"] = time.time()
                        save_json(DATA_PATH, self.cog.translate_db)

                        await interaction.response.send_message(
                            "✅ 翻訳を学習しました。",
                            ephemeral=True
                        )
                        return

        await interaction.response.send_message(
            "⚠️ 対応する翻訳が見つかりませんでした。",
            ephemeral=True
        )

async def setup(bot):
    await bot.add_cog(Core(bot))