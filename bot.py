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

BOT_VERSION = "v19-direct-response"

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
    return {row["box_id"]: row for row in rows} if isinstance(rows, list) else {}

async def load_box(box_id: str) -> dict | None:
    url = f"{SUPABASE_URL}/rest/v1/boxes?box_id=eq.{box_id}&select=*"
    r = await http_client.get(url, headers=sb_headers())
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        return None
    return rows[0]

async def save_box(box_id: str, box: dict) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/boxes"
    headers = {**sb_headers(), "Prefer": "return=representation"}
    payload = {
        "box_id": box_id, "name": box["name"], "series": box["series"],
        "prezzo": box["prezzo"], "created_by": box["created_by"],
        "created_at": box["created_at"], "message_id": box.get("message_id"),
        "channel_id": box["channel_id"], "variants": json.dumps(box["variants"]),
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
    url = f"{SUPABASE_URL}/rest/v1/boxes?box_id=eq.{box_id}"
    headers = {**sb_headers(), "Prefer": "return=representation"}
    r = await http_client.patch(url, headers=headers, json={"variants": json.dumps(variants)})
    if r.status_code not in (200, 204):
        print(f"[UPDATE_VARIANTS] ERRORE status={r.status_code} body={r.text[:200]}")
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
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=None) if dt.tzinfo else dt

async def load_ban(user_id: str) -> dict | None:
    url = f"{SUPABASE_URL}/rest/v1/bans?user_id=eq.{user_id}&select=*"
    r = await http_client.get(url, headers=sb_headers())
    if r.status_code != 200:
        print(f"[LOAD_BAN] ERRORE status={r.status_code} body={r.text[:200]}")
        return None
    rows = r.json()
    if not isinstance(rows, list) or not rows:
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
        print(f"[IS_BANNED] Errore, procedo senza blocco: {e}")
        return False, None
    if not record:
        return False, None
    if record.get("permanent"):
        return True, "🚫 Sei stato bannato permanentemente dal sistema di prenotazioni. Contatta lo staff."
    bu = record.get("banned_until")
    if bu:
        try:
            until = parse_dt(bu)
        except Exception:
            return False, None
        if datetime.now() < until:
            d = until - datetime.now()
            return True, (f"🚫 Sei temporaneamente bloccato dalle prenotazioni "
                          f"(ancora ~{d.days}g {int(d.total_seconds()//3600)%24}h). "
                          f"Motivo: annullamento a split completato.")
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
    record["user_id"] = user_id

    history = get_history(record)
    history.append({"box_id": box_id, "variant": variant, "at": now.isoformat(),
                    "infraction_number": record["infractions"]})
    record["history"] = history

    n = record["infractions"]
    if n >= 3:
        record["permanent"] = True
        record["banned_until"] = None
        msg = "🚫 **Ban permanente.** Hai annullato a split completato per la 3ª volta. Solo un admin può sbloccarti."
    else:
        g = BAN_DURATIONS[n]
        record["banned_until"] = (now + timedelta(days=g)).isoformat()
        record["permanent"] = False
        msg = (f"🚫 **Sei stato bloccato per {g} giorni** dalle prenotazioni (infrazione #{n}). "
               f"Annullare a split completato penalizza tutti gli altri partecipanti.")
    await save_ban(record)
    return msg

# ── Helpers ──────────────────────────────────────────────────────
def fmt_prezzo(p) -> str:
    try:
        return f"{float(p):.2f}€".replace(".", ",")
    except Exception:
        return f"{p}€"

def get_variants(box: dict) -> dict:
    v = box["variants"]
    return json.loads(v) if isinstance(v, str) else v

