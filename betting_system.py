import discord
from discord.ext import commands, tasks
import re
import asyncio
from datetime import datetime, timedelta

STEAM_ID_RE = re.compile(r'^\d{17}$')
BET_TIMEOUT = 420  # 7 minuti

# Admin del bot (ID Discord) — hanno accesso a tutti i comandi admin
BOT_ADMINS = [377081422558789633]


def is_bot_admin():
    async def predicate(ctx):
        if ctx.guild is None:
            return False  # DM non supportati per comandi admin
        if ctx.author.guild_permissions.administrator:
            return True
        if ctx.author.id in BOT_ADMINS:
            return True
        await ctx.send("❌ Solo gli admin del bot possono usare questo comando.", delete_after=10)
        return False
    return commands.check(predicate)


# =============================================================================
# MODAL SCOMMESSA
# =============================================================================
class BetModal(discord.ui.Modal, title='Piazza la tua scommessa'):
    amount_input = discord.ui.TextInput(
        label='Quantita di U', placeholder='Es: 50', min_length=1, max_length=6
    )

    def __init__(self, dito_id, team_num, api_ref):
        super().__init__()
        self.dito_id = dito_id
        self.team_num = team_num
        self.api_ref = api_ref

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = int(self.amount_input.value)
            if val <= 0:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "❌ Inserisci un numero valido maggiore di 0.", ephemeral=True
            )
        try:
            await self.api_ref.place_bet(self.dito_id, interaction.user.id, val, self.team_num)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        await interaction.response.send_message(
            f"✅ Hai scommesso **{val} U** sul **Team {self.team_num}**!", ephemeral=True
        )


# =============================================================================
# BETVIEW CON TIMER VISIBILE
# =============================================================================
class BetView(discord.ui.View):
    def __init__(self, dito_id, team1_names, team2_names, api_ref):
        super().__init__(timeout=BET_TIMEOUT)
        self.dito_id = dito_id
        self.t1 = [n.lower() for n in team1_names]
        self.t2 = [n.lower() for n in team2_names]
        self.api_ref = api_ref
        self._message = None
        self._deadline = datetime.now() + timedelta(seconds=BET_TIMEOUT)

    def set_message(self, message):
        self._message = message

    def _time_left(self):
        secs = int((self._deadline - datetime.now()).total_seconds())
        if secs < 0:
            return "⏰ CHIUSO"
        m, s = divmod(secs, 60)
        return f"⏳ {m}:{s:02d}"

    @discord.ui.button(label="Punta Team 1", style=discord.ButtonStyle.success, emoji="🔵", row=0)
    async def team1_bet(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BetModal(self.dito_id, 1, self.api_ref))

    @discord.ui.button(label="Punta Team 2", style=discord.ButtonStyle.danger, emoji="🔴", row=0)
    async def team2_bet(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BetModal(self.dito_id, 2, self.api_ref))

    @discord.ui.button(label="ALL IN Team 1", style=discord.ButtonStyle.success, emoji="🔥", row=1)
    async def allin_team1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._allin_bet(interaction, 1)

    @discord.ui.button(label="ALL IN Team 2", style=discord.ButtonStyle.danger, emoji="🔥", row=1)
    async def allin_team2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._allin_bet(interaction, 2)

    async def _allin_bet(self, interaction: discord.Interaction, team: int):
        balance = await self.api_ref.get_wallet(interaction.user.id)
        if balance <= 0:
            return await interaction.response.send_message("❌ Non hai abbastanza U!", ephemeral=True)
        try:
            await self.api_ref.place_bet(self.dito_id, interaction.user.id, balance, team)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        await interaction.response.send_message(
            f"🔥 **ALL IN!** {balance} U sul **Team {team}**!", ephemeral=True
        )

    def start_live_refresh(self):
        self._refresh_task = asyncio.create_task(self._live_refresh_loop())

    async def _live_refresh_loop(self):
        try:
            while True:
                await asyncio.sleep(15)
                if not self._message:
                    break
                bets = await self.api_ref.get_dito_bets(self.dito_id)
                t1_pool = sum(b['amount'] for b in bets if b['prediction'] == 1)
                t2_pool = sum(b['amount'] for b in bets if b['prediction'] == 2)
                total = t1_pool + t2_pool
                time_str = self._time_left()
                old = self._message.content or ""
                base = old.split("📊")[0] if "📊" in old else old.split("Usa i bottoni")[0] if "Usa i bottoni" in old else old
                if total > 0:
                    q1 = (total / t1_pool) * 0.95 if t1_pool > 0 else 99
                    q2 = (total / t2_pool) * 0.95 if t2_pool > 0 else 99
                    new_content = f"{base.strip()}\n📊 Live: 🔵 x{q1:.2f} | 🔴 x{q2:.2f} | 💰 {total} U | {time_str}\nUsa i bottoni qui sotto per puntare i tuoi **U**."
                else:
                    new_content = f"{base.strip()}\n{time_str} | 💰 Nessuna puntata\nUsa i bottoni qui sotto per puntare i tuoi **U**."
                try:
                    await self._message.edit(content=new_content, view=self)
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    async def on_timeout(self):
        if hasattr(self, '_refresh_task'):
            self._refresh_task.cancel()
        for child in self.children:
            child.disabled = True
        async with self.api_ref.pool.acquire() as conn:
            await conn.execute(
                "UPDATE diti_active SET status = 'closed' WHERE id = $1 AND status = 'betting'",
                self.dito_id
            )
        if self._message:
            try:
                await self._message.edit(
                    content="⏰ **Scommesse chiuse!** Tempo scaduto (7 min).\n"
                            "Un admin può risolvere con `!risolvi_dito`.",
                    view=self
                )
            except Exception:
                pass


