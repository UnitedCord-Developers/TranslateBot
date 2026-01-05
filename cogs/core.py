import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import aiohttp
import asyncio
from difflib import SequenceMatcher
import time
from collections import defaultdict, deque
import random

DATA_PATH = "data/dictionaries/translate.json"
CHANNEL_CONFIG_PATH = "data/channel_links.json"
MEANING_DISTANCE_PATH = "data/dictionaries/meaning_distance.json"
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

def save_meaning_distance(path, data):
    serializable = {
        a: dict(b)
        for a, b in data.items()
    }
    save_json(path, {
        "meta": {"updated": time.time()},
        "distances": serializable
    })

def load_meaning_distance(path):
    raw = load_json(path, {"distances": {}})
    md = defaultdict(lambda: defaultdict(float))
    for a, bs in raw.get("distances", {}).items():
        for b, v in bs.items():
            md[a][b] = float(v)
    return md
    
class Core(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.translate_db = load_json(DATA_PATH, {"meta": {}, "entries": {}})
        self.meaning_distance = load_meaning_distance(MEANING_DISTANCE_PATH)
        self.channel_links = load_json(CHANNEL_CONFIG_PATH, {})
        self.context_logs = {}
        self.meaning_clusters = defaultdict(lambda: defaultdict(int))
        self.CONTEXT_WINDOW = 20
        self.last_decay_check = time.time()
        self.CONFIDENCE_HALF_LIFE = 60 * 60 * 24 * 7  # 7日
        self.http_session = aiohttp.ClientSession()
        
    def detect_emotion(self, text, lang):
        t = text.lower()

        if any(x in t for x in ["!", "！？", "!!"]):
            return "excited"

        if any(x in t.split() for x in ["w", "lol"]) or "草" in t:
            return "joking"

        if any(x in t for x in ["wtf", "は？", "なにそれ"]):
            return "angry"

        if "?" in t or "？" in t:
            return "question"

        return "neutral"
    
    def log_context(self, message, meaning_id, emotion):
        cid = str(message.channel.id)

        self.context_logs.setdefault(
            cid, deque(maxlen=self.CONTEXT_WINDOW)
        )

        self.context_logs[cid].append({
            "timestamp": message.created_at.timestamp(),
            "content": message.content,
            "meaning_id": meaning_id,
            "emotion": emotion
        })

        if meaning_id:
            self.meaning_clusters[cid][meaning_id] += 1
            self.learn_meaning_distance(str(message.channel.id))

    def learn_meaning_distance(self, channel_id):
        logs = list(self.context_logs.get(channel_id, []))

        for i in range(len(logs) - 1):
            a = logs[i]["meaning_id"]
            b = logs[i + 1]["meaning_id"]

            if not a or not b or a == b:
                continue

            self.meaning_distance[a][b] += 0.1
            self.meaning_distance[b][a] += 0.1

        save_meaning_distance(MEANING_DISTANCE_PATH, self.meaning_distance)
    def decay_confidence(self):
        now = time.time()
        elapsed = now - self.last_decay_check

        if elapsed < 3600:
            return  # 1時間に1回で十分

        for entry in self.translate_db["entries"].values():
            conf = entry.get("confidence", 0.3)

            decay_factor = 0.5 ** (elapsed / self.CONFIDENCE_HALF_LIFE)
            entry["confidence"] = max(0.05, conf * decay_factor)

        self.last_decay_check = now
        save_json(DATA_PATH, self.translate_db)
        for entry in self.translate_db["entries"].values():
            ctx = entry.get("context", {}).get("emotion", {})
            for k in list(ctx.keys()):
                ctx[k] *= decay_factor
                if ctx[k] < 0.05:
                    del ctx[k]
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
    def choose_meaning_with_probability(self, text, src_lang, message):
        cid = str(message.channel.id)
        logs = self.context_logs.get(cid, [])
        clusters = self.meaning_clusters.get(cid, {})

        emotion = self.detect_emotion(text, src_lang)

        scored = []

        for eid, entry in self.translate_db["entries"].items():
            if src_lang not in entry["languages"]:
                continue

            # 基本確率 = confidence
            score = entry.get("confidence", 0.3)

            # 文言類似
            for phrase in entry["languages"][src_lang]:
                sim = SequenceMatcher(None, text.lower(), phrase.lower()).ratio()
                if sim > 0.8:
                    score += sim * 0.4

            # クラスタ（最近よく使われている意味）
            score += clusters.get(eid, 0) * 0.05

            # 感情一致補正（context 名を使う）
            if entry.get("context"):
                emotion_ctx = entry.get("context", {}).get("emotion", {})
                if emotion in emotion_ctx:
                    score += emotion_ctx[emotion] * 0.4

            # リプライ補正
            if message.reference:
                for log in reversed(logs):
                    if log["meaning_id"] == eid:
                        score += 0.3
                        break
            # 直近意味との距離補正
            recent_meaning = None
            if logs:
                recent_meaning = logs[-1]["meaning_id"]

            if recent_meaning and eid in self.meaning_distance[recent_meaning]:
                score += self.meaning_distance[recent_meaning][eid] * 0.3
            scored.append((eid, max(score, 0.01)))

        if not scored:
            return None, None, emotion

        # 確率分布化
        total = sum(s for _, s in scored)
        r = random.uniform(0, total)

        upto = 0
        for eid, s in scored:
            upto += s
            if upto >= r:
                entry = self.translate_db["entries"][eid]
                results = {
                    lang: variants[0]
                    for lang, variants in entry["languages"].items()
                }
                return results, eid, emotion

        return None, None, emotion
        
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        self.decay_confidence()
        if message.author.bot:
            return

        cid = str(message.channel.id)
        if cid not in self.channel_links:
            return

        source_lang = self.channel_links[cid]["lang"]
        content = message.content

        translated, meaning_id, emotion = self.choose_meaning_with_probability(
            content, source_lang, message
        )

        if translated:
            self.adjust_confidence(
                self.translate_db["entries"][meaning_id], +0.02
            )
        else:
            translated = await self.translate_api(content, source_lang)
            meaning_id = None

        self.log_context(message, meaning_id, emotion)
        await self.broadcast(message, translated, source_lang)

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
                    if resp.status != 200:
                        continue

                    data = await resp.json()
                    try:
                        results[target] = data["data"]["translations"][0]["translatedText"]
                    except (KeyError, IndexError, TypeError):
                        continue

        self.register_translation(text, src_lang, results)
        return results

    # =========================
    # JSON登録
    # =========================
    def register_translation(self, src_text, src_lang, translated):
        new_id = str(max(map(int, self.translate_db["entries"].keys()), default=1000) + 1)

        self.translate_db["entries"][new_id] = {
            "context": {
                "emotion": {},
                "usage": {}
            },
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

            webhook = discord.Webhook.from_url(
                info["webhook"],
                session=self.http_session
            )

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
    # ❓リアクション（統合版）
    # =========================
    @commands.Cog.listener()
    async def on_reaction_add(self, reaction, user):
        if user.bot or str(reaction.emoji) != "❓":
            return
        if reaction.message.webhook_id is None:
            return

        message = reaction.message

        # confidence を軽く下げる（疑義が出た時点で）
        for entry in self.translate_db["entries"].values():
            for variants in entry["languages"].values():
                if message.content in variants:
                    self.adjust_confidence(entry, -0.05)
                    save_json(DATA_PATH, self.translate_db)
                    break

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
                        emotion = self.cog.detect_emotion(fixed, lang)
                        ctx = entry.setdefault("context", {}).setdefault("emotion", {})
                        ctx[emotion] = min(ctx.get(emotion, 0.0) + 0.2, 1.0)
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
async def cog_unload(self):
    await self.http_session.close()