def build_embed(box: dict, box_id: str) -> discord.Embed:
    variants = get_variants(box)
    total = len(variants)
    taken = sum(1 for v in variants.values() if v["reserved_by"] is not None)
    embed = discord.Embed(
        title=f"🎁 {box['name']} — {box['series']}",
        description=f"**{taken}/{total}** varianti prenotate  •  💰 **{fmt_prezzo(box.get('prezzo','?'))} a variante**",
        color=discord.Color.green() if taken == total else discord.Color.blurple(),
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
    lines = [f"**Riepilogo prenotazioni** — {fmt_prezzo(box.get('prezzo','?'))} a variante:"]
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
        cancel = discord.ui.Button(
            label="❌ Annulla la mia prenotazione",
            style=discord.ButtonStyle.danger,
            custom_id=f"cx_{self.box_id}", row=4,
        )
        cancel.callback = self.cancel_callback
        self.add_item(cancel)

    async def show_state(self, interaction: discord.Interaction, box_message: discord.Message, box: dict):
        """Aggiorna embed+bottoni. Se possibile risponde DIRETTAMENTE all'interazione
        (sblocca subito il bottone lato client), altrimenti modifica il messaggio."""
        self.populate(box)
        embed = build_embed(box, self.box_id)
        same_msg = (interaction.message is not None and box_message is not None
                    and interaction.message.id == box_message.id)
        try:
            if same_msg and not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
                print(f"[STATE] box={self.box_id} risposta diretta OK")
            else:
                await box_message.edit(embed=embed, view=self)
                print(f"[STATE] box={self.box_id} edit messaggio OK")
        except discord.NotFound:
            print(f"[STATE] box={self.box_id} interazione scaduta, fallback su edit")
            await box_message.edit(embed=embed, view=self)

    async def reply(self, interaction: discord.Interaction, content: str, ephemeral: bool = True):
        """Risponde in modo sicuro, sia che l'interazione sia già stata usata o no."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(content, ephemeral=ephemeral)
        except Exception as e:
            print(f"[REPLY] fallito: {e}")

    # ── Prenotazione ──
    def _make_callback(self, variant: str):
        async def callback(interaction: discord.Interaction):
            t0 = time.monotonic()
            print(f"[CLICK] prenota box={self.box_id} var='{variant}' user={interaction.user.id}")
            try:
                await self._do_reserve(interaction, variant)
                print(f"[CLICK] completato in {time.monotonic()-t0:.2f}s")
            except discord.NotFound:
                print(f"[CLICK] interazione scaduta (>3s) box={self.box_id}")
            except Exception as e:
                print(f"[RESERVE] Errore: {type(e).__name__}: {e}")
                await self.reply(interaction, "❌ Qualcosa è andato storto, riprova tra qualche secondo.")
        return callback

    async def _do_reserve(self, interaction: discord.Interaction, variant: str):
        user_id = str(interaction.user.id)
        box_message = interaction.message

        banned, ban_msg = await is_user_banned(user_id)
        if banned:
            await self.reply(interaction, ban_msg)
            return

        async with get_lock(self.box_id):
            box = await load_box(self.box_id)
            if not box:
                await self.reply(interaction, "❌ Box non trovata.")
                return

            variants = get_variants(box)
            if variant not in variants:
                await self.reply(interaction, "❌ Variante non trovata in questa box.")
                return

            if variants[variant]["reserved_by"] is not None:
                box["variants"] = variants
                await self.show_state(interaction, box_message, box)
                await self.reply(interaction, f"⚠️ **{variant}** è già stata prenotata!")
                return

            variants[variant]["reserved_by"] = user_id
            variants[variant]["reserved_at"] = datetime.now().isoformat()

            if not await update_variants(self.box_id, variants):
                await self.reply(interaction, "❌ Errore di salvataggio, riprova tra qualche secondo.")
                return

            box["variants"] = variants
            await self.show_state(interaction, box_message, box)
            all_reserved = all(v["reserved_by"] is not None for v in variants.values())

        await interaction.followup.send(
            f"🎉 **{interaction.user.display_name}** ha prenotato **{variant}**!")
        if all_reserved:
            await interaction.channel.send(f"🏆 **Box completata!**\n\n{build_summary(box)}")

    # ── Annullamento ──
    async def cancel_callback(self, interaction: discord.Interaction):
        print(f"[CLICK] annulla box={self.box_id} user={interaction.user.id}")
        try:
            await self._handle_cancel(interaction)
        except discord.NotFound:
            print(f"[CLICK] interazione scaduta (>3s) box={self.box_id}")
        except Exception as e:
            print(f"[CANCEL] Errore: {type(e).__name__}: {e}")
            await self.reply(interaction, "❌ Qualcosa è andato storto, riprova tra qualche secondo.")

    async def _handle_cancel(self, interaction: discord.Interaction):
        user_id = str(interaction.user.id)
        box_message = interaction.message

        box = await load_box(self.box_id)
        if not box:
            await self.reply(interaction, "❌ Box non trovata.")
            return

        variants = get_variants(box)
        mine = [v for v, info in variants.items() if info["reserved_by"] == user_id]

        if not mine:
            await self.reply(interaction, "ℹ️ Non hai nessuna prenotazione attiva in questa box.")
            return

        if len(mine) == 1:
            await self._do_cancel(interaction, box_message, mine[0], user_id)
            return

        # Più prenotazioni: menu di scelta (risposta diretta = bottone sbloccato subito)
        select = discord.ui.Select(
            placeholder="Quale variante vuoi annullare?",
            options=[discord.SelectOption(label=v[:100], value=v[:100]) for v in mine],
        )

        async def select_callback(si: discord.Interaction):
            chosen = select.values[0]
            try:
                await si.response.edit_message(
                    content=f"↩️ Annullamento di **{chosen}** in corso...", view=None)
            except Exception as e:
                print(f"[CANCEL-SELECT] risposta fallita: {e}")
            try:
                await self._do_cancel(si, box_message, chosen, str(si.user.id))
            except Exception as e:
                print(f"[CANCEL-SELECT] Errore: {type(e).__name__}: {e}")

        select.callback = select_callback
        cv = discord.ui.View(timeout=180)
        cv.add_item(select)
        await interaction.response.send_message(
            "Quale prenotazione vuoi annullare?", view=cv, ephemeral=True)

    async def _do_cancel(self, interaction: discord.Interaction, box_message: discord.Message,
                         variant: str, user_id: str):
        was_complete = False
        async with get_lock(self.box_id):
            box = await load_box(self.box_id)
            if not box:
                await self.reply(interaction, "❌ Box non trovata.")
                return
            variants = get_variants(box)

            if variants.get(variant, {}).get("reserved_by") != user_id:
                box["variants"] = variants
                await self.show_state(interaction, box_message, box)
                await self.reply(interaction, "⚠️ Questa prenotazione non risulta più tua.")
                return

            was_complete = all(v["reserved_by"] is not None for v in variants.values())
            variants[variant]["reserved_by"] = None
            variants[variant]["reserved_at"] = None

            if not await update_variants(self.box_id, variants):
                await self.reply(interaction, "❌ Errore di salvataggio, riprova tra qualche secondo.")
                return

            box["variants"] = variants
            await self.show_state(interaction, box_message, box)

        await interaction.channel.send(
            f"↩️ **{interaction.user.display_name}** ha annullato la prenotazione di **{variant}**.")

        if was_complete:
            ban_msg = await register_infraction(user_id, self.box_id, variant)
            await self.reply(interaction, ban_msg)
            await interaction.channel.send(
                f"⚠️ **{interaction.user.display_name}** ha annullato una prenotazione a split **già completato**.")

# ── Comandi ──────────────────────────────────────────────────────
@app_commands.guild_only()
class BlindBoxCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="newbox", description="Crea una nuova full box da splittare")
    @app_commands.describe(nome="Nome (es. Skullpanda)", serie="Serie/edizione",
                           varianti="Varianti separate da virgola", prezzo="Prezzo per variante")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def newbox(self, interaction: discord.Interaction, nome: str, serie: str, varianti: str, prezzo: float):
        await interaction.response.defer()
        vl = [v.strip() for v in varianti.split(",") if v.strip()]

        if len(vl) not in [6, 8, 9, 12]:
            await interaction.followup.send(
                f"⚠️ Le varianti devono essere 6, 8, 9 o 12. Hai inserito {len(vl)}.", ephemeral=True)
            return
        seen, dup = set(), set()
        for v in vl:
            if v.lower() in seen:
                dup.add(v)
            seen.add(v.lower())
        if dup:
            await interaction.followup.send(f"⚠️ Varianti duplicate: **{', '.join(dup)}**.", ephemeral=True)
            return
        if any(len(v) > 75 for v in vl):
            await interaction.followup.send("⚠️ Alcuni nomi superano i 75 caratteri.", ephemeral=True)
            return
        if prezzo <= 0:
            await interaction.followup.send("⚠️ Il prezzo deve essere maggiore di zero.", ephemeral=True)
            return

        box_id = await generate_unique_box_id()
        box = {
            "name": nome, "series": serie, "prezzo": prezzo,
            "created_by": str(interaction.user.id),
            "created_at": datetime.now().isoformat(),
            "message_id": None, "channel_id": str(interaction.channel_id),
            "variants": {v: {"reserved_by": None, "reserved_at": None} for v in vl},
        }
        if not await save_box(box_id, box):
            await interaction.followup.send("❌ Errore nel salvataggio, riprova.", ephemeral=True)
            return

        view = BoxView(box_id)
        view.populate(box)
        msg = await interaction.followup.send(embed=build_embed(box, box_id), view=view, wait=True)
        await update_message_id(box_id, str(msg.id))
        interaction.client.add_view(view, message_id=msg.id)
        print(f"[NEWBOX] box={box_id} msg={msg.id}")

    @app_commands.command(name="listbox", description="Mostra le box ancora aperte")
    async def listbox(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        allb = await load_all_boxes()
        lines = []
        for box_id, box in allb.items():
            variants = get_variants(box)
            taken = sum(1 for v in variants.values() if v["reserved_by"] is not None)
            if taken == len(variants):
                continue
            lines.append(f"• **{box['name']} {box['series']}** — ⏳ {taken}/{len(variants)} — "
                         f"{fmt_prezzo(box.get('prezzo','?'))}/var | `{box_id}`")
        if not lines:
            await interaction.followup.send(
                f"ℹ️ Nessuna box aperta. (Totale nel database: {len(allb)})", ephemeral=True)
            return
        testo = "\n".join(lines[:40])
        if len(lines) > 40:
            testo += f"\n\n_...e altre {len(lines)-40} box aperte._"
        await interaction.followup.send(
            embed=discord.Embed(title="📦 Box aperte", description=testo, color=discord.Color.blurple()),
            ephemeral=True)

    @app_commands.command(name="boxinfo", description="Dettagli e prenotazioni di una box")
    @app_commands.describe(box_id="ID della box")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def boxinfo(self, interaction: discord.Interaction, box_id: str):
        await interaction.response.defer(ephemeral=True)
        box = await load_box(box_id)
        if not box:
            await interaction.followup.send("❌ Box non trovata.", ephemeral=True)
            return
        lines = []
        for variant, info in get_variants(box).items():
            lines.append(f"✅ **{variant}** → <@{info['reserved_by']}>" if info["reserved_by"]
                         else f"🎁 **{variant}** → *libera*")
        await interaction.followup.send(embed=discord.Embed(
            title=f"📋 {box['name']} — {box['series']}",
            description=f"💰 {fmt_prezzo(box.get('prezzo','?'))} a variante\n\n" + "\n".join(lines),
            color=discord.Color.gold()), ephemeral=True)

    @app_commands.command(name="refreshbox", description="Riallinea il messaggio di una box (admin)")
    @app_commands.describe(box_id="ID della box")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def refreshbox(self, interaction: discord.Interaction, box_id: str):
        await interaction.response.defer(ephemeral=True)
        box = await load_box(box_id)
        if not box:
            await interaction.followup.send("❌ Box non trovata.", ephemeral=True)
            return
        if not box.get("message_id"):
            await interaction.followup.send("❌ Nessun messaggio associato.", ephemeral=True)
            return
        try:
            channel = interaction.client.get_channel(int(box["channel_id"])) or interaction.channel
            msg = await channel.fetch_message(int(box["message_id"]))
            view = BoxView(box_id)
            view.populate(box)
            await msg.edit(embed=build_embed(box, box_id), view=view)
            interaction.client.add_view(view, message_id=msg.id)
            await interaction.followup.send("✅ Messaggio riallineato!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Errore: {e}", ephemeral=True)

    @app_commands.command(name="deletebox", description="Elimina una box (solo admin)")
    @app_commands.describe(box_id="ID della box")
    @app_commands.checks.has_permissions(administrator=True)
    async def deletebox(self, interaction: discord.Interaction, box_id: str):
        await interaction.response.defer(ephemeral=True)
        box = await load_box(box_id)
        if not box:
            await interaction.followup.send("❌ Box non trovata.", ephemeral=True)
            return
        if box.get("message_id"):
            try:
                channel = interaction.client.get_channel(int(box["channel_id"])) or interaction.channel
                msg = await channel.fetch_message(int(box["message_id"]))
                emb = build_embed(box, box_id)
                emb.color = discord.Color.dark_grey()
                emb.title = f"🚫 [ELIMINATA] {emb.title}"
                await msg.edit(embed=emb, view=None)
            except Exception as e:
                print(f"[DELETEBOX] messaggio non aggiornato: {e}")
        await delete_box_db(box_id)
        await interaction.followup.send(f"🗑️ Box `{box_id}` eliminata.", ephemeral=True)

    @app_commands.command(name="cleanup", description="Rimuove dal DB le box completate più vecchie di N giorni (admin)")
    @app_commands.describe(giorni="Età minima in giorni (default 30)")
    @app_commands.checks.has_permissions(administrator=True)
    async def cleanup(self, interaction: discord.Interaction, giorni: int = 30):
        await interaction.response.defer(ephemeral=True)
        allb = await load_all_boxes()
        limite = datetime.now() - timedelta(days=giorni)
        rimosse = 0
        for box_id, box in allb.items():
            variants = get_variants(box)
            if not all(v["reserved_by"] is not None for v in variants.values()):
                continue  # solo box completate
            try:
                if parse_dt(box["created_at"]) > limite:
                    continue
            except Exception:
                continue
            await delete_box_db(box_id)
            rimosse += 1
        await interaction.followup.send(
            f"🧹 Rimosse **{rimosse}** box completate più vecchie di {giorni} giorni.\n"
            f"Restano **{len(allb)-rimosse}** box nel database.", ephemeral=True)

    @app_commands.command(name="unban", description="Sblocca un utente (admin)")
    @app_commands.describe(utente="Utente da sbloccare")
    @app_commands.checks.has_permissions(administrator=True)
    async def unban(self, interaction: discord.Interaction, utente: discord.User):
        await interaction.response.defer(ephemeral=True)
        if not await load_ban(str(utente.id)):
            await interaction.followup.send(f"ℹ️ {utente.mention} non ha infrazioni.", ephemeral=True)
            return
        await delete_ban(str(utente.id))
        await interaction.followup.send(f"✅ {utente.mention} sbloccato e contatore azzerato.", ephemeral=True)

    @app_commands.command(name="baninfo", description="Stato ban e storico di un utente (admin)")
    @app_commands.describe(utente="Utente da controllare")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def baninfo(self, interaction: discord.Interaction, utente: discord.User):
        await interaction.response.defer(ephemeral=True)
        rec = await load_ban(str(utente.id))
        if not rec:
            await interaction.followup.send(f"✅ {utente.mention}: nessuna infrazione.", ephemeral=True)
            return
        perm = rec.get("permanent", False)
        bu = rec.get("banned_until")
        if perm:
            stato = "🔴 **BAN PERMANENTE**"
        elif bu and parse_dt(bu) > datetime.now():
            stato = f"🟠 Bloccato fino al **{parse_dt(bu).strftime('%d/%m/%Y %H:%M')}**"
        else:
            stato = "🟢 Nessun blocco attivo"
        lines = [f"**Utente:** {utente.mention}", f"**Stato:** {stato}",
                 f"**Infrazioni:** {rec.get('infractions', 0)}"]
        if rec.get("last_infraction_at"):
            ld = parse_dt(rec["last_infraction_at"])
            lines.append(f"**Ultima infrazione:** {ld.strftime('%d/%m/%Y')} ({(datetime.now()-ld).days}g fa)")
            if not perm:
                lines.append(f"**Contatore azzerato il:** {(ld+timedelta(days=INFRACTION_WINDOW_DAYS)).strftime('%d/%m/%Y')}")
        hist = get_history(rec)
        if hist:
            lines.append("\n**Storico:**")
            for h in hist[-5:]:
                lines.append(f"• #{h['infraction_number']} — **{h['variant']}** (box `{h['box_id']}`) "
                             f"il {parse_dt(h['at']).strftime('%d/%m/%Y')}")
        await interaction.followup.send(embed=discord.Embed(
            title="📋 Info ban utente", description="\n".join(lines),
            color=discord.Color.red() if (perm or bu) else discord.Color.green()), ephemeral=True)

    @newbox.error
    @boxinfo.error
    @deletebox.error
    @refreshbox.error
    @cleanup.error
    @unban.error
    @baninfo.error
    async def permission_error(self, interaction: discord.Interaction, error):
        msg = "🚫 Non hai i permessi." if isinstance(error, app_commands.MissingPermissions) \
              else "❌ Qualcosa è andato storto, riprova."
        if not isinstance(error, app_commands.MissingPermissions):
            print(f"[COMMAND] Errore: {type(error).__name__}: {error}")
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

        allb = await load_all_boxes()
        restored = 0
        for box_id, box in allb.items():
            variants = get_variants(box)
            if all(v["reserved_by"] is not None for v in variants.values()):
                continue
            view = BoxView(box_id)
            view.populate(box)
            if box.get("message_id"):
                self.add_view(view, message_id=int(box["message_id"]))
            else:
                self.add_view(view)
            restored += 1
        print(f"✅ Comandi sincronizzati. {restored} box aperte ripristinate (su {len(allb)} totali).")

    async def on_ready(self):
        print(f"🤖 Bot connesso come {self.user} (ID: {self.user.id}) — VERSIONE: {BOT_VERSION}")

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
