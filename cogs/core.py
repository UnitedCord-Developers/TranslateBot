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

        # JSON翻訳を優先
        translated = self.translate_from_json(content, source_lang)

        if not translated:
            translated = await self.translate_api(content, source_lang)

        await self.broadcast(message, translated, source_lang)

    # =========================
    # JSON翻訳
    # =========================
    def translate_from_json(self, text, src_lang):
        results = {}
        for eid, entry in self.translate_db["entries"].items():
            if src_lang in entry["languages"]:
                for phrase in entry["languages"][src_lang]:
                    ratio = SequenceMatcher(None, text.lower(), phrase.lower()).ratio()
                    if ratio > 0.9:
                        for lang, variants in entry["languages"].items():
                            results[lang] = variants[0]
                        return results
        return None

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
                    translated = data["data"]["translations"][0]["translatedText"]
                    results[target] = translated

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

            webhook = discord.Webhook.from_url(
                info["webhook"],
                session=aiohttp.ClientSession()
            )

            content = translated.get(info["lang"])
            if not content:
                continue

            await webhook.send(
                content,
                username=message.author.display_name,
                avatar_url=message.author.display_avatar.url
            )


async def setup(bot):
    await bot.add_cog(Core(bot))
