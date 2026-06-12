import discord
from discord.ext import commands
from discord import app_commands
import os
import json
import time
from datetime import datetime
import httpx

TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def load_all_boxes() -> dict:
    url = f"{SUPABASE_URL}/rest/v1/boxes?select=*"
    r = httpx.get(url, headers=sb_headers())
    rows = r.json()
    if not isinstance(rows, list):
        return {}
    return {row["box_id"]: row for row in rows}

def load_box(box_id: str) -> dict | None:
    url = f"{SUPABASE_URL}/rest/v1/boxes?box_id=eq.{box_id}&select=*"
    r = httpx.get(url, headers=sb_headers())
    rows = r.json()
    if not isinstance(rows, list) or len(rows) == 0:
        return None
    return rows[0]

def save_box(box_id: str, box: dict):
    url = f"{SUPABASE_URL}/rest/v1/boxes"
    headers = {**sb_headers(), "Prefer": "resolution=merge-duplicates,return=representation"}
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
    r = httpx.post(url, headers=headers, json=payload)
    print(f"[SAVE_BOX] status={r.status_code}")

def delete_box(box_id: str):
    url = f"{SUPABASE_URL}/rest/v1/boxes?box_id=eq.{box_id}"
    httpx.delete(url, headers=sb_headers())

def update_variants(box_id: str, variants: dict):
    url = f"{SUPABASE_URL}/rest/v1/boxes?box_id=eq.{box_id}"
    headers = {**sb_headers(), "Prefer": "return=representation"}
    httpx.patch(url, headers=headers, json={"variants": json.dumps(variants)})

def update_message_id(box_id: str, message_id: str):
    url = f"{SUPABASE_URL}/rest/v1/boxes?box_id=eq.{box_id}"
    headers = {**sb_headers(), "Prefer": "return=representation"}
    httpx.patch(url, headers=headers, json={"message_id": message_id})

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
        timestamp=datetime.fromisoformat(box["created_at"]),
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
        uid = info["reserved_by"]
        lines.append(f"• **{variant}** → <@{uid}>")
    return "\n".join(lines)

