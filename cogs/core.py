import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import aiohttp
import asyncio
from difflib import SequenceMatcher
import time
from collections import deque
import time

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
        self.context_logs = {}
        self.CONTEXT_WINDOW = 20

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
    # メッセージ選択監視
    # =========================
    
    def choose_meaning_with_context(self, text, src_lang, message):
    cid = str(message.channel.id)
    logs = self.context_logs.get(cid, [])

    candidates = []

    for eid, entry in self.translate_db["entries"].items():
        if src_lang not in entry["languages"]:
            continue

        base_score = entry.get("confidence", 0.3)

        # 文言類似
        for phrase in entry["languages"][src_lang]:
            sim = SequenceMatcher(None, text.lower(), phrase.lower()).ratio()
            if sim > 0.85:
                base_score += sim * 0.4

        # 文脈補正
        for log in reversed(logs):
            time_diff = message.created_at.timestamp() - log["timestamp"]
            if time_diff > 300:
                break

            if log["meaning_id"] == eid:
                base_score += 0.3

            if message.reference and log["author"] == message.reference.resolved.author.id:
                base_score += 0.4

        candidates.append((eid, base_score))

    if not candidates:
        return None, None

    best_id, score = max(candidates, key=lambda x: x[1])

    if score < 0.5:
        return None, None

    entry = self.translate_db["entries"][best_id]
    results = {lang: variants[0] for lang, variants in entry["languages"].items()}
    return results, best_id
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        cid = str(message.channel.id)
        if cid not in self.channel_links:
            return

        source_lang = self.channel_links[cid]["lang"]
        content = message.content

        translated, meaning_id = self.choose_meaning_with_context(
            content, source_lang, message
        )

        if translated:
            self.adjust_confidence(
                self.translate_db["entries"][meaning_id], +0.03
            )
        else:
            translated = await self.translate_api(content, source_lang)
            meaning_id = None

        self.log_context(message, meaning_id)
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
        
# =========================
# 意味ID統合 View
# =========================
class MeaningMergeView(discord.ui.View):
    def __init__(self, cog, message):
        super().__init__(timeout=180)
        self.cog = cog
        self.message = message

        options = []
        for eid, entry in cog.translate_db["entries"].items():
            preview = []
            for lang, words in entry["languages"].items():
                preview.append(f"{lang}:{words[0]}")
            label = f"ID {eid} | {' / '.join(preview[:2])}"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=eid,
                    description=f"context={entry.get('context','unknown')}"
                )
            )

        self.select = discord.ui.Select(
            placeholder="統合先の意味IDを選択",
            options=options[:25]
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        target_id = self.select.values[0]

        source_entry = None
        for eid, entry in self.cog.translate_db["entries"].items():
            for variants in entry["languages"].values():
                if self.message.content in variants:
                    source_entry = (eid, entry)
                    break

        if not source_entry:
            await interaction.response.send_message(
                "⚠️ 元の意味IDが見つかりません",
                ephemeral=True
            )
            return

        source_id, source = source_entry
        target = self.cog.translate_db["entries"][target_id]

        if source_id == target_id:
            await interaction.response.send_message(
                "⚠️ 同じIDです",
                ephemeral=True
            )
            return

        # languages をマージ
        for lang, variants in source["languages"].items():
            target.setdefault("languages", {}).setdefault(lang, [])
            for v in variants:
                if v not in target["languages"][lang]:
                    target["languages"][lang].append(v)

        # confidence 調整（統合は強い学習）
        target["confidence"] = min(
            max(target.get("confidence", 0.5), source.get("confidence", 0.5)) + 0.1,
            1.0
        )

        target["last_modified"] = time.time()

        # 元ID削除
        del self.cog.translate_db["entries"][source_id]
        save_json(DATA_PATH, self.cog.translate_db)

        await interaction.response.send_message(
            f"✅ 意味ID `{source_id}` を `{target_id}` に統合しました",
            ephemeral=True
        )
        self.stop()

# =========================
# ❓リアクション拡張
# =========================
@commands.Cog.listener()
async def on_reaction_add(self, reaction, user):
    if user.bot or str(reaction.emoji) != "❓":
        return

    message = reaction.message

    embed = discord.Embed(
        title="翻訳の扱いを選択してください",
        description=(
            "この翻訳はどう扱いますか？\n\n"
            "🛠 修正 → 表現を追加\n"
            "🧬 統合 → 別の意味IDにまとめる\n"
        ),
        color=0xF1C40F
    )

    await message.channel.send(
        embed=embed,
        view=TranslationActionView(self, message)
    )

# =========================
# 行動選択 View
# =========================
class TranslationActionView(discord.ui.View):
    def __init__(self, cog, message):
        super().__init__(timeout=120)
        self.cog = cog
        self.message = message

    @discord.ui.button(label="🛠 修正", style=discord.ButtonStyle.primary)
    async def fix(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            TranslationFixModal(self.cog, self.message)
        )
        self.stop()

    @discord.ui.button(label="🧬 意味ID統合", style=discord.ButtonStyle.secondary)
    async def merge(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "統合先の意味IDを選んでください",
            view=MeaningMergeView(self.cog, self.message),
            ephemeral=True
        )
        self.stop()

async def setup(bot):
    await bot.add_cog(Core(bot))