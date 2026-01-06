import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import json
import os
import time
from datetime import datetime

# =========================
# 設定
# =========================
SUPPORTED_LANGS = {
    "ja": "日本語",
    "en": "English",
    "ko": "한국어",
    "zh": "中文"
}

DATA_DIR = "data"
CHANNEL_LINK_PATH = f"{DATA_DIR}/channel_links.json"
TRANSLATE_LOG_PATH = f"{DATA_DIR}/translate_logs.json"
LANGDICT_PATH = f"{DATA_DIR}/lang_dict.json"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent"

# =========================
# ユーティリティ
# =========================
def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def append_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    logs = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            logs = json.load(f)
    logs.append(data)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

# =========================
# Model翻訳
# =========================
class ModelTranslator:
    """
    LangDictJson を使った自作翻訳
    """
    def __init__(self):
        self.lang_dict = load_json(LANGDICT_PATH, {"entries": {}})

    def translate(self, text: str, src_lang: str):
        """
        src_lang 以外の言語に翻訳
        単語が見つからなければ None
        """
        result = {}
        entries = self.lang_dict.get("entries", {})

        for eid, entry in entries.items():
            langs = entry.get("languages", {})
            if src_lang not in langs:
                continue

            if text in langs[src_lang]:
                # 見つかったら confidence をチェックして返す
                for target_lang, texts in langs.items():
                    if target_lang == src_lang:
                        continue
                    result[target_lang] = texts[0]  # 単純に最初の翻訳を返す

        return result if result else None

# =========================
# 翻訳Cog
# =========================
class TranslateCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.channel_links = load_json(CHANNEL_LINK_PATH, {})
        self.session = aiohttp.ClientSession()
        self.model_translator = ModelTranslator()

    async def cog_unload(self):
        await self.session.close()

    # =========================
    # Gemini翻訳
    # =========================
    async def translate_with_gemini(self, text: str, src_lang: str):
        prompt = (
            "You are a professional translation assistant.\n"
            "Translate the following message naturally.\n"
            "Preserve tone and intent.\n\n"
            f"Source language: {src_lang}\n"
            f"Message: {text}\n\n"
            "Return JSON only:\n"
            "{ \"ja\": \"...\", \"en\": \"...\", \"ko\": \"...\", \"zh\": \"...\" }"
        )

        payload = {
            "contents": [{"parts": [{"text": prompt}]}]
        }

        async with self.session.post(
            f"{GEMINI_API_URL}?key={GEMINI_API_KEY}",
            json=payload
        ) as resp:
            if resp.status != 200:
                return {}

            data = await resp.json()

        try:
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(raw)
        except Exception:
            return {}

        return {
            lang: parsed.get(lang)
            for lang in SUPPORTED_LANGS
            if lang != src_lang and parsed.get(lang)
        }

    # =========================
    # ログ保存
    # =========================
    def save_translate_log(self, translations: dict):
        log = {
            "timestamp": int(time.time()),
            "time": datetime.utcnow().strftime("%Y:%m:%d"),
            "word": translations
        }
        append_json(TRANSLATE_LOG_PATH, log)

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

        src_lang = self.channel_links[cid]["lang"]
        text = message.content.strip()
        if not text:
            return

        # ===== Model翻訳優先 =====
        translations = self.model_translator.translate(text, src_lang)

        # ===== Geminiフォールバック =====
        if not translations:
            translations = await self.translate_with_gemini(text, src_lang)

        if not translations:
            return

        # ===== ログ保存 =====
        full_log = {src_lang: text}
        full_log.update(translations)
        self.save_translate_log(full_log)

        # ===== ブロードキャスト =====
        for target_cid, info in self.channel_links.items():
            if info["lang"] == src_lang:
                continue

            content = translations.get(info["lang"])
            if not content:
                continue

            webhook = discord.Webhook.from_url(
                info["webhook"],
                session=self.session
            )

            await webhook.send(
                content,
                username=message.author.display_name,
                avatar_url=message.author.display_avatar.url
            )

    # =========================
    # /setchat
    # =========================
    @app_commands.command(name="setchat", description="このチャンネルを翻訳連携に追加します")
    async def setchat(self, interaction: discord.Interaction):
        options = [
            discord.SelectOption(label=name, value=code)
            for code, name in SUPPORTED_LANGS.items()
        ]

        select = discord.ui.Select(
            placeholder="このチャンネルの言語を選択",
            options=options
        )

        async def callback(inter):
            lang = select.values[0]
            channel = inter.channel

            webhook = await channel.create_webhook(
                name=f"Translate-{lang}"
            )

            self.channel_links[str(channel.id)] = {
                "lang": lang,
                "webhook": webhook.url
            }

            os.makedirs(DATA_DIR, exist_ok=True)
            with open(CHANNEL_LINK_PATH, "w", encoding="utf-8") as f:
                json.dump(self.channel_links, f, ensure_ascii=False, indent=2)

            await inter.response.send_message(
                f"✅ `{SUPPORTED_LANGS[lang]}` として設定しました",
                ephemeral=True
            )

        select.callback = callback
        view = discord.ui.View()
        view.add_item(select)

        await interaction.response.send_message(
            "チャンネルの言語を選択してください",
            view=view,
            ephemeral=True
        )

    # =========================
    # /delete_settings
    # =========================
    @app_commands.command(name="delete_settings", description="翻訳設定を解除します")
    async def delete_settings(self, interaction: discord.Interaction):
        cid = str(interaction.channel.id)
        if cid not in self.channel_links:
            await interaction.response.send_message(
                "このチャンネルは未登録です",
                ephemeral=True
            )
            return

        del self.channel_links[cid]
        with open(CHANNEL_LINK_PATH, "w", encoding="utf-8") as f:
            json.dump(self.channel_links, f, ensure_ascii=False, indent=2)

        await interaction.response.send_message(
            "🗑️ 翻訳設定を解除しました",
            ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(TranslateCog(bot))