import discord
from discord.ext import commands
from discord import app_commands
import os
import json
import time
import random
import asyncio
from datetime import datetime, timedelta
import httpx

BOT_VERSION = "v18-trace"

TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GUILD_ID = 1442484265475506207

http_client: httpx.AsyncClient | None = None

box_locks: dict[str, asyncio.Lock] = {}

def get_lock(box_id: str) -> asyncio.Lock:
    if box_id not in box_locks:
        box_locks[box_id] = asyncio.Lock()
    return box_locks[box_id]

def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

# ── Supabase: boxes ──────────────────────────────────────────────
async def load_all_boxes() -> dict:
    url = f"{SUPABASE_URL}/rest/v1/boxes?select=*"
    r = await http_client.get(url, headers=sb_headers())
    rows = r.json()
    if not isinstance(rows, list):
        return {}
    return {row["box_id"]: row for row in rows}

async def load_box(box_id: str) -> dict | None:
    t0 = time.monotonic()
    url = f"{SUPABASE_URL}/rest/v1/boxes?box_id=eq.{box_id}&select=*"
    r = await http_client.get(url, headers=sb_headers())
    print(f"[DB] load_box({box_id}) status={r.status_code} in {time.monotonic()-t0:.2f}s")
    rows = r.json()
    if not isinstance(rows, list) or len(rows) == 0:
        return None
    return rows[0]

async def save_box(box_id: str, box: dict) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/boxes"
    headers = {**sb_headers(), "Prefer": "return=representation"}
    payload = {
        "box_id": box_id,
        "name": box["name"],
        "series": box["series"],
        "prezzo": box["prezzo"],
        "created_by": box["created_by"],
        "created_at": box["created_at"],
        "message_id": box.get("message_id"),
        "channel_id": box["channel_id"],
        "variants": json.dumps(box["variants"]),
    }
    r = await http_client.post(url, headers=headers, json=payload)
    if r.status_code not in (200, 201):
        print(f"[SAVE_BOX] ERRORE status={r.status_code} body={r.text[:200]}")
        return False
    return True

async def delete_box_db(box_id: str):
    url = f"{SUPABASE_URL}/rest/v1/boxes?box_id=eq.{box_id}"
    await http_client.delete(url, headers=sb_headers())

async def update_variants(box_id: str, variants: dict) -> bool:
    t0 = time.monotonic()
    url = f"{SUPABASE_URL}/rest/v1/boxes?box_id=eq.{box_id}"
    headers = {**sb_headers(), "Prefer": "return=representation"}
    r = await http_client.patch(url, headers=headers, json={"variants": json.dumps(variants)})
    print(f"[DB] update_variants({box_id}) status={r.status_code} in {time.monotonic()-t0:.2f}s")
    if r.status_code not in (200, 204):
        print(f"[UPDATE_VARIANTS] ERRORE body={r.text[:200]}")
        return False
    return True

async def update_message_id(box_id: str, message_id: str):
    url = f"{SUPABASE_URL}/rest/v1/boxes?box_id=eq.{box_id}"
    headers = {**sb_headers(), "Prefer": "return=representation"}
    await http_client.patch(url, headers=headers, json={"message_id": message_id})

async def generate_unique_box_id() -> str:
    for _ in range(5):
        box_id = f"{str(int(time.time()))[-6:]}{random.randint(10, 99)}"
        if await load_box(box_id) is None:
            return box_id
    return str(int(time.time() * 1000))[-10:]

# ── Supabase: bans ───────────────────────────────────────────────
INFRACTION_WINDOW_DAYS = 30
BAN_DURATIONS = {1: 3, 2: 7}

