import discord
from discord.ext import commands
from discord import app_commands
import json
import os
from datetime import datetime

# ── Configurazione ──────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")          # token dal .env
DATA_FILE = "data/boxes.json"               # persistenza JSON

# ── Utilità JSON ────────────────────────────────────────────────
def load_data():
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data):
    os.makedirs("data", exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ── Intents & Bot ───────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True

class BlindBoxBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.add_cog(BlindBoxCog(self))
        await self.tree.sync()
        print("✅ Comandi slash sincronizzati.")

    async def on_ready(self):
        print(f"🤖 Bot connesso come {self.user} (ID: {self.user.id})")

bot = BlindBoxBot()

# ── View con bottoni varianti ────────────────────────────────────
class BoxView(discord.ui.View):
    def __init__(self, box_id: str):
        super().__init__(timeout=None)
        self.box_id = box_id
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        data = load_data()
        box = data.get(self.box_id)
        if not box:
            return
        for variant, info in box["variants"].items():
            taken = info["reserved_by"] is not None
            btn = discord.ui.Button(
                label=f"{'✅' if taken else '🎁'} {variant}",
                style=discord.ButtonStyle.success if taken else discord.ButtonStyle.primary,
                custom_id=f"reserve_{self.box_id}_{variant}",
                disabled=taken,
            )
            btn.callback = self._make_callback(variant)
            self.add_item(btn)

        # Bottone annulla prenotazione
        cancel_btn = discord.ui.Button(
            label="❌ Annulla la mia prenotazione",
            style=discord.ButtonStyle.danger,
            custom_id=f"cancel_{self.box_id}",
            row=4,
        )
        cancel_btn.callback = self.cancel_callback
        self.add_item(cancel_btn)

    def _make_callback(self, variant: str):
        async def callback(interaction: discord.Interaction):
            data = load_data()
            box = data.get(self.box_id)
            if not box:
                await interaction.response.send_message("❌ Box non trovata.", ephemeral=True)
                return

            user_id = str(interaction.user.id)

            # Controlla se l'utente ha già prenotato qualcosa in questa box
            for v, info in box["variants"].items():
                if info["reserved_by"] == user_id:
                    await interaction.response.send_message(
                        f"⚠️ Hai già prenotato **{v}** in questa box! Annulla prima quella prenotazione.",
                        ephemeral=True
                    )
                    return

            # Controlla se la variante è ancora disponibile
            if box["variants"][variant]["reserved_by"] is not None:
                await interaction.response.send_message(
                    f"⚠️ **{variant}** è già stata prenotata da qualcun altro. Scegli un'altra variante!",
                    ephemeral=True
                )
                return

            # Prenota
            box["variants"][variant]["reserved_by"] = user_id
            box["variants"][variant]["reserved_at"] = datetime.now().isoformat()
            save_data(data)

            # Aggiorna il messaggio
            embed = build_embed(box, self.box_id)
            self._build_buttons()

            # Controlla se la box è completa
            all_reserved = all(v["reserved_by"] is not None for v in box["variants"].values())

            await interaction.response.edit_message(embed=embed, view=self)
            await interaction.followup.send(
                f"🎉 **{interaction.user.display_name}** ha prenotato **{variant}**!", ephemeral=False
            )

            if all_reserved:
                summary = build_summary(box)
                await interaction.channel.send(
                    f"🏆 **Box completata!** Tutte le varianti sono state prenotate!\n\n{summary}"
                )

        return callback

    async def cancel_callback(self, interaction: discord.Interaction):
        data = load_data()
        box = data.get(self.box_id)
        if not box:
            await interaction.response.send_message("❌ Box non trovata.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        found = None
        for variant, info in box["variants"].items():
            if info["reserved_by"] == user_id:
                found = variant
                break

        if not found:
            await interaction.response.send_message("ℹ️ Non hai nessuna prenotazione attiva in questa box.", ephemeral=True)
            return

        box["variants"][found]["reserved_by"] = None
        box["variants"][found]["reserved_at"] = None
        save_data(data)

        embed = build_embed(box, self.box_id)
        self._build_buttons()
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"↩️ **{interaction.user.display_name}** ha annullato la prenotazione di **{found}**."
        )


# ── Helpers embed ────────────────────────────────────────────────
def build_embed(box: dict, box_id: str) -> discord.Embed:
    total = len(box["variants"])
    taken = sum(1 for v in box["variants"].values() if v["reserved_by"] is not None)
    color = discord.Color.green() if taken == total else discord.Color.blurple()

    embed = discord.Embed(
        title=f"🎁 {box['name']} — {box['series']}",
        description=f"**{taken}/{total}** varianti prenotate",
        color=color,
        timestamp=datetime.fromisoformat(box["created_at"]),
    )
    embed.set_footer(text=f"ID box: {box_id}")

    lines = []
    for variant, info in box["variants"].items():
        if info["reserved_by"]:
            lines.append(f"✅ ~~{variant}~~ — *prenotata*")
        else:
            lines.append(f"🎁 **{variant}** — libera")
    embed.add_field(name="Varianti", value="\n".join(lines), inline=False)
    return embed


def build_summary(box: dict) -> str:
    lines = ["**Riepilogo prenotazioni:**"]
    for variant, info in box["variants"].items():
        uid = info["reserved_by"]
        lines.append(f"• **{variant}** → <@{uid}>")
    return "\n".join(lines)


# ── Cog comandi ──────────────────────────────────────────────────
class BlindBoxCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # /newbox
    @app_commands.command(name="newbox", description="Crea una nuova full box da splittare")
    @app_commands.describe(
        nome="Nome della serie (es. Skullpanda)",
        serie="Nome della serie/edizione (es. Serie1)",
        varianti="Varianti separate da virgola (es. Rosso,Blu,Verde,Giallo,Nero,Bianco)",
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    async def newbox(self, interaction: discord.Interaction, nome: str, serie: str, varianti: str):
        variant_list = [v.strip() for v in varianti.split(",") if v.strip()]
        if len(variant_list) not in [6, 8, 12]:
            await interaction.response.send_message(
                f"⚠️ Le varianti devono essere 6, 8 o 12. Hai inserito {len(variant_list)}.", ephemeral=True
            )
            return

        data = load_data()
        box_id = f"{nome.lower().replace(' ', '_')}_{serie.lower().replace(' ', '_')}_{len(data)+1}"
        data[box_id] = {
            "name": nome,
            "series": serie,
            "created_by": str(interaction.user.id),
            "created_at": datetime.now().isoformat(),
            "message_id": None,
            "channel_id": str(interaction.channel_id),
            "variants": {v: {"reserved_by": None, "reserved_at": None} for v in variant_list},
        }
        save_data(data)

        embed = build_embed(data[box_id], box_id)
        view = BoxView(box_id)
        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()

        data[box_id]["message_id"] = str(msg.id)
        save_data(data)

    # /listbox
    @app_commands.command(name="listbox", description="Mostra tutte le box attive")
    async def listbox(self, interaction: discord.Interaction):
        data = load_data()
        if not data:
            await interaction.response.send_message("ℹ️ Nessuna box attiva al momento.", ephemeral=True)
            return

        lines = []
        for box_id, box in data.items():
            total = len(box["variants"])
            taken = sum(1 for v in box["variants"].values() if v["reserved_by"] is not None)
            status = "✅ Completa" if taken == total else f"⏳ {taken}/{total}"
            lines.append(f"• **{box['name']} {box['series']}** — {status} | ID: `{box_id}`")

        embed = discord.Embed(title="📦 Box attive", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # /boxinfo
    @app_commands.command(name="boxinfo", description="Dettagli e prenotazioni di una box")
    @app_commands.describe(box_id="ID della box (usa /listbox per trovarlo)")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def boxinfo(self, interaction: discord.Interaction, box_id: str):
        data = load_data()
        box = data.get(box_id)
        if not box:
            await interaction.response.send_message("❌ Box non trovata.", ephemeral=True)
            return

        lines = []
        for variant, info in box["variants"].items():
            if info["reserved_by"]:
                lines.append(f"✅ **{variant}** → <@{info['reserved_by']}> ({info['reserved_at'][:10]})")
            else:
                lines.append(f"🎁 **{variant}** → *libera*")

        embed = discord.Embed(
            title=f"📋 {box['name']} — {box['series']}",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # /deletebox
    @app_commands.command(name="deletebox", description="Elimina una box (solo admin)")
    @app_commands.describe(box_id="ID della box da eliminare")
    @app_commands.checks.has_permissions(administrator=True)
    async def deletebox(self, interaction: discord.Interaction, box_id: str):
        data = load_data()
        if box_id not in data:
            await interaction.response.send_message("❌ Box non trovata.", ephemeral=True)
            return
        del data[box_id]
        save_data(data)
        await interaction.response.send_message(f"🗑️ Box `{box_id}` eliminata.", ephemeral=True)

    # Gestione errori permessi
    @newbox.error
    @boxinfo.error
    @deletebox.error
    async def permission_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("🚫 Non hai i permessi per usare questo comando.", ephemeral=True)


# ── Avvio ────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("❌ DISCORD_TOKEN non trovato! Controlla il file .env")
    bot.run(TOKEN)