class BoxView(discord.ui.View):
    def __init__(self, box_id: str):
        super().__init__(timeout=None)
        self.box_id = box_id
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        box = load_box(self.box_id)
        if not box:
            return
        variants = get_variants(box)
        variant_names = list(variants.keys())

        for i, variant in enumerate(variant_names):
            info = variants[variant]
            taken = info["reserved_by"] is not None
            btn = discord.ui.Button(
                label=f"{'✅' if taken else '🎁'} {variant}",
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

    def _make_callback(self, variant: str):
        async def callback(interaction: discord.Interaction):
            box = load_box(self.box_id)
            if not box:
                await interaction.response.send_message("❌ Box non trovata.", ephemeral=True)
                return
            variants = get_variants(box)
            user_id = str(interaction.user.id)
            if variants[variant]["reserved_by"] is not None:
                await interaction.response.send_message(
                    f"⚠️ **{variant}** è già stata prenotata!", ephemeral=True)
                return
            variants[variant]["reserved_by"] = user_id
            variants[variant]["reserved_at"] = datetime.now().isoformat()
            update_variants(self.box_id, variants)
            box["variants"] = variants
            embed = build_embed(box, self.box_id)
            self._build_buttons()
            all_reserved = all(v["reserved_by"] is not None for v in variants.values())
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(f"🎉 **{interaction.user.display_name}** ha prenotato **{variant}**!")
            if all_reserved:
                await interaction.channel.send(f"🏆 **Box completata!**\n\n{build_summary(box)}")
        return callback

    async def cancel_callback(self, interaction: discord.Interaction):
        box = load_box(self.box_id)
        if not box:
            await interaction.response.send_message("❌ Box non trovata.", ephemeral=True)
            return
        variants = get_variants(box)
        user_id = str(interaction.user.id)
        user_variants = [v for v, info in variants.items() if info["reserved_by"] == user_id]

        if not user_variants:
            await interaction.response.send_message("ℹ️ Non hai nessuna prenotazione attiva in questa box.", ephemeral=True)
            return

        if len(user_variants) == 1:
            found = user_variants[0]
            variants[found]["reserved_by"] = None
            variants[found]["reserved_at"] = None
            update_variants(self.box_id, variants)
            box["variants"] = variants
            embed = build_embed(box, self.box_id)
            self._build_buttons()
            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(
                f"↩️ **{interaction.user.display_name}** ha annullato la prenotazione di **{found}**."
            )
            return

        # Più prenotazioni: menu di selezione
        select = discord.ui.Select(
            placeholder="Quale variante vuoi annullare?",
            options=[discord.SelectOption(label=v, value=v) for v in user_variants]
        )

        async def select_callback(si: discord.Interaction):
            chosen = select.values[0]
            b2 = load_box(self.box_id)
            v2 = get_variants(b2)

            # Verifica che la prenotazione sia ancora dell'utente
            if v2.get(chosen, {}).get("reserved_by") != str(si.user.id):
                await si.response.edit_message(content="⚠️ Questa prenotazione non risulta più tua.", view=None)
                return

            v2[chosen]["reserved_by"] = None
            v2[chosen]["reserved_at"] = None
            update_variants(self.box_id, v2)
            b2["variants"] = v2

            # Conferma nel messaggio effimero del menu
            await si.response.edit_message(content=f"↩️ Prenotazione di **{chosen}** annullata!", view=None)

            # Aggiorna il messaggio PRINCIPALE della box (non quello effimero!)
            self._build_buttons()
            try:
                msg_id = b2.get("message_id")
                if msg_id:
                    box_msg = await si.channel.fetch_message(int(msg_id))
                    await box_msg.edit(embed=build_embed(b2, self.box_id), view=self)
            except Exception as e:
                print(f"[CANCEL] Errore aggiornamento messaggio box: {e}")

            # Notifica pubblica nel canale
            await si.channel.send(f"↩️ **{si.user.display_name}** ha annullato la prenotazione di **{chosen}**.")

        select.callback = select_callback
        cv = discord.ui.View(timeout=180)
        cv.add_item(select)
        await interaction.response.send_message("Quale prenotazione vuoi annullare?", view=cv, ephemeral=True)

class BlindBoxCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="newbox", description="Crea una nuova full box da splittare")
    @app_commands.describe(
        nome="Nome della serie (es. Skullpanda)",
        serie="Nome della serie/edizione (es. Serie1)",
        varianti="Varianti separate da virgola (es. Rosso,Blu,Verde,Giallo,Nero,Bianco)",
        prezzo="Prezzo per variante in euro (es. 15)",
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    async def newbox(self, interaction: discord.Interaction, nome: str, serie: str, varianti: str, prezzo: float):
        variant_list = [v.strip() for v in varianti.split(",") if v.strip()]
        if len(variant_list) not in [6, 8, 9, 12]:
            await interaction.response.send_message(
                f"⚠️ Le varianti devono essere 6, 8, 9 o 12. Hai inserito {len(variant_list)}.", ephemeral=True)
            return

        box_id = str(int(time.time()))[-8:]
        box = {
            "name": nome, "series": serie, "prezzo": prezzo,
            "created_by": str(interaction.user.id),
            "created_at": datetime.now().isoformat(),
            "message_id": None,
            "channel_id": str(interaction.channel_id),
            "variants": {v: {"reserved_by": None, "reserved_at": None} for v in variant_list},
        }
        save_box(box_id, box)

        view = BoxView(box_id)
        embed = build_embed(box, box_id)
        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        update_message_id(box_id, str(msg.id))
        interaction.client.add_view(view)

    @app_commands.command(name="listbox", description="Mostra tutte le box attive")
    async def listbox(self, interaction: discord.Interaction):
        all_boxes = load_all_boxes()
        if not all_boxes:
            await interaction.response.send_message("ℹ️ Nessuna box attiva al momento.", ephemeral=True)
            return
        lines = []
        for box_id, box in all_boxes.items():
            variants = get_variants(box)
            total = len(variants)
            taken = sum(1 for v in variants.values() if v["reserved_by"] is not None)
            status = "✅ Completa" if taken == total else f"⏳ {taken}/{total}"
            prezzo_str = fmt_prezzo(box.get("prezzo", "?"))
            lines.append(f"• **{box['name']} {box['series']}** — {status} — {prezzo_str}/var | ID: `{box_id}`")
        embed = discord.Embed(title="📦 Box attive", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="boxinfo", description="Dettagli e prenotazioni di una box")
    @app_commands.describe(box_id="ID della box (usa /listbox per trovarlo)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def boxinfo(self, interaction: discord.Interaction, box_id: str):
        box = load_box(box_id)
        if not box:
            await interaction.response.send_message("❌ Box non trovata.", ephemeral=True)
            return
        variants = get_variants(box)
        prezzo_str = fmt_prezzo(box.get("prezzo", "?"))
        lines = []
        for variant, info in variants.items():
            if info["reserved_by"]:
                lines.append(f"✅ **{variant}** → <@{info['reserved_by']}> ({info['reserved_at'][:10]})")
            else:
                lines.append(f"🎁 **{variant}** → *libera*")
        embed = discord.Embed(
            title=f"📋 {box['name']} — {box['series']}",
            description=f"💰 {prezzo_str} a variante\n\n" + "\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="deletebox", description="Elimina una box (solo admin)")
    @app_commands.describe(box_id="ID della box da eliminare")
    @app_commands.checks.has_permissions(administrator=True)
    async def deletebox(self, interaction: discord.Interaction, box_id: str):
        box = load_box(box_id)
        if not box:
            await interaction.response.send_message("❌ Box non trovata.", ephemeral=True)
            return
        delete_box(box_id)
        await interaction.response.send_message(f"🗑️ Box `{box_id}` eliminata.", ephemeral=True)

    @newbox.error
    @boxinfo.error
    @deletebox.error
    async def permission_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("🚫 Non hai i permessi.", ephemeral=True)

intents = discord.Intents.default()
intents.message_content = True

class BlindBoxBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.add_cog(BlindBoxCog(self))
        guild = discord.Object(id=1442484265475506207)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        all_boxes = load_all_boxes()
        for box_id in all_boxes:
            self.add_view(BoxView(box_id))
        print(f"✅ Comandi slash sincronizzati sul server. {len(all_boxes)} box ripristinate da Supabase.")

    async def on_ready(self):
        print(f"🤖 Bot connesso come {self.user} (ID: {self.user.id})")

bot = BlindBoxBot()

if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("❌ DISCORD_TOKEN non trovato!")
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("❌ SUPABASE_URL o SUPABASE_KEY non trovati!")
    bot.run(TOKEN)