def parse_dt(value: str) -> datetime:
    """Parsa un timestamp Supabase in datetime naive (senza timezone)."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt

async def load_ban(user_id: str) -> dict | None:
    t0 = time.monotonic()
    url = f"{SUPABASE_URL}/rest/v1/bans?user_id=eq.{user_id}&select=*"
    r = await http_client.get(url, headers=sb_headers())
    print(f"[DB] load_ban({user_id}) status={r.status_code} in {time.monotonic()-t0:.2f}s")
    if r.status_code != 200:
        print(f"[LOAD_BAN] ERRORE body={r.text[:200]}")
        return None
    rows = r.json()
    if not isinstance(rows, list) or len(rows) == 0:
        return None
    return rows[0]

async def save_ban(record: dict) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/bans"
    headers = {**sb_headers(), "Prefer": "resolution=merge-duplicates,return=representation"}
    history = record.get("history", [])
    payload = {
        "user_id": record["user_id"],
        "infractions": record["infractions"],
        "last_infraction_at": record.get("last_infraction_at"),
        "banned_until": record.get("banned_until"),
        "permanent": record.get("permanent", False),
        "history": history if isinstance(history, str) else json.dumps(history),
    }
    r = await http_client.post(url, headers=headers, json=payload)
    if r.status_code not in (200, 201):
        print(f"[SAVE_BAN] ERRORE status={r.status_code} body={r.text[:200]}")
        return False
    return True

async def delete_ban(user_id: str):
    url = f"{SUPABASE_URL}/rest/v1/bans?user_id=eq.{user_id}"
    await http_client.delete(url, headers=sb_headers())

def get_history(record: dict) -> list:
    h = record.get("history", [])
    if isinstance(h, str):
        try:
            return json.loads(h)
        except Exception:
            return []
    return h or []

async def is_user_banned(user_id: str) -> tuple[bool, str | None]:
    try:
        record = await load_ban(user_id)
    except Exception as e:
        # Se il controllo ban fallisce, NON bloccare l'utente
        print(f"[IS_BANNED] Errore, procedo senza blocco: {e}")
        return False, None
    if not record:
        return False, None
    if record.get("permanent"):
        return True, "🚫 Sei stato bannato permanentemente dal sistema di prenotazioni. Contatta lo staff."
    banned_until = record.get("banned_until")
    if banned_until:
        try:
            until = parse_dt(banned_until)
        except Exception as e:
            print(f"[IS_BANNED] Data non valida '{banned_until}': {e}")
            return False, None
        if datetime.now() < until:
            delta = until - datetime.now()
            giorni = delta.days
            ore = int(delta.total_seconds() // 3600) % 24
            return True, f"🚫 Sei temporaneamente bloccato dalle prenotazioni (ancora ~{giorni}g {ore}h). Motivo: annullamento a split completato."
    return False, None

async def register_infraction(user_id: str, box_id: str, variant: str) -> str:
    record = await load_ban(user_id)
    now = datetime.now()

    if record is None:
        record = {"user_id": user_id, "infractions": 0, "last_infraction_at": None,
                  "banned_until": None, "permanent": False, "history": []}

    last = record.get("last_infraction_at")
    if last:
        try:
            if (now - parse_dt(last)) > timedelta(days=INFRACTION_WINDOW_DAYS):
                record["infractions"] = 0
        except Exception:
            pass

    record["infractions"] = int(record.get("infractions") or 0) + 1
    record["last_infraction_at"] = now.isoformat()

    history = get_history(record)
    history.append({"box_id": box_id, "variant": variant, "at": now.isoformat(),
                    "infraction_number": record["infractions"]})
    record["history"] = history
    record["user_id"] = user_id

    n = record["infractions"]
    if n >= 3:
        record["permanent"] = True
        record["banned_until"] = None
        msg = "🚫 **Ban permanente.** Hai annullato a split completato per la 3ª volta. Solo un admin può sbloccarti."
    else:
        giorni = BAN_DURATIONS[n]
        record["banned_until"] = (now + timedelta(days=giorni)).isoformat()
        record["permanent"] = False
        msg = f"🚫 **Sei stato bloccato per {giorni} giorni** dalle prenotazioni (infrazione #{n}). Annullare a split completato penalizza tutti gli altri partecipanti."

    await save_ban(record)
    return msg

# ── Helpers ──────────────────────────────────────────────────────
def fmt_prezzo(prezzo) -> str:
    try:
        return f"{float(prezzo):.2f}€".replace(".", ",")
    except Exception:
        return f"{prezzo}€"

def get_variants(box: dict) -> dict:
    v = box["variants"]
    if isinstance(v, str):
        return json.loads(v)
    return v

def build_embed(box: dict, box_id: str) -> discord.Embed:
    variants = get_variants(box)
    total = len(variants)
    taken = sum(1 for v in variants.values() if v["reserved_by"] is not None)
    color = discord.Color.green() if taken == total else discord.Color.blurple()
    prezzo_str = fmt_prezzo(box.get("prezzo", "?"))
    embed = discord.Embed(
        title=f"🎁 {box['name']} — {box['series']}",
        description=f"**{taken}/{total}** varianti prenotate  •  💰 **{prezzo_str} a variante**",
        color=color,
    )
    embed.set_footer(text=f"ID box: {box_id}")
    lines = []
    for variant, info in variants.items():
        if info["reserved_by"]:
            lines.append(f"✅ ~~{variant}~~ — *prenotata*")
        else:
            lines.append(f"🎁 **{variant}** — libera")
    embed.add_field(name="Varianti", value="\n".join(lines), inline=False)
    return embed

def build_summary(box: dict) -> str:
    variants = get_variants(box)
    prezzo_str = fmt_prezzo(box.get("prezzo", "?"))
    lines = [f"**Riepilogo prenotazioni** — {prezzo_str} a variante:"]
    for variant, info in variants.items():
        lines.append(f"• **{variant}** → <@{info['reserved_by']}>")
    return "\n".join(lines)

# ── View ─────────────────────────────────────────────────────────
class BoxView(discord.ui.View):
    def __init__(self, box_id: str):
        super().__init__(timeout=None)
        self.box_id = box_id

    def populate(self, box: dict):
        self.clear_items()
        variants = get_variants(box)
        for i, variant in enumerate(list(variants.keys())):
            taken = variants[variant]["reserved_by"] is not None
            btn = discord.ui.Button(
                label=f"{'✅' if taken else '🎁'} {variant}"[:80],
                style=discord.ButtonStyle.success if taken else discord.ButtonStyle.primary,
                custom_id=f"b{self.box_id}_v{i}",
                disabled=taken,
                row=i // 5,
            )
            btn.callback = self._make_callback(variant)
            self.add_item(btn)

        cancel_btn = discord.ui.Button(
            label="❌ Annulla la mia prenotazione",
            style=discord.ButtonStyle.danger,
            custom_id=f"cx_{self.box_id}",
            row=4,
        )
        cancel_btn.callback = self.cancel_callback
        self.add_item(cancel_btn)

    async def refresh_box_message(self, message: discord.Message, box: dict,
                                  interaction: discord.Interaction | None = None):
        variants = get_variants(box)
        taken = sum(1 for v in variants.values() if v["reserved_by"] is not None)
        print(f"[REFRESH] box={self.box_id} msg={message.id} stato={taken}/{len(variants)}")
        self.populate(box)
        embed = build_embed(box, self.box_id)
        try:
            if (interaction is not None and interaction.message is not None
                    and interaction.message.id == message.id):
                await interaction.edit_original_response(embed=embed, view=self)
                print(f"[REFRESH] box={self.box_id} edit OK (via interazione)")
            else:
                await message.edit(embed=embed, view=self)
                print(f"[REFRESH] box={self.box_id} edit OK (via messaggio)")
        except Exception as e:
            print(f"[REFRESH] box={self.box_id} edit FALLITO: {e}")
            raise

    def _make_callback(self, variant: str):
        async def callback(interaction: discord.Interaction):
            print(f"[CALLBACK] prenotazione box={self.box_id} variante='{variant}' user={interaction.user.id}")
            try:
                await interaction.response.defer()
                print(f"[CALLBACK] defer OK box={self.box_id}")
            except Exception as e:
                print(f"[CALLBACK] defer FALLITO: {e}")
                return
            try:
                await self._do_reserve(interaction, variant)
            except Exception as e:
                print(f"[RESERVE] Errore inatteso: {type(e).__name__}: {e}")
                try:
                    await interaction.followup.send(
                        "❌ Qualcosa è andato storto, riprova tra qualche secondo.", ephemeral=True)
                except Exception:
                    pass
        return callback

    async def _do_reserve(self, interaction: discord.Interaction, variant: str):
        banned, ban_msg = await is_user_banned(str(interaction.user.id))
        if banned:
            await interaction.followup.send(ban_msg, ephemeral=True)
            return

        print(f"[RESERVE] acquisisco lock box={self.box_id}")
        async with get_lock(self.box_id):
            print(f"[RESERVE] lock acquisito box={self.box_id}")
            box = await load_box(self.box_id)
            if not box:
                await interaction.followup.send("❌ Box non trovata.", ephemeral=True)
                return

            variants = get_variants(box)
            user_id = str(interaction.user.id)

            if variant not in variants:
                await interaction.followup.send("❌ Variante non trovata in questa box.", ephemeral=True)
                return

            if variants[variant]["reserved_by"] is not None:
                box["variants"] = variants
                await self.refresh_box_message(interaction.message, box, interaction)
                await interaction.followup.send(
                    f"⚠️ **{variant}** è già stata prenotata!", ephemeral=True)
                return

            variants[variant]["reserved_by"] = user_id
            variants[variant]["reserved_at"] = datetime.now().isoformat()

            if not await update_variants(self.box_id, variants):
                await interaction.followup.send(
                    "❌ Errore di salvataggio, riprova tra qualche secondo.", ephemeral=True)
                return

            box["variants"] = variants
            await self.refresh_box_message(interaction.message, box, interaction)
            all_reserved = all(v["reserved_by"] is not None for v in variants.values())

        await interaction.followup.send(
            f"🎉 **{interaction.user.display_name}** ha prenotato **{variant}**!")
        if all_reserved:
            await interaction.channel.send(f"🏆 **Box completata!**\n\n{build_summary(box)}")

    async def cancel_callback(self, interaction: discord.Interaction):
        print(f"[CALLBACK] annulla box={self.box_id} user={interaction.user.id}")
        try:
            await interaction.response.defer()
        except Exception as e:
            print(f"[CALLBACK] defer FALLITO: {e}")
            return
        try:
            await self._handle_cancel(interaction)
        except Exception as e:
            print(f"[CANCEL] Errore inatteso: {type(e).__name__}: {e}")
            try:
                await interaction.followup.send(
                    "❌ Qualcosa è andato storto, riprova tra qualche secondo.", ephemeral=True)
            except Exception:
                pass

    async def _handle_cancel(self, interaction: discord.Interaction):
        box = await load_box(self.box_id)
        if not box:
            await interaction.followup.send("❌ Box non trovata.", ephemeral=True)
            return

        variants = get_variants(box)
        user_id = str(interaction.user.id)
        user_variants = [v for v, info in variants.items() if info["reserved_by"] == user_id]

        if not user_variants:
            await interaction.followup.send(
                "ℹ️ Non hai nessuna prenotazione attiva in questa box.", ephemeral=True)
            return

        if len(user_variants) == 1:
            await self._do_cancel(interaction, interaction.message, user_variants[0], user_id)
            return

        select = discord.ui.Select(
            placeholder="Quale variante vuoi annullare?",
            options=[discord.SelectOption(label=v[:100], value=v[:100]) for v in user_variants]
        )
        box_message = interaction.message

        async def select_callback(si: discord.Interaction):
            await si.response.defer()
            chosen = select.values[0]
            try:
                await si.edit_original_response(
                    content=f"↩️ Annullamento di **{chosen}** in corso...", view=None)
            except Exception:
                pass
            try:
                await self._do_cancel(si, box_message, chosen, str(si.user.id))
            except Exception as e:
                print(f"[CANCEL-SELECT] Errore: {type(e).__name__}: {e}")

        select.callback = select_callback
        cv = discord.ui.View(timeout=180)
        cv.add_item(select)
        await interaction.followup.send("Quale prenotazione vuoi annullare?", view=cv, ephemeral=True)

    async def _do_cancel(self, interaction: discord.Interaction, box_message: discord.Message,
                         variant: str, user_id: str):
        was_complete = False
        async with get_lock(self.box_id):
            box = await load_box(self.box_id)
            if not box:
                await interaction.followup.send("❌ Box non trovata.", ephemeral=True)
                return
            variants = get_variants(box)

            if variants.get(variant, {}).get("reserved_by") != user_id:
                box["variants"] = variants
                await self.refresh_box_message(box_message, box, interaction)
                await interaction.followup.send(
                    "⚠️ Questa prenotazione non risulta più tua.", ephemeral=True)
                return

            was_complete = all(v["reserved_by"] is not None for v in variants.values())

            variants[variant]["reserved_by"] = None
            variants[variant]["reserved_at"] = None

            if not await update_variants(self.box_id, variants):
                await interaction.followup.send(
                    "❌ Errore di salvataggio, riprova tra qualche secondo.", ephemeral=True)
                return

            box["variants"] = variants
            await self.refresh_box_message(box_message, box, interaction)

        await interaction.channel.send(
            f"↩️ **{interaction.user.display_name}** ha annullato la prenotazione di **{variant}**.")

        if was_complete:
            ban_msg = await register_infraction(user_id, self.box_id, variant)
            try:
                await interaction.followup.send(ban_msg, ephemeral=True)
            except Exception:
                pass
            await interaction.channel.send(
                f"⚠️ **{interaction.user.display_name}** ha annullato una prenotazione a split **già completato**.")

# ── Comandi ──────────────────────────────────────────────────────
@app_commands.guild_only()
class BlindBoxCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="newbox", description="Crea una nuova full box da splittare")
    @app_commands.describe(
        nome="Nome della serie (es. Skullpanda)",
        serie="Nome della serie/edizione (es. Serie1)",
        varianti="Varianti separate da virgola",
        prezzo="Prezzo per variante in euro (es. 15)",
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    async def newbox(self, interaction: discord.Interaction, nome: str, serie: str, varianti: str, prezzo: float):
        await interaction.response.defer()
        variant_list = [v.strip() for v in varianti.split(",") if v.strip()]

        if len(variant_list) not in [6, 8, 9, 12]:
            await interaction.followup.send(
                f"⚠️ Le varianti devono essere 6, 8, 9 o 12. Hai inserito {len(variant_list)}.", ephemeral=True)
            return

        seen, duplicates = set(), set()
        for v in variant_list:
            if v.lower() in seen:
                duplicates.add(v)
            seen.add(v.lower())
        if duplicates:
            await interaction.followup.send(
                f"⚠️ Varianti duplicate: **{', '.join(duplicates)}**.", ephemeral=True)
            return

        too_long = [v for v in variant_list if len(v) > 75]
        if too_long:
            await interaction.followup.send(
                f"⚠️ Nomi troppo lunghi (max 75 caratteri): **{', '.join(v[:30] for v in too_long)}**", ephemeral=True)
            return

        if prezzo <= 0:
            await interaction.followup.send("⚠️ Il prezzo deve essere maggiore di zero.", ephemeral=True)
            return

        box_id = await generate_unique_box_id()
        box = {
            "name": nome, "series": serie, "prezzo": prezzo,
            "created_by": str(interaction.user.id),
            "created_at": datetime.now().isoformat(),
            "message_id": None,
            "channel_id": str(interaction.channel_id),
            "variants": {v: {"reserved_by": None, "reserved_at": None} for v in variant_list},
        }
        if not await save_box(box_id, box):
            await interaction.followup.send("❌ Errore nel salvataggio della box, riprova.", ephemeral=True)
            return

        view = BoxView(box_id)
        view.populate(box)
        msg = await interaction.followup.send(embed=build_embed(box, box_id), view=view, wait=True)
        await update_message_id(box_id, str(msg.id))
        interaction.client.add_view(view, message_id=msg.id)
        print(f"[NEWBOX] creata box={box_id} msg={msg.id}")

    @app_commands.command(name="listbox", description="Mostra le box attive (non completate)")
    async def listbox(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        all_boxes = await load_all_boxes()
        if not all_boxes:
            await interaction.followup.send("ℹ️ Nessuna box attiva al momento.", ephemeral=True)
            return
        lines = []
        for box_id, box in all_boxes.items():
            variants = get_variants(box)
            total = len(variants)
            taken = sum(1 for v in variants.values() if v["reserved_by"] is not None)
            if taken == total:
                continue
            lines.append(f"• **{box['name']} {box['series']}** — ⏳ {taken}/{total} — "
                         f"{fmt_prezzo(box.get('prezzo','?'))}/var | ID: `{box_id}`")
        if not lines:
            await interaction.followup.send(
                f"ℹ️ Nessuna box incompleta. (Totale nel database: {len(all_boxes)})", ephemeral=True)
            return
        testo = "\n".join(lines[:40])
        if len(lines) > 40:
            testo += f"\n\n_...e altre {len(lines)-40} box._"
        embed = discord.Embed(title="📦 Box attive", description=testo, color=discord.Color.blurple())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="boxinfo", description="Dettagli e prenotazioni di una box")
    @app_commands.describe(box_id="ID della box")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def boxinfo(self, interaction: discord.Interaction, box_id: str):
        await interaction.response.defer(ephemeral=True)
        box = await load_box(box_id)
        if not box:
            await interaction.followup.send("❌ Box non trovata.", ephemeral=True)
            return
        variants = get_variants(box)
        lines = []
        for variant, info in variants.items():
            if info["reserved_by"]:
                lines.append(f"✅ **{variant}** → <@{info['reserved_by']}>")
            else:
                lines.append(f"🎁 **{variant}** → *libera*")
        embed = discord.Embed(
            title=f"📋 {box['name']} — {box['series']}",
            description=f"💰 {fmt_prezzo(box.get('prezzo','?'))} a variante\n\n" + "\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="refreshbox", description="Riallinea il messaggio di una box (admin)")
    @app_commands.describe(box_id="ID della box")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def refreshbox(self, interaction: discord.Interaction, box_id: str):
        await interaction.response.defer(ephemeral=True)
        box = await load_box(box_id)
        if not box:
            await interaction.followup.send("❌ Box non trovata.", ephemeral=True)
            return
        msg_id = box.get("message_id")
        if not msg_id:
            await interaction.followup.send("❌ Nessun messaggio associato.", ephemeral=True)
            return
        try:
            channel = interaction.client.get_channel(int(box["channel_id"])) or interaction.channel
            msg = await channel.fetch_message(int(msg_id))
            view = BoxView(box_id)
            view.populate(box)
            await msg.edit(embed=build_embed(box, box_id), view=view)
            interaction.client.add_view(view, message_id=msg.id)
            await interaction.followup.send("✅ Messaggio riallineato!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Errore: {e}", ephemeral=True)

    @app_commands.command(name="deletebox", description="Elimina una box (solo admin)")
    @app_commands.describe(box_id="ID della box da eliminare")
    @app_commands.checks.has_permissions(administrator=True)
    async def deletebox(self, interaction: discord.Interaction, box_id: str):
        await interaction.response.defer(ephemeral=True)
        box = await load_box(box_id)
        if not box:
            await interaction.followup.send("❌ Box non trovata.", ephemeral=True)
            return
        msg_id = box.get("message_id")
        if msg_id:
            try:
                channel = interaction.client.get_channel(int(box["channel_id"])) or interaction.channel
                msg = await channel.fetch_message(int(msg_id))
                emb = build_embed(box, box_id)
                emb.color = discord.Color.dark_grey()
                emb.title = f"🚫 [ELIMINATA] {emb.title}"
                await msg.edit(embed=emb, view=None)
            except Exception as e:
                print(f"[DELETEBOX] messaggio non aggiornato: {e}")
        await delete_box_db(box_id)
        await interaction.followup.send(f"🗑️ Box `{box_id}` eliminata.", ephemeral=True)

    @app_commands.command(name="unban", description="Sblocca un utente (admin)")
    @app_commands.describe(utente="Utente da sbloccare")
    @app_commands.checks.has_permissions(administrator=True)
    async def unban(self, interaction: discord.Interaction, utente: discord.User):
        await interaction.response.defer(ephemeral=True)
        if not await load_ban(str(utente.id)):
            await interaction.followup.send(f"ℹ️ {utente.mention} non ha infrazioni registrate.", ephemeral=True)
            return
        await delete_ban(str(utente.id))
        await interaction.followup.send(
            f"✅ {utente.mention} sbloccato e contatore azzerato.", ephemeral=True)

    @app_commands.command(name="baninfo", description="Stato ban e storico di un utente (admin)")
    @app_commands.describe(utente="Utente da controllare")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def baninfo(self, interaction: discord.Interaction, utente: discord.User):
        await interaction.response.defer(ephemeral=True)
        record = await load_ban(str(utente.id))
        if not record:
            await interaction.followup.send(f"✅ {utente.mention}: nessuna infrazione.", ephemeral=True)
            return

        permanent = record.get("permanent", False)
        banned_until = record.get("banned_until")
        if permanent:
            stato = "🔴 **BAN PERMANENTE**"
        elif banned_until and parse_dt(banned_until) > datetime.now():
            stato = f"🟠 Bloccato fino al **{parse_dt(banned_until).strftime('%d/%m/%Y %H:%M')}**"
        else:
            stato = "🟢 Nessun blocco attivo"

        lines = [f"**Utente:** {utente.mention}", f"**Stato:** {stato}",
                 f"**Infrazioni:** {record.get('infractions', 0)}"]
        last = record.get("last_infraction_at")
        if last:
            ld = parse_dt(last)
            lines.append(f"**Ultima infrazione:** {ld.strftime('%d/%m/%Y')} ({(datetime.now()-ld).days}g fa)")
            if not permanent:
                lines.append(f"**Contatore si azzera il:** {(ld + timedelta(days=INFRACTION_WINDOW_DAYS)).strftime('%d/%m/%Y')}")

        history = get_history(record)
        if history:
            lines.append("\n**Storico:**")
            for h in history[-5:]:
                lines.append(f"• #{h['infraction_number']} — **{h['variant']}** (box `{h['box_id']}`) "
                             f"il {parse_dt(h['at']).strftime('%d/%m/%Y')}")

        embed = discord.Embed(title="📋 Info ban utente", description="\n".join(lines),
                              color=discord.Color.red() if (permanent or banned_until) else discord.Color.green())
        await interaction.followup.send(embed=embed, ephemeral=True)

    @newbox.error
    @boxinfo.error
    @deletebox.error
    @refreshbox.error
    @unban.error
    @baninfo.error
    async def permission_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "🚫 Non hai i permessi."
        else:
            print(f"[COMMAND] Errore: {type(error).__name__}: {error}")
            msg = "❌ Qualcosa è andato storto, riprova."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass

# ── Bot ──────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True

class BlindBoxBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        global http_client
        http_client = httpx.AsyncClient(timeout=15.0)

        await self.add_cog(BlindBoxCog(self))
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

        all_boxes = await load_all_boxes()
        restored = 0
        for box_id, box in all_boxes.items():
            variants = get_variants(box)
            # Salta le box già completate: non servono più bottoni attivi
            if all(v["reserved_by"] is not None for v in variants.values()):
                continue
            view = BoxView(box_id)
            view.populate(box)
            msg_id = box.get("message_id")
            if msg_id:
                self.add_view(view, message_id=int(msg_id))
            else:
                self.add_view(view)
            restored += 1
        print(f"✅ Comandi sincronizzati. {restored} box attive ripristinate (su {len(all_boxes)} totali).")

    async def on_ready(self):
        print(f"🤖 Bot connesso come {self.user} (ID: {self.user.id}) — VERSIONE: {BOT_VERSION}")

    async def on_interaction(self, interaction: discord.Interaction):
        """Traccia OGNI interazione in arrivo (non interferisce con le view)."""
        try:
            if interaction.type == discord.InteractionType.component:
                cid = (interaction.data or {}).get("custom_id")
                print(f"[INTERACTION] componente custom_id='{cid}' user={interaction.user.id} msg={interaction.message.id if interaction.message else None}")
        except Exception as e:
            print(f"[INTERACTION] errore log: {e}")

    async def close(self):
        if http_client:
            await http_client.aclose()
        await super().close()

bot = BlindBoxBot()

if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("❌ DISCORD_TOKEN non trovato!")
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("❌ SUPABASE_URL o SUPABASE_KEY non trovati!")
    bot.run(TOKEN)