# =============================================================================
# COMANDI
# =============================================================================
def setup_betting(bot, api):

    @bot.command(name='wallet')
    async def wallet_command(ctx):
        balance = await api.get_wallet(ctx.author.id)
        await ctx.send(f'💰 Il tuo saldo attuale è: **{balance} U**')

    @bot.command(name='daily')
    async def daily_command(ctx):
        async with api.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO user_wallets (discord_id, balance) VALUES ($1, 100) ON CONFLICT (discord_id) DO NOTHING",
                    ctx.author.id
                )
                row = await conn.fetchrow(
                    "SELECT last_daily FROM user_wallets WHERE discord_id = $1 FOR UPDATE",
                    ctx.author.id
                )
                now = datetime.now()
                if row['last_daily'] and now - row['last_daily'] < timedelta(days=1):
                    diff = timedelta(days=1) - (now - row['last_daily'])
                    h, s = divmod(int(diff.total_seconds()), 3600)
                    m, _ = divmod(s, 60)
                    return await ctx.send(f"⏳ Già riscattato. Torna tra {h}h {m}m.")
                await conn.execute(
                    "UPDATE user_wallets SET balance = balance + 50, last_daily = $1 WHERE discord_id = $2",
                    now, ctx.author.id
                )
        await ctx.send("🎁 Bonus giornaliero di **50 U**!")

    @bot.command(name='pay')
    async def pay_command(ctx, member: discord.Member, amount: int):
        if amount <= 0:
            return await ctx.send("❌ Importo positivo.")
        if member.id == ctx.author.id:
            return await ctx.send("❌ Non puoi inviare soldi a te stesso!")
        try:
            await api.transfer_balance(ctx.author.id, member.id, amount)
        except ValueError as e:
            return await ctx.send(f"❌ {e}")
        await ctx.send(f"✅ {amount} U inviati a {member.mention}.")

    @pay_command.error
    async def pay_error(ctx, error):
        if isinstance(error, (commands.BadArgument, commands.MemberNotFound)):
            await ctx.send("❌ **Uso:** `!pay @utente importo` (es. `!pay @Thomy 50`)")
        else:
            raise error

    @bot.command(name='banca')
    async def banca_command(ctx):
        top = await api.get_top_wallets(11)  # prendi 11 per filtrarne via il banco
        if not top:
            return await ctx.send("Nessun wallet registrato.")
        msg = "🏦 **Classifica Banca (Top 10):**\n"
        pos = 1
        for row in top:
            if row['discord_id'] == 0:  # salta il banco
                continue
            if pos > 10:
                break
            user = bot.get_user(row['discord_id'])
            name = user.name if user else f"User {row['discord_id']}"
            msg += f"{pos}. {name} - **{row['balance']} U**\n"
            pos += 1
        await ctx.send(msg)

    @bot.command(name='diti_attivi')
    async def diti_attivi_command(ctx):
        diti = await api.get_active_diti()
        if not diti:
            return await ctx.send("Nessun dito attivo al momento.")
        msg = "**🎲 Diti con scommesse aperte:**\n"
        for d in diti:
            msg += f"- ID **{d['id']}**: {d['lobby_name']} | ELO T1: {d['team1_avg_elo']} vs T2: {d['team2_avg_elo']}\n"
        await ctx.send(msg)

    @bot.command(name='scommesse')
    async def scommesse_command(ctx, dito_id: int):
        bets = await api.get_dito_bets(dito_id)
        if not bets:
            return await ctx.send(f"Nessuna scommessa per il dito #{dito_id}.")
        msg = f"📊 **Scommesse Dito #{dito_id}**\n"
        t1_bets = [b for b in bets if b['prediction'] == 1]
        t2_bets = [b for b in bets if b['prediction'] == 2]
        msg += "\n**🔵 Team 1:**\n"
        for b in t1_bets:
            user = bot.get_user(b['discord_id'])
            name = user.name if user else f"User {b['discord_id']}"
            msg += f"- {name}: {b['amount']} U\n"
        if not t1_bets:
            msg += "- *Nessuna*\n"
        msg += "\n**🔴 Team 2:**\n"
        for b in t2_bets:
            user = bot.get_user(b['discord_id'])
            name = user.name if user else f"User {b['discord_id']}"
            msg += f"- {name}: {b['amount']} U\n"
        if not t2_bets:
            msg += "- *Nessuna*\n"
        t1_total = sum(b['amount'] for b in t1_bets)
        t2_total = sum(b['amount'] for b in t2_bets)
        msg += f"\n💰 Totale: {t1_total + t2_total} U (T1: {t1_total} | T2: {t2_total})"
        await ctx.send(msg)

    @bot.command(name='mie_scommesse')
    async def mie_scommesse_command(ctx):
        async with api.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT b.amount, b.prediction, a.lobby_name FROM diti_bets b "
                "JOIN diti_active a ON b.dito_id = a.id "
                "WHERE b.discord_id = $1 AND a.status = 'betting'",
                ctx.author.id
            )
            if not rows:
                return await ctx.send("Non hai scommesse attive.")
            msg = "📑 **Le tue scommesse attive:**\n"
            for r in rows:
                msg += f"- {r['amount']} U su **Team {r['prediction']}** in `{r['lobby_name']}`\n"
            await ctx.send(msg)

    @bot.command(name='guida')
    async def guida_command(ctx):
        embed = discord.Embed(title="📖 TG Dito — Guida Completa", description="🎲 **CASINÒ** — Ogni gioco ha il suo comando!", color=discord.Color.gold())
        embed.add_field(name="💰 Economia", value="• `!daily` — Bonus **50 U** ogni 24h\n• `!wallet` — Saldo attuale\n• `!banca` — Classifica TOP 10 più ricchi\n• `!pay @utente 100` — Invia U a qualcuno\n• `!profilo` — Le tue statistiche scommesse", inline=False)
        embed.add_field(name="🎲 Dadi `!dadi <n>`", value="Lancia 2 dadi:\n🎯 7 o 11 → **x2**\n🎯 2 o 12 → **x3**\n❌ Altro → perdi (al banco)\nMax puntata: 500 U", inline=False)
        embed.add_field(name="🎡 Roulette `!roulette <n>`", value="Roulette europea (0-36):\n🔴 RED / ⚫ BLACK → **x2**\n🟢 ZERO → **x36**\n1-18 / 19-36 → **x2**\nEVEN / ODD → **x2**\nMax puntata: 1000 U | Casa: 2.7%", inline=False)
        embed.add_field(name="🎰 Slot `!slot <n>`", value="3 rulli con emoji:\n💎💎💎 → **x20 JACKPOT**\n7️⃣7️⃣7️⃣ → **x10**\n⭐⭐⭐ → **x5**\n🍒🍒🍒 tris → **x3**\n2 uguali → **x1.5**\nMax puntata: 500 U", inline=False)
        embed.add_field(name="🟢 Plinko `!plinko <n>`", value="La pallina cade a zigzag:\n8 rimbalzi casuali tra i pioli\nAtterra su: **x0  x0.5  x1  x1.5  x3  x5**\nMax puntata: 300 U", inline=False)
        embed.add_field(name="🃏 Blackjack `!blackjack <n>`", value="Contro il banco:\n➕ HIT = pesca una carta\n✋ STAND = tieni\n🔥 DOUBLE = raddoppia + 1 carta\n🃏 Blackjack (21 con 2 carte) = **x2.5**\nVittoria = **x2** | Push = rimborso\nMax puntata: 1000 U", inline=False)
        embed.add_field(name="🎲 Scommesse Dito", value="• `!bilancia dito <nome>` — Scommesse 1v1\n• `!bilancia <lobby>` — Team con quote live\n• `!scommesse <id>` — Vedi puntate\n• `!diti_attivi` — Diti aperti\n• Timer 7 min | Tassa 5% | 🤖 Auto-risoluzione!", inline=False)
        embed.add_field(name="🎟️ Lotteria & Torneo", value="• `!lotto <n>` — Biglietto lotteria settimanale\n• `!torneo lista` — Match torneo attivi\n• `!tbet <id> <1|2> <n>` — Scommetti sul torneo", inline=False)
        embed.add_field(name="📊 Statistiche", value="• `!stats <nome>` — ELO, WR, partite\n• `!classifica` — Top 20 ELO attivi\n• `!scontri A vs B` — Testa a testa\n• `!streak <nome>` — Serie vittorie/sconfitte\n• `!civ <nome>` — Civiltà preferite", inline=False)
        embed.set_footer(text="🏦 Il banco (ID 0) è la casa. !banco per vedere gli incassi (admin)")
        await ctx.send(embed=embed)

    # =========================================================================
    # ADMIN
    # =========================================================================
    @bot.command(name='risolvi_dito')
    @is_bot_admin()
    async def risolvi_dito_command(ctx, dito_id: int, winning_team: int):
        if winning_team not in [1, 2]:
            return await ctx.send("❌ Scegli 1 o 2.")

        # === CONFERMA DI SICUREZZA ===
        from bot import dito_profile_ids
        auto_tracked = "🤖 Auto-risoluzione è ATTIVA su questo dito" if dito_id in dito_profile_ids else "⚠️ Dito non tracciato automaticamente"

        await ctx.send(
            f"⚠️ **Conferma risoluzione Dito #{dito_id}**\n"
            f"🏆 Team vincente: **{winning_team}**\n"
            f"{auto_tracked}\n\n"
            f"Scrivi `confermo` entro 30 secondi per procedere."
        )

        def check(m):
            return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id

        try:
            response = await bot.wait_for('message', check=check, timeout=30)
            if response.content.lower() != 'confermo':
                return await ctx.send("❌ Risoluzione annullata.")
        except asyncio.TimeoutError:
            return await ctx.send("⏰ Tempo scaduto. Risoluzione annullata.")

        success, result = await api.resolve_dito(dito_id, winning_team)
        if not success:
            return await ctx.send(f"❌ {result}")
        payouts = result.get('payouts', [])
        stats = result.get('stats', {})
        if not payouts:
            return await ctx.send(f"✅ Dito #{dito_id} risolto. Nessuna scommessa.")
        msg = (f"🏆 **Dito #{dito_id} — Vince Team {winning_team}!**\n"
               f"💰 Pool: {stats.get('total_pool', 0)} U | Tassa 5%: {stats.get('tax', 0)} U\n"
               f"📈 x{stats.get('multiplier', 1):.2f}\n\n**Pagamenti:**\n")
        for p in payouts:
            user = bot.get_user(p['discord_id'])
            name = user.name if user else f"User {p['discord_id']}"
            if p.get('refund'):
                msg += f"- {name}: Rimborso {p['winnings']} U\n"
            else:
                msg += f"- {name}: +{p['winnings']} U (puntata {p['original_bet']})\n"
        await ctx.send(msg)

    @bot.command(name='stop_scommesse')
    @is_bot_admin()
    async def stop_scommesse_command(ctx, dito_id: int):
        """Chiude manualmente le scommesse per un dito (senza risolverlo)."""
        async with api.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE diti_active SET status = 'closed' WHERE id = $1 AND status = 'betting'",
                dito_id
            )
        if result == "UPDATE 0":
            await ctx.send(f"❌ Dito #{dito_id} non trovato o già chiuso.")
        else:
            await ctx.send(f"🔒 Scommesse chiuse per il Dito #{dito_id}. Usa `!risolvi_dito {dito_id} <1|2>` per risolvere.")

    @bot.command(name='annulla_scommessa')
    @is_bot_admin()
    async def annulla_scommessa_command(ctx, dito_id: int, member: discord.Member):
        """Rimborsa la scommessa di un utente specifico sul dito e la cancella."""
        async with api.pool.acquire() as conn:
            async with conn.transaction():
                bet = await conn.fetchrow(
                    "SELECT id, amount, prediction FROM diti_bets WHERE dito_id = $1 AND discord_id = $2",
                    dito_id, member.id
                )
                if not bet:
                    return await ctx.send(f"❌ {member.mention} non ha scommesse sul Dito #{dito_id}.")
                await conn.execute("DELETE FROM diti_bets WHERE id = $1", bet['id'])
                await conn.execute(
                    "UPDATE user_wallets SET balance = balance + $1 WHERE discord_id = $2",
                    bet['amount'], member.id
                )
        await ctx.send(f"♻️ Rimborsati **{bet['amount']} U** a {member.mention} e cancellata scommessa sul Dito #{dito_id}.")

    @bot.command(name='annulla_dito')
    @is_bot_admin()
    async def annulla_dito_command(ctx, dito_id: int):
        """Annulla TUTTE le scommesse di un dito e rimborsa tutti."""
        async with api.pool.acquire() as conn:
            async with conn.transaction():
                dito = await conn.fetchrow(
                    "SELECT id, status FROM diti_active WHERE id = $1", dito_id
                )
                if not dito:
                    return await ctx.send(f"❌ Dito #{dito_id} non trovato.")
                if dito['status'] == 'resolved':
                    return await ctx.send(f"❌ Dito #{dito_id} già risolto, impossibile annullare.")

                bets = await conn.fetch(
                    "SELECT discord_id, amount FROM diti_bets WHERE dito_id = $1", dito_id
                )
                if not bets:
                    await conn.execute("UPDATE diti_active SET status = 'cancelled' WHERE id = $1", dito_id)
                    return await ctx.send(f"✅ Dito #{dito_id} cancellato. Nessuna scommessa da rimborsare.")

                total = 0
                for b in bets:
                    await conn.execute(
                        "UPDATE user_wallets SET balance = balance + $1 WHERE discord_id = $2",
                        b['amount'], b['discord_id']
                    )
                    total += b['amount']

                await conn.execute("DELETE FROM diti_bets WHERE dito_id = $1", dito_id)
                await conn.execute("UPDATE diti_active SET status = 'cancelled' WHERE id = $1", dito_id)

        # Pulisci il tracking auto-resolve
        try:
            from bot import dito_profile_ids
            if dito_id in dito_profile_ids:
                del dito_profile_ids[dito_id]
        except Exception:
            pass
        await ctx.send(f"♻️ **Dito #{dito_id} annullato!** Rimborsati {total} U a {len(bets)} giocatori.")

    @bot.command(name='give_u')
    @is_bot_admin()
    async def give_u_command(ctx, member: discord.Member, amount: int):
        if amount <= 0:
            return await ctx.send("❌ Importo positivo.")
        await api.get_wallet(member.id)
        await api.update_balance(member.id, amount)
        await ctx.send(f"🏦 +{amount} U a {member.mention}.")

    @bot.command(name='remove_u')
    @is_bot_admin()
    async def remove_u_command(ctx, member: discord.Member, amount: int):
        if amount <= 0:
            return await ctx.send("❌ Importo positivo.")
        balance = await api.get_wallet(member.id)
        if balance < amount:
            return await ctx.send(f"❌ {member.mention} ha solo {balance} U.")
        await api.update_balance(member.id, -amount)
        await ctx.send(f"🏦 -{amount} U da {member.mention}.")

    @bot.command(name='premia')
    @is_bot_admin()
    async def premia_command(ctx, member: discord.Member, amount: int):
        """Preleva U dal banco e le dá a un utente. Uso: !premia @utente 100"""
        if amount <= 0:
            return await ctx.send("❌ Importo positivo.")
        try:
            await api.transfer_balance(0, member.id, amount)
        except ValueError:
            house = await api.get_wallet(0)
            return await ctx.send(f"❌ Il banco ha solo {house} U.")
        await ctx.send(f"🏦 **{amount} U** dal banco a {member.mention}!")

    @bot.command(name='testbet')
    async def testbet_command(ctx):
        t1 = [{'name': 'Player1', 'steam_id': '1', 'elo': 1000}, {'name': 'Player2', 'steam_id': '2', 'elo': 1100}]
        t2 = [{'name': 'Player3', 'steam_id': '3', 'elo': 1050}, {'name': 'Player4', 'steam_id': '4', 'elo': 950}]
        dito_id = await api.create_active_dito(ctx.channel.id, "TEST LOBBY", t1, t2)
        view = BetView(dito_id, [p['name'] for p in t1], [p['name'] for p in t2], api)
        sent = await ctx.send(
            f"🧪 **TEST (ID: {dito_id})** | ⏳ 15:00\n"
            f"Scommesse aperte! Admin: `!risolvi_dito {dito_id} 1` o `2`.",
            view=view
        )
        view.set_message(sent)

    @bot.command(name='link')
    async def link_command(ctx, steam_id: str):
        if not STEAM_ID_RE.match(steam_id):
            return await ctx.send("❌ Steam ID: 17 cifre (es. `76561198000000000`).")
        async with api.pool.acquire() as conn:
            await conn.execute("UPDATE user_wallets SET steam_id = $1 WHERE discord_id = $2", steam_id, ctx.author.id)
        await ctx.send(f"✅ Steam `{steam_id}` collegato.")
