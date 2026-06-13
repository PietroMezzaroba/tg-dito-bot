"""
features.py — Nuovi comandi per il bot TG Dito.
!profilo, !dadi, !lotto, !torneo, !tbet, !indovina
"""

import discord
from discord.ext import commands, tasks
import random
import asyncio
import traceback
from datetime import datetime, timedelta, date

# Admin del bot (ID Discord)
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
# TABELLE
# =============================================================================

async def ensure_tables(pool):
    """Crea le tabelle mancanti per le nuove feature (idempotente)."""
    queries = [
        """
        CREATE TABLE IF NOT EXISTS lottery_entries (
            id SERIAL PRIMARY KEY,
            discord_id BIGINT NOT NULL,
            amount INTEGER NOT NULL CHECK (amount > 0),
            week_start DATE NOT NULL DEFAULT date_trunc('week', CURRENT_DATE),
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tournament_matches (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            player1 TEXT NOT NULL,
            player2 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'betting' CHECK (status IN ('betting', 'resolved')),
            winner INTEGER CHECK (winner IN (0, 1, 2)),
            channel_id BIGINT,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tournament_bets (
            id SERIAL PRIMARY KEY,
            match_id INTEGER NOT NULL,
            discord_id BIGINT NOT NULL,
            amount INTEGER NOT NULL,
            prediction INTEGER NOT NULL CHECK (prediction IN (1, 2)),
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS balance_guesses (
            id SERIAL PRIMARY KEY,
            dito_id INTEGER NOT NULL,
            discord_id BIGINT NOT NULL,
            guessed_diff INTEGER NOT NULL CHECK (guessed_diff >= 0),
            actual_diff INTEGER,
            reward_paid BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS house_earnings (
            id SERIAL PRIMARY KEY,
            source TEXT NOT NULL,
            source_id INTEGER,
            amount INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """,
    ]
    async with pool.acquire() as conn:
        for q in queries:
            await conn.execute(q)


# =============================================================================
# SISTEMA TITOLI
# =============================================================================

def _safe(v, default):
    """Restituisce v se non è None, altrimenti default."""
    return default if v is None else v


TITLES = [
    ("👑", "High Roller", lambda bal, prof: _safe(bal, 0) > 500),
    ("🐋", "Whale", lambda bal, prof: _safe(bal, 0) > 1000),
    ("🔥", "Lucky", lambda bal, prof: _safe(prof.get("current_streak_win"), 0) >= 3),
    ("🏦", "Banker", lambda bal, prof: _safe(prof.get("rank"), 999) <= 3),
    ("💀", "Degenerate", lambda bal, prof: _safe(prof.get("total_bets"), 0) > 50),
    ("🎯", "Sniper", lambda bal, prof: _safe(prof.get("winrate"), 0) >= 0.60 and _safe(prof.get("total_bets"), 0) >= 5),
    ("❄️", "Cold Streak", lambda bal, prof: _safe(prof.get("current_streak_loss"), 0) >= 5),
    ("👻", "Fantasma", lambda bal, prof: _safe(prof.get("days_since_last_bet"), 999) > 7),
    ("🃏", "Joker", lambda bal, prof: _safe(prof.get("all_in_count"), 0) >= 3),
]


def calculate_titles(balance, profile):
    earned = []
    for emoji, name, condition in TITLES:
        if condition(balance, profile):
            earned.append(f"{emoji} {name}")
    return earned if earned else ["🎫 Novizio"]


# =============================================================================
# HELPERS
# =============================================================================

def dice_emoji(value):
    return ["", "⚀", "⚁", "⚂", "⚃", "⚄", "⚅"][value]


def lottery_week_start():
    today = date.today()
    return today - timedelta(days=today.weekday())


def format_ordinal(n):
    """1 -> 1°, 2 -> 2°, ..."""
    return f"{n}°"


# =============================================================================
# SETUP
# =============================================================================

def setup_features(bot, api):

    # =========================================================================
    # !profilo
    # =========================================================================
    @bot.command(name='profilo')
    async def profilo_command(ctx, member: discord.Member = None):
        """Statistiche scommesse tue o di un altro utente."""
        target = member or ctx.author
        balance = await api.get_wallet(target.id)

        async with api.pool.acquire() as conn:
            stats_row = await conn.fetchrow(
                "SELECT COUNT(*) AS total_bets, COALESCE(SUM(amount), 0) AS total_wagered, "
                "COALESCE(MAX(amount), 0) AS max_bet FROM diti_bets WHERE discord_id = $1",
                target.id
            )
            wins_losses = await conn.fetchrow(
                "SELECT COALESCE(SUM(CASE WHEN b.prediction = a.winning_team THEN 1 ELSE 0 END), 0) AS wins, "
                "COALESCE(SUM(CASE WHEN b.prediction != a.winning_team THEN 1 ELSE 0 END), 0) AS losses "
                "FROM diti_bets b JOIN diti_active a ON b.dito_id = a.id "
                "WHERE b.discord_id = $1 AND a.status = 'resolved'",
                target.id
            )
            last_bet = await conn.fetchrow(
                "SELECT amount, created_at FROM diti_bets WHERE discord_id = $1 ORDER BY id DESC LIMIT 1",
                target.id
            )
            rank_row = await conn.fetchrow(
                "SELECT rank FROM (SELECT discord_id, ROW_NUMBER() OVER (ORDER BY balance DESC) AS rank "
                "FROM user_wallets) sub WHERE discord_id = $1",
                target.id
            )
            resolved_bets = await conn.fetch(
                "SELECT b.prediction, a.winning_team FROM diti_bets b "
                "JOIN diti_active a ON b.dito_id = a.id "
                "WHERE b.discord_id = $1 AND a.status = 'resolved' ORDER BY b.id DESC LIMIT 20",
                target.id
            )
            all_in_row = await conn.fetchrow(
                "SELECT COUNT(*) as cnt FROM diti_bets WHERE discord_id = $1 AND amount >= 200",
                target.id
            )

        total = stats_row['total_bets'] or 0
        wagered = stats_row['total_wagered'] or 0
        max_bet = stats_row['max_bet'] or 0
        wins = wins_losses['wins'] or 0
        losses = wins_losses['losses'] or 0
        winrate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
        rank = rank_row['rank'] if rank_row else None
        all_ins = all_in_row['cnt'] or 0

        # Streak
        current_streak_win, current_streak_loss = 0, 0
        for r in resolved_bets:
            if r['prediction'] == r['winning_team']:
                if current_streak_loss == 0:
                    current_streak_win += 1
                else:
                    break
            else:
                if current_streak_win == 0:
                    current_streak_loss += 1
                else:
                    break

        days_since = None
        if last_bet and last_bet['created_at']:
            days_since = (datetime.now() - last_bet['created_at']).days

        profile = {
            "total_bets": total, "total_wagered": wagered, "max_bet": max_bet,
            "wins": wins, "losses": losses, "winrate": winrate,
            "current_streak_win": current_streak_win, "current_streak_loss": current_streak_loss,
            "rank": rank, "days_since_last_bet": days_since, "all_in_count": all_ins,
        }

        titles = calculate_titles(balance, profile)

        embed = discord.Embed(
            title=f"📊 Profilo Scommesse — {target.display_name}",
            color=discord.Color.gold()
        )
        embed.add_field(name="💰 Saldo", value=f"**{balance} U**", inline=True)
        embed.add_field(name="🏆 Winrate", value=f"**{winrate:.0f}%** ({wins}V / {losses}S)", inline=True)
        embed.add_field(name="📊 Partite", value=f"**{total}** scommesse", inline=True)
        embed.add_field(name="💸 Totale Puntato", value=f"**{wagered} U** (max: {max_bet} U)", inline=True)
        embed.add_field(name="🏦 Rank", value=f"**{format_ordinal(rank)}**" if rank else "N/D", inline=True)

        streak_text = ""
        if current_streak_win > 0:
            streak_text = f"🔥 **{current_streak_win} vittorie** consecutive"
        elif current_streak_loss > 0:
            streak_text = f"❄️ **{current_streak_loss} sconfitte** consecutive"
        else:
            streak_text = "—"
        embed.add_field(name="📈 Streak", value=streak_text, inline=True)
        embed.add_field(name="🎖️ Titoli", value=" ".join(titles), inline=False)

        await ctx.send(embed=embed)

    # =========================================================================
    # !dadi
    # =========================================================================
    @bot.command(name='dadi')
    async def dadi_command(ctx, amount: int):
        """Lancia due dadi. 7/11 = x2, 2/12 = x3. Vince paga il banco, perde incassa il banco."""
        if amount <= 0:
            return await ctx.send("❌ Inserisci un importo positivo (es. `!dadi 20`).")
        
        # Controllo dinamico: il banco deve poter pagare la vincita massima (x3)
        house = await api.get_wallet(0)
        max_bet = min(500, house // 3)
        if max_bet <= 0:
            return await ctx.send(f"❌ Il banco è in rosso! Torna più tardi.")
        if amount > max_bet:
            return await ctx.send(f"❌ Puntata massima: **{max_bet} U** (banco: {house} U).")

        # Controlla saldo giocatore
        player_balance = await api.get_wallet(ctx.author.id)
        if player_balance < amount:
            return await ctx.send(f"❌ Saldo insufficiente! Hai **{player_balance} U**.")

        # Lancia i dadi PRIMA di scalare
        d1, d2 = random.randint(1, 6), random.randint(1, 6)
        total = d1 + d2

        multiplier = 0
        if total in (7, 11):
            multiplier = 2
        elif total in (2, 12):
            multiplier = 3

        winnings = int(amount * multiplier)

        if winnings > 0:
            # Controlla se il banco può pagare PRIMA di scalare
            house_balance = await api.get_wallet(0)
            if house_balance < winnings:
                # Banco non può pagare: non scalare, annulla tutto
                embed = discord.Embed(
                    title=f"🎲 {dice_emoji(d1)} + {dice_emoji(d2)} = **{total}** (x{multiplier})",
                    description=f"⚠️ Banco ha solo **{house_balance} U**, non può pagare **{winnings} U**.\n"
                                f"💡 La puntata non è stata scalata. Il banco ha bisogno di più perdite!",
                    color=discord.Color.orange()
                )
                return await ctx.send(embed=embed)

            # Banco può pagare: scala giocatore e paga
            await api.update_balance(ctx.author.id, -amount)
            await api.update_balance(0, -winnings)
            await api.update_balance(ctx.author.id, winnings)
            embed = discord.Embed(
                title=f"🎲 {dice_emoji(d1)} + {dice_emoji(d2)} = **{total}**",
                description=f"✅ Hai vinto **{winnings} U**! (x{multiplier})\n"
                            f"💰 Nuovo saldo: **{await api.get_wallet(ctx.author.id)} U**",
                color=discord.Color.green()
            )
        else:
            # Perdita: scala giocatore e versa al banco
            await api.update_balance(ctx.author.id, -amount)
            await api.update_balance(0, amount)
            embed = discord.Embed(
                title=f"🎲 {dice_emoji(d1)} + {dice_emoji(d2)} = **{total}**",
                description=f"❌ Hai perso **{amount} U** (al banco 🏦).\n"
                            f"💰 Nuovo saldo: **{await api.get_wallet(ctx.author.id)} U**",
                color=discord.Color.red()
            )
        await ctx.send(embed=embed)

    # =========================================================================
    # !roulette — ROULETTE EUROPEA 🎡
    # =========================================================================
    
    # Costanti roulette
    ROULETTE_RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    ROULETTE_BLACK = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}
    ROULETTE_PAYOUTS = {'red': 2, 'black': 2, 'zero': 36, 'low': 2, 'high': 2, 'even': 2, 'odd': 2}

    def roulette_number_emoji(n):
        if n == 0:
            return "🟢"
        return "🔴" if n in ROULETTE_RED else "⚫"

    class RouletteView(discord.ui.View):
        def __init__(self, amount, api_ref, author_id):
            super().__init__(timeout=60)
            self.amount = amount
            self.api_ref = api_ref
            self.author_id = author_id
            self._message = None
            self._resolved = False

        def set_message(self, msg):
            self._message = msg

        async def _spin_and_resolve(self, interaction, bet_type, bet_label):
            if self._resolved:
                return await interaction.response.send_message("❌ Questa roulette è già stata giocata.", ephemeral=True)
            if interaction.user.id != self.author_id:
                return await interaction.response.send_message("❌ Solo chi ha lanciato la roulette può puntare.", ephemeral=True)

            # Ricontrolla saldo
            balance = await self.api_ref.get_wallet(self.author_id)
            if balance < self.amount:
                return await interaction.response.send_message(
                    f"❌ Saldo insufficiente! Hai **{balance} U**.", ephemeral=True
                )

            self._resolved = True
            # Disabilita tutti i bottoni
            for child in self.children:
                child.disabled = True

            # Scala la puntata
            await self.api_ref.update_balance(self.author_id, -self.amount)

            # Gira la ruota!
            result = random.randint(0, 36)

            # Determina esito
            won = False
            if bet_type == 'zero' and result == 0:
                won = True
            elif bet_type == 'red' and result in ROULETTE_RED:
                won = True
            elif bet_type == 'black' and result in ROULETTE_BLACK:
                won = True
            elif bet_type == 'low' and 1 <= result <= 18:
                won = True
            elif bet_type == 'high' and 19 <= result <= 36:
                won = True
            elif bet_type == 'even' and result != 0 and result % 2 == 0:
                won = True
            elif bet_type == 'odd' and result % 2 == 1:
                won = True

            multiplier = ROULETTE_PAYOUTS[bet_type]
            winnings = self.amount * multiplier
            emoji = roulette_number_emoji(result)

            # === ANIMAZIONE SPIN ===
            spin_emojis = ["🎡","💫","✨"]
            spin_msg = await interaction.response.send_message(
                f"{spin_emojis[0]} **La pallina gira...** `{random.randint(0,36)}`", 
                ephemeral=False
            )
            spin_msg = await interaction.original_response()

            anim_numbers = [random.randint(0, 36) for _ in range(10)]
            anim_numbers.append(result)  # l'ultimo è quello vero
            for i, n in enumerate(anim_numbers):
                await asyncio.sleep(0.3)
                bar = "▰" * (i % 4 + 1) + "▱" * (3 - i % 4)
                try:
                    await spin_msg.edit(content=f"🎡 **La pallina gira...** {bar} `{n}`")
                except Exception:
                    pass

            # Ultimo frame
            await asyncio.sleep(0.3)
            await spin_msg.delete()

            # Costruisci embed risultato
            if won:
                # Controlla se il banco può pagare
                house_balance = await self.api_ref.get_wallet(0)
                if house_balance < winnings:
                    # Rimborsa il giocatore
                    await self.api_ref.update_balance(self.author_id, self.amount)
                    embed = discord.Embed(
                        title=f"🎡 Roulette — {emoji} **{result}**",
                        description=f"⚠️ Il banco ha solo **{house_balance} U**, non può pagare **{winnings} U**."
                                    f"\n♻️ Puntata rimborsata.",
                        color=discord.Color.orange()
                    )
                else:
                    # Paga!
                    await self.api_ref.update_balance(0, -winnings)
                    await self.api_ref.update_balance(self.author_id, winnings)
                    new_bal = await self.api_ref.get_wallet(self.author_id)
                    embed = discord.Embed(
                        title=f"🎡 Roulette — {emoji} **{result}**",
                        description=f"✅ **{bet_label}** — HAI VINTO **{winnings} U**! (x{multiplier})\n"
                                    f"💰 Nuovo saldo: **{new_bal} U**",
                        color=discord.Color.green()
                    )
            else:
                # Versa al banco
                await self.api_ref.update_balance(0, self.amount)
                new_bal = await self.api_ref.get_wallet(self.author_id)
                embed = discord.Embed(
                    title=f"🎡 Roulette — {emoji} **{result}**",
                    description=f"❌ **{bet_label}** — Hai perso **{self.amount} U** (al banco 🏦).\n"
                                f"💰 Nuovo saldo: **{new_bal} U**",
                    color=discord.Color.red()
                )

            # Aggiorna il messaggio originale con i bottoni disabilitati
            if self._message:
                try:
                    await self._message.edit(view=self)
                except Exception:
                    pass

            await interaction.channel.send(embed=embed)

        @discord.ui.button(label="🔴 RED x2", style=discord.ButtonStyle.danger, row=0)
        async def btn_red(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._spin_and_resolve(interaction, 'red', '🔴 RED')

        @discord.ui.button(label="⚫ BLACK x2", style=discord.ButtonStyle.secondary, row=0)
        async def btn_black(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._spin_and_resolve(interaction, 'black', '⚫ BLACK')

        @discord.ui.button(label="🟢 ZERO x36", style=discord.ButtonStyle.success, row=0)
        async def btn_zero(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._spin_and_resolve(interaction, 'zero', '🟢 ZERO')

        @discord.ui.button(label="1-18 x2", style=discord.ButtonStyle.primary, row=1)
        async def btn_low(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._spin_and_resolve(interaction, 'low', '1-18 (Basso)')

        @discord.ui.button(label="19-36 x2", style=discord.ButtonStyle.primary, row=1)
        async def btn_high(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._spin_and_resolve(interaction, 'high', '19-36 (Alto)')

        @discord.ui.button(label="EVEN x2", style=discord.ButtonStyle.primary, row=2)
        async def btn_even(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._spin_and_resolve(interaction, 'even', 'EVEN (Pari)')

        @discord.ui.button(label="ODD x2", style=discord.ButtonStyle.primary, row=2)
        async def btn_odd(self, interaction: discord.Interaction, button: discord.ui.Button):
            await self._spin_and_resolve(interaction, 'odd', 'ODD (Dispari)')

        async def on_timeout(self):
            for child in self.children:
                child.disabled = True
            if self._message:
                try:
                    await self._message.edit(content="⏰ **Roulette scaduta** — usa `!roulette <importo>` per giocare di nuovo!", view=self)
                except Exception:
                    pass

    @bot.command(name='roulette')
    async def roulette_command(ctx, amount: int):
        """🎡 Roulette Europea! Puntata fissa, scegli il tipo di puntata coi bottoni."""
        if amount <= 0:
            return await ctx.send("❌ Inserisci un importo positivo (es. `!roulette 50`).")
        house = await api.get_wallet(0)
        max_bet = min(1000, house // 36)
        if max_bet <= 0:
            return await ctx.send(f"❌ Il banco è in rosso! Torna più tardi.")
        if amount > max_bet:
            return await ctx.send(f"❌ Puntata massima: **{max_bet} U** (banco: {house} U).")

        balance = await api.get_wallet(ctx.author.id)
        if balance < amount:
            return await ctx.send(f"❌ Saldo insufficiente! Hai **{balance} U**.")

        # Tabella roulette in ASCII
        table = (
            "```\n"
            "     🟢 0\n"
            "🔴 1  2  3  4  5  6  7  8  9 10 11 12\n"
            "⚫ 13 14 15 16 17 18 19 20 21 22 23 24\n"
            "🔴 25 26 27 28 29 30 31 32 33 34 35 36\n"
            "```"
        )

        embed = discord.Embed(
            title="🎡 **ROULETTE TG DITO** 🎡",
            description=f"💶 Puntata: **{amount} U**\n{table}\n"
                        f"🎯 Scegli il tipo di puntata coi bottoni qui sotto!",
            color=discord.Color.dark_gold()
        )
        embed.set_footer(text=f"🎲 {ctx.author.display_name} | Margine casa: 2.7% | Premi un bottone per girare!")

        view = RouletteView(amount, api, ctx.author.id)
        msg = await ctx.send(embed=embed, view=view)
        view.set_message(msg)

    # =========================================================================

    # =========================================================================
    # !slot — SLOT MACHINE 🎰
    # =========================================================================

    SLOT_EMOJIS = ["🍒", "🍋", "🍊", "🍇", "🔔", "⭐", "7️⃣", "💎"]
    SLOT_PAYOUTS = {"💎": 20, "7️⃣": 10, "⭐": 5, "same3": 3, "pair": 1.5}

    @bot.command(name='slot')
    async def slot_command(ctx, amount: int):
        """🎰 Slot machine! 3 rulli, jackpot fino a x20."""
        if amount <= 0:
            return await ctx.send("❌ Importo positivo (es. `!slot 20`).")
        if amount > 500:
            return await ctx.send("❌ Puntata massima: 500 U.")

        balance = await api.get_wallet(ctx.author.id)
        if balance < amount:
            return await ctx.send(f"❌ Saldo insufficiente! Hai **{balance} U**.")
        await api.update_balance(ctx.author.id, -amount)

        # Animazione spin
        status_msg = await ctx.send("🎰 **GIRANO I RULLI...** 🎰")
        reel1, reel2, reel3 = [], [], []
        for i in range(8):
            r1, r2, r3 = random.choice(SLOT_EMOJIS), random.choice(SLOT_EMOJIS), random.choice(SLOT_EMOJIS)
            reel1.append(r1); reel2.append(r2); reel3.append(r3)
            await asyncio.sleep(0.25)
            try:
                frame = i + 1
                bar1 = "▰" * min(frame, 3) + "▱" * max(0, 3 - frame)
                await status_msg.edit(content=f"🎰 {bar1} GIRANO... 🎰\n┃ {r1} ┃ {r2} ┃ {r3} ┃")
            except Exception:
                pass

        # Risultato finale
        final = (reel1[-1], reel2[-1], reel3[-1])
        s1, s2, s3 = final

        # Determina vincita
        if s1 == s2 == s3:
            if s1 in ["💎", "7️⃣", "⭐"]:
                multiplier = SLOT_PAYOUTS[s1]
                win_type = f"JACKPOT! {s1}{s1}{s1}"
            else:
                multiplier = SLOT_PAYOUTS["same3"]
                win_type = f"TRIS! {s1}{s2}{s3}"
        elif s1 == s2 or s2 == s3 or s1 == s3:
            multiplier = SLOT_PAYOUTS["pair"]
            win_type = f"Coppia!"
        else:
            multiplier = 0
            win_type = ""

        winnings = int(amount * multiplier) if multiplier > 0 else 0

        await status_msg.delete()

        if winnings > 0:
            house = await api.get_wallet(0)
            if house < winnings:
                await api.update_balance(ctx.author.id, amount)
                embed = discord.Embed(
                    title=f"🎰 ┃ {s1} ┃ {s2} ┃ {s3} ┃ 🎰",
                    description=f"⚠️ {win_type} ma il banco non può pagare **{winnings} U**.\n♻️ Rimborsato.",
                    color=discord.Color.orange()
                )
            else:
                await api.update_balance(0, -winnings)
                await api.update_balance(ctx.author.id, winnings)
                new_bal = await api.get_wallet(ctx.author.id)
                embed = discord.Embed(
                    title=f"🎰 ┃ {s1} ┃ {s2} ┃ {s3} ┃ 🎰",
                    description=f"🎉 **{win_type}** — Vinti **{winnings} U**! (x{multiplier})\n💰 Nuovo saldo: **{new_bal} U**",
                    color=discord.Color.green()
                )
        else:
            await api.update_balance(0, amount)
            new_bal = await api.get_wallet(ctx.author.id)
            embed = discord.Embed(
                title=f"🎰 ┃ {s1} ┃ {s2} ┃ {s3} ┃ 🎰",
                description=f"😢 Nessuna combo — Persi **{amount} U**.\n💰 Nuovo saldo: **{new_bal} U**",
                color=discord.Color.red()
            )
        await ctx.send(embed=embed)

    # =========================================================================
    # !plinko — PLINKO 🟢
    # =========================================================================

    @bot.command(name='plinko')
    async def plinko_command(ctx, amount: int):
        """🟢 Plinko! La pallina cade tra i pioli e atterra su un moltiplicatore."""
        if amount <= 0:
            return await ctx.send("❌ Importo positivo (es. `!plinko 30`).")
        if amount > 300:
            return await ctx.send("❌ Puntata massima: 300 U.")

        balance = await api.get_wallet(ctx.author.id)
        if balance < amount:
            return await ctx.send(f"❌ Saldo insufficiente! Hai **{balance} U**.")
        await api.update_balance(ctx.author.id, -amount)

        # I moltiplicatori sul fondo
        bottom_multipliers = [0, 0.5, 1, 1.5, 3, 5, 3, 1.5, 1, 0.5, 0]

        # Simula il percorso: parte dal centro (col 5), 8 passi di zigzag
        position = 5
        steps = []
        for _ in range(8):
            direction = random.choice([-1, 1])
            position = max(0, min(10, position + direction))
            steps.append(("⬅️" if direction == -1 else "➡️", position))

        multiplier = bottom_multipliers[position]
        winnings = int(amount * multiplier)

        # Animazione
        status_msg = await ctx.send("🟢 **La pallina cade...**")
        path_display = ["▬"] * 11
        path_display[5] = "🟢"
        await asyncio.sleep(0.3)

        for step_idx, (arrow, pos) in enumerate(steps):
            old_pos = [i for i, x in enumerate(path_display) if x in ["🟢", "⚪"]][0] if any(x in ["🟢", "⚪"] for x in path_display) else 5
            path_display[old_pos] = "⬇️"
            path_display[pos] = "🟢"

            # Mostra solo pioli visibili (fino a depth corrente)
            depth = step_idx + 1
            row = "".join(path_display)
            try:
                await status_msg.edit(content=f"🟢 **La pallina cade...**\n```{row}\n{'  ' * pos}🟢```")
            except Exception:
                pass
            await asyncio.sleep(0.3)

        await status_msg.delete()

        # Mostra risultato con i moltiplicatori
        mult_row = " ".join([f"x{m}" for m in bottom_multipliers])
        pointer = "  " * position + "🏆"

        if winnings > 0:
            house = await api.get_wallet(0)
            if house < winnings:
                await api.update_balance(ctx.author.id, amount)
                embed = discord.Embed(
                    title="🟢 PLINKO 🟢",
                    description=f"⚠️ Banco non può pagare **{winnings} U**.\n♻️ Rimborsato.",
                    color=discord.Color.orange()
                )
            else:
                await api.update_balance(0, -winnings)
                await api.update_balance(ctx.author.id, winnings)
                new_bal = await api.get_wallet(ctx.author.id)
                embed = discord.Embed(
                    title="🟢 PLINKO 🟢",
                    description=f"🎉 Atterrato su **x{multiplier}**! Vinti **{winnings} U**\n```{mult_row}\n{pointer}```\n💰 Nuovo saldo: **{new_bal} U**",
                    color=discord.Color.green()
                )
        else:
            await api.update_balance(0, amount)
            new_bal = await api.get_wallet(ctx.author.id)
            embed = discord.Embed(
                title="🟢 PLINKO 🟢",
                description=f"💀 Atterrato su **x{multiplier}**! Persi **{amount} U**\n```{mult_row}\n{pointer}```\n💰 Nuovo saldo: **{new_bal} U**",
                color=discord.Color.red()
            )
        await ctx.send(embed=embed)

    # =========================================================================
    # !blackjack — BLACKJACK 🃏
    # =========================================================================

    CARD_SUITS = ["♠", "♥", "♦", "♣"]
    CARD_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

    def card_value(rank):
        if rank in ["J", "Q", "K"]:
            return 10
        if rank == "A":
            return 11  # gestito con soft/hard nel calcolo
        return int(rank)

    def hand_value(hand):
        """Calcola il valore migliore della mano (A = 1 o 11)."""
        total = 0
        aces = 0
        for rank, _ in hand:
            if rank in ["J", "Q", "K"]:
                total += 10
            elif rank == "A":
                aces += 1
                total += 11
            else:
                total += int(rank)
        while total > 21 and aces > 0:
            total -= 10
            aces -= 1
        return total

    def hand_str(hand):
        return " ".join([f"[{r}{s}]" for r, s in hand])

    class BlackjackView(discord.ui.View):
        def __init__(self, amount, api_ref, author_id, player_hand, dealer_hand, deck):
            super().__init__(timeout=120)
            self.amount = amount
            self.api_ref = api_ref
            self.author_id = author_id
            self.player_hand = player_hand
            self.dealer_hand = dealer_hand
            self.deck = deck
            self._message = None
            self._resolved = False

        def set_message(self, msg):
            self._message = msg

        async def _end_game(self, interaction, doubled=False):
            if self._resolved:
                return
            self._resolved = True
            for child in self.children:
                child.disabled = True

            # Turno del banco
            while hand_value(self.dealer_hand) < 17:
                self.dealer_hand.append(self.deck.pop())

            player_val = hand_value(self.player_hand)
            dealer_val = hand_value(self.dealer_hand)
            bet = self.amount * (2 if doubled else 1)

            # Determina esito
            if player_val > 21:
                result = "bust"
            elif dealer_val > 21:
                result = "win"
            elif player_val > dealer_val:
                result = "win"
            elif player_val == dealer_val:
                result = "push"
            else:
                result = "lose"

            # Blackjack!
            if len(self.player_hand) == 2 and player_val == 21 and not doubled:
                result = "blackjack"

            if result == "blackjack":
                winnings = int(bet * 2.5)  # 3:2 on blackjack
            elif result == "win":
                winnings = bet * 2
            elif result == "push":
                winnings = bet
            else:
                winnings = 0

            if winnings > bet:
                house = await self.api_ref.get_wallet(0)
                net = winnings - bet
                if house < net:
                    result = "house_broke"
                    winnings = bet

            # Pagamenti
            if winnings > 0 and result != "push":
                if winnings > bet:
                    await self.api_ref.update_balance(0, -(winnings - bet))
                    await self.api_ref.update_balance(self.author_id, winnings - bet)
                elif winnings == bet:
                    pass  # push, già scalato
            elif result == "bust" or result == "lose":
                await self.api_ref.update_balance(0, bet)

            new_bal = await self.api_ref.get_wallet(self.author_id)

            # Embed
            p_val = hand_value(self.player_hand)
            d_val = hand_value(self.dealer_hand)

            if result == "blackjack":
                title = "🃏 BLACKJACK! 🎉"
                desc = f"**{winnings} U** vinti! (x2.5)\n💰 Nuovo saldo: **{new_bal} U**"
                color = discord.Color.gold()
            elif result == "win":
                title = "🃏 HAI VINTO!"
                desc = f"Tuo: **{p_val}** vs Banco: **{d_val}**\n+**{winnings} U** (x2)\n💰 Nuovo saldo: **{new_bal} U**"
                color = discord.Color.green()
            elif result == "push":
                title = "🤝 PUSH (Pari)"
                desc = f"Tuo: **{p_val}** vs Banco: **{d_val}**\n♻️ Rimborso **{bet} U**\n💰 Saldo: **{new_bal} U**"
                color = discord.Color.blue()
            elif result == "house_broke":
                title = "⚠️ BANCO IN ROTTA"
                desc = f"Il banco non può pagare!\n♻️ Rimborso **{bet} U**\n💰 Saldo: **{new_bal} U**"
                color = discord.Color.orange()
            else:
                title = "💀 HAI PERSO"
                desc = f"Tuo: **{p_val}** vs Banco: **{d_val}**\n-{bet} U\n💰 Nuovo saldo: **{new_bal} U**"
                color = discord.Color.red()

            embed = discord.Embed(title=title, description=desc, color=color)
            embed.add_field(name="🃏 La tua mano", value=f"{hand_str(self.player_hand)} = **{p_val}**", inline=True)
            embed.add_field(name="🏦 Banco", value=f"{hand_str(self.dealer_hand)} = **{d_val}**", inline=True)
            await interaction.message.edit(embed=embed, view=self)

        @discord.ui.button(label="HIT", style=discord.ButtonStyle.success, emoji="➕")
        async def btn_hit(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != self.author_id:
                return await interaction.response.defer()
            self.player_hand.append(self.deck.pop())
            p_val = hand_value(self.player_hand)

            if p_val > 21:
                await interaction.response.defer()
                await self._end_game(interaction)
                return

            embed = discord.Embed(
                title="🃏 BLACKJACK",
                description=f"💶 Puntata: **{self.amount} U**\n\n🃏 **Tua mano:** {hand_str(self.player_hand)} = **{p_val}**\n🏦 **Banco:** [{self.dealer_hand[0][0]}{self.dealer_hand[0][1]}] [?]",
                color=discord.Color.dark_teal()
            )
            embed.set_footer(text="HIT / STAND / DOUBLE")
            await interaction.response.edit_message(embed=embed, view=self)

        @discord.ui.button(label="STAND", style=discord.ButtonStyle.primary, emoji="✋")
        async def btn_stand(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != self.author_id:
                return await interaction.response.defer()
            await interaction.response.defer()
            await self._end_game(interaction)

        @discord.ui.button(label="DOUBLE", style=discord.ButtonStyle.danger, emoji="🔥")
        async def btn_double(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != self.author_id:
                return await interaction.response.defer()
            bal = await self.api_ref.get_wallet(self.author_id)
            if bal < self.amount:
                return await interaction.response.send_message(f"❌ Non hai abbastanza U per raddoppiare! (servono {self.amount})", ephemeral=True)
            await self.api_ref.update_balance(self.author_id, -self.amount)
            self.player_hand.append(self.deck.pop())
            await interaction.response.defer()
            await self._end_game(interaction, doubled=True)

        async def on_timeout(self):
            if not self._resolved:
                # Auto-stand
                for child in self.children:
                    child.disabled = True
                while hand_value(self.dealer_hand) < 17:
                    self.dealer_hand.append(self.deck.pop())
                p_val = hand_value(self.player_hand)
                d_val = hand_value(self.dealer_hand)
                bet = self.amount
                if p_val <= 21 and (d_val > 21 or p_val > d_val):
                    await self.api_ref.update_balance(0, -bet)
                    await self.api_ref.update_balance(self.author_id, bet * 2)
                elif p_val <= 21 and p_val == d_val:
                    await self.api_ref.update_balance(self.author_id, bet)
                else:
                    await self.api_ref.update_balance(0, bet)
                if self._message:
                    try:
                        await self._message.edit(content="⏰ **Tempo scaduto** — mano chiusa automaticamente.", view=self)
                    except Exception:
                        pass

    @bot.command(name='blackjack')
    async def blackjack_command(ctx, amount: int):
        """🃏 Blackjack contro il banco! Hit, Stand o Double."""
        if amount <= 0:
            return await ctx.send("❌ Importo positivo (es. `!blackjack 50`).")
        if amount > 1000:
            return await ctx.send("❌ Puntata massima: 1000 U.")

        balance = await api.get_wallet(ctx.author.id)
        if balance < amount:
            return await ctx.send(f"❌ Saldo insufficiente! Hai **{balance} U**.")
        await api.update_balance(ctx.author.id, -amount)

        # Crea mazzo e mescola
        deck = [(r, s) for s in CARD_SUITS for r in CARD_RANKS]
        random.shuffle(deck)

        player_hand = [deck.pop(), deck.pop()]
        dealer_hand = [deck.pop(), deck.pop()]

        p_val = hand_value(player_hand)
        d_up = dealer_hand[0]

        if p_val == 21:  # Blackjack istantaneo!
            winnings = int(amount * 2.5)
            house = await api.get_wallet(0)
            net = winnings - amount
            if house >= net:
                await api.update_balance(0, -net)
                await api.update_balance(ctx.author.id, net)
            new_bal = await api.get_wallet(ctx.author.id)
            embed = discord.Embed(
                title="🃏 BLACKJACK! 🎉",
                description=f"**21** con le prime due carte!\nVinti **{winnings} U** (x2.5)\n💰 Nuovo saldo: **{new_bal} U**",
                color=discord.Color.gold()
            )
            embed.add_field(name="🃏 Tua mano", value=f"{hand_str(player_hand)} = **21**", inline=True)
            embed.add_field(name="🏦 Banco", value=f"{hand_str(dealer_hand)} = **{hand_value(dealer_hand)}**", inline=True)
            return await ctx.send(embed=embed)

        embed = discord.Embed(
            title="🃏 BLACKJACK",
            description=f"💶 Puntata: **{amount} U**\n\n🃏 **Tua mano:** {hand_str(player_hand)} = **{p_val}**\n🏦 **Banco:** [{d_up[0]}{d_up[1]}] [?]",
            color=discord.Color.dark_teal()
        )
        embed.set_footer(text="Scegli: HIT / STAND / DOUBLE (raddoppia puntata + 1 carta)")

        view = BlackjackView(amount, api, ctx.author.id, player_hand, dealer_hand, deck)
        msg = await ctx.send(embed=embed, view=view)
        view.set_message(msg)
    # !lotto / !estrai_lotto
    # =========================================================================
    @bot.command(name='lotto')
    async def lotto_command(ctx, amount: int):
        """Partecipa alla lotteria settimanale."""
        if amount <= 0:
            return await ctx.send("❌ Inserisci un importo positivo (es. `!lotto 50`).")
        if amount > 200:
            return await ctx.send("❌ Massimo 200 U a biglietto.")

        week_start = lottery_week_start()

        async with api.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO user_wallets (discord_id, balance) VALUES ($1, 100) "
                    "ON CONFLICT (discord_id) DO NOTHING", ctx.author.id
                )
                result = await conn.fetchrow(
                    "UPDATE user_wallets SET balance = balance - $1 "
                    "WHERE discord_id = $2 AND balance >= $1 RETURNING balance",
                    amount, ctx.author.id
                )
                if not result:
                    return await ctx.send("❌ Saldo insufficiente.")

                await conn.execute(
                    "INSERT INTO lottery_entries (discord_id, amount, week_start) VALUES ($1, $2, $3)",
                    ctx.author.id, amount, week_start
                )

            pool_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(amount), 0) AS pool, COUNT(*) AS tickets "
                "FROM lottery_entries WHERE week_start = $1",
                week_start
            )

        pool = pool_row['pool']
        tickets = pool_row['tickets']

        embed = discord.Embed(
            title="🎟️ Lotteria Settimanale",
            description=f"Biglietto da **{amount} U** acquistato!\n\n"
                        f"📅 Settimana del {week_start.strftime('%d/%m/%Y')}\n"
                        f"🎫 Biglietti venduti: **{tickets}**\n"
                        f"💰 Montepremi attuale: **{pool} U**\n\n"
                        f"🏆 70% al vincitore | 🔥 30% tassa",
            color=discord.Color.purple()
        )
        embed.set_footer(text="!estrai_lotto — domenica sera (admin)")
        await ctx.send(embed=embed)

    @bot.command(name='estrai_lotto')
    @is_bot_admin()
    async def estrai_lotto_command(ctx):
        """Estrae il vincitore della lotteria (solo admin)."""
        week_start = lottery_week_start()

        async with api.pool.acquire() as conn:
            async with conn.transaction():
                entries = await conn.fetch(
                    "SELECT id, discord_id, amount FROM lottery_entries WHERE week_start = $1",
                    week_start
                )
                if not entries:
                    return await ctx.send(f"📭 Nessun biglietto per la settimana del {week_start}.")

                pool = sum(e['amount'] for e in entries)
                prize = int(pool * 0.70)
                tax = pool - prize
                if tax > 0:
                    await conn.execute(
                        "INSERT INTO house_earnings (source, source_id, amount) VALUES ('lotteria', 0, $1)",
                        tax
                    )

                weighted = []
                for e in entries:
                    weighted.extend([e] * e['amount'])
                winner = random.choice(weighted)

                await conn.execute(
                    "UPDATE user_wallets SET balance = balance + $1 WHERE discord_id = $2",
                    prize, winner['discord_id']
                )
                await conn.execute("DELETE FROM lottery_entries WHERE week_start = $1", week_start)

        user = bot.get_user(winner['discord_id'])
        winner_name = user.name if user else f"User {winner['discord_id']}"

        lines = []
        for e in entries:
            u = bot.get_user(e['discord_id'])
            uname = u.name if u else f"User {e['discord_id']}"
            lines.append(f"- {uname}: {e['amount']} U")
        part_list = "\n".join(lines)

        embed = discord.Embed(
            title="🏆 Estrazione Lotteria!",
            description=f"📅 {week_start.strftime('%d/%m/%Y')}\n🎫 {len(entries)} biglietti | 💰 Pool: {pool} U\n"
                        f"🎉 **{winner_name}** vince **{prize} U**!\n\n🎲 Partecipanti:\n{part_list}",
            color=discord.Color.gold()
        )
        embed.set_footer(text="🔥 30% bruciato nel vuoto cosmico")
        await ctx.send(embed=embed)

    # =========================================================================
    # !torneo / !tbet
    # =========================================================================
    @bot.command(name='torneo')
    @is_bot_admin()
    async def torneo_command(ctx, action: str, *, args: str = ""):
        """Gestione torneo. Azioni: crea, risolvi, lista"""
        if action == "crea":
            parts = [p.strip().strip('"') for p in args.split('"') if p.strip()]
            if len(parts) < 3:
                return await ctx.send('❌ Uso: `!torneo crea "Nome" "P1" "P2"`')
            name, p1, p2 = parts[0], parts[1], parts[2]
            async with api.pool.acquire() as conn:
                match_id = await conn.fetchval(
                    "INSERT INTO tournament_matches (name, player1, player2, channel_id) "
                    "VALUES ($1, $2, $3, $4) RETURNING id",
                    name, p1, p2, ctx.channel.id
                )
            embed = discord.Embed(
                title=f"🏟️ Torneo — {name}",
                description=f"ID: **{match_id}** | 🔵 **{p1}** vs 🔴 **{p2}**\n"
                            f"`!tbet {match_id} <1|2> <importo>`",
                color=discord.Color.blue()
            )
            await ctx.send(embed=embed)

        elif action == "risolvi":
            parts = args.split()
            if len(parts) < 2:
                return await ctx.send("❌ Uso: `!torneo risolvi <match_id> <1|2>`")
            match_id = int(parts[0])
            winner = int(parts[1])
            if winner not in (1, 2):
                return await ctx.send("❌ Winner: 1 o 2.")

            async with api.pool.acquire() as conn:
                async with conn.transaction():
                    match = await conn.fetchrow(
                        "UPDATE tournament_matches SET status = 'resolved', winner = $1 "
                        "WHERE id = $2 AND status = 'betting' RETURNING id, player1, player2",
                        winner, match_id
                    )
                    if not match:
                        return await ctx.send("❌ Match non trovato o già risolto.")
                    bets = await conn.fetch(
                        "SELECT discord_id, amount, prediction FROM tournament_bets WHERE match_id = $1",
                        match_id
                    )
                    if not bets:
                        return await ctx.send("✅ Match risolto. Nessuna scommessa.")

                    pool = sum(b['amount'] for b in bets)
                    tax = int(pool * 0.05)
                    net = pool - tax
                    if tax > 0:
                        await conn.execute(
                            "INSERT INTO house_earnings (source, source_id, amount) VALUES ('torneo', $1, $2)",
                            match_id, tax
                        )
                    win_bets = [b for b in bets if b['prediction'] == winner]
                    w_total = sum(b['amount'] for b in win_bets)

                    msg = (f"🏆 **{match['player1']} vs {match['player2']}**\n"
                           f"Vince: **{match['player1'] if winner == 1 else match['player2']}**!\n"
                           f"💰 Pool: {pool} U | Tassa: {tax} U\n\n**Pagamenti:**\n")

                    if w_total > 0:
                        mult = net / w_total
                        for b in win_bets:
                            winnings = int(net * (b['amount'] / w_total))
                            await conn.execute(
                                "UPDATE user_wallets SET balance = balance + $1 WHERE discord_id = $2",
                                winnings, b['discord_id']
                            )
                            uname = (bot.get_user(b['discord_id']).name if bot.get_user(b['discord_id']) else f"User {b['discord_id']}")
                            msg += f"- {uname}: +{winnings} U (x{mult:.2f})\n"
                    else:
                        for b in bets:
                            await conn.execute(
                                "UPDATE user_wallets SET balance = balance + $1 WHERE discord_id = $2",
                                b['amount'], b['discord_id']
                            )
                            uname = (bot.get_user(b['discord_id']).name if bot.get_user(b['discord_id']) else f"User {b['discord_id']}")
                            msg += f"- {uname}: Rimborso {b['amount']} U\n"

            await ctx.send(msg)

        elif action == "lista":
            async with api.pool.acquire() as conn:
                matches = await conn.fetch(
                    "SELECT id, name, player1, player2 FROM tournament_matches "
                    "WHERE status = 'betting' ORDER BY id DESC LIMIT 10"
                )
            if not matches:
                return await ctx.send("Nessun match di torneo attivo.")
            msg = "**🏟️ Match Torneo Attivi:**\n"
            for m in matches:
                msg += f"- ID **{m['id']}**: {m['player1']} vs {m['player2']} ({m['name']})\n"
            msg += "\nScommetti con `!tbet <id> <1|2> <importo>`"
            await ctx.send(msg)

        else:
            await ctx.send("❌ Azioni: `crea`, `risolvi`, `lista`")

    @bot.command(name='tbet')
    async def tbet_command(ctx, match_id: int, prediction: int, amount: int):
        """Scommetti su un match torneo. Uso: !tbet <id> <1|2> <importo>"""
        if prediction not in (1, 2):
            return await ctx.send("❌ Scegli 1 o 2.")
        if amount <= 0:
            return await ctx.send("❌ Importo positivo.")

        async with api.pool.acquire() as conn:
            async with conn.transaction():
                match = await conn.fetchrow(
                    "SELECT id, player1, player2 FROM tournament_matches "
                    "WHERE id = $1 AND status = 'betting' FOR UPDATE",
                    match_id
                )
                if not match:
                    return await ctx.send("❌ Match non trovato o già chiuso.")
                await conn.execute(
                    "INSERT INTO user_wallets (discord_id, balance) VALUES ($1, 100) "
                    "ON CONFLICT (discord_id) DO NOTHING", ctx.author.id
                )
                result = await conn.fetchrow(
                    "UPDATE user_wallets SET balance = balance - $1 "
                    "WHERE discord_id = $2 AND balance >= $1 RETURNING balance",
                    amount, ctx.author.id
                )
                if not result:
                    return await ctx.send("❌ Saldo insufficiente.")
                await conn.execute(
                    "INSERT INTO tournament_bets (match_id, discord_id, amount, prediction) "
                    "VALUES ($1, $2, $3, $4)",
                    match_id, ctx.author.id, amount, prediction
                )

        await ctx.send(
            f"✅ Scommessi **{amount} U** su **{match['player1'] if prediction == 1 else match['player2']}** "
            f"nel match `{match_id}`!"
        )

    # =========================================================================
    # !indovina / !risolvi_indovina
    # =========================================================================
    @bot.command(name='indovina')
    async def indovina_command(ctx, dito_id: int, guessed_diff: int):
        """Indovina il differenziale ELO tra i team. 10 U, x3 al vincitore!"""
        if guessed_diff < 0:
            return await ctx.send("❌ Il differenziale deve essere positivo.")

        async with api.pool.acquire() as conn:
            async with conn.transaction():
                dito = await conn.fetchrow(
                    "SELECT id FROM diti_active WHERE id = $1", dito_id
                )
                if not dito:
                    return await ctx.send("❌ Dito non trovato.")
                await conn.execute(
                    "INSERT INTO user_wallets (discord_id, balance) VALUES ($1, 100) "
                    "ON CONFLICT (discord_id) DO NOTHING", ctx.author.id
                )
                result = await conn.fetchrow(
                    "UPDATE user_wallets SET balance = balance - 10 "
                    "WHERE discord_id = $1 AND balance >= 10 RETURNING balance",
                    ctx.author.id
                )
                if not result:
                    return await ctx.send("❌ Servono almeno 10 U per partecipare.")
                await conn.execute(
                    "INSERT INTO balance_guesses (dito_id, discord_id, guessed_diff) "
                    "VALUES ($1, $2, $3)",
                    dito_id, ctx.author.id, guessed_diff
                )

        await ctx.send(
            f"🎯 Differenziale **{guessed_diff} ELO** per Dito #{dito_id}!\n"
            f"Chi si avvicina di più vince **30 U**!"
        )

    @bot.command(name='risolvi_indovina')
    @is_bot_admin()
    async def risolvi_indovina_command(ctx, dito_id: int):
        """Risolve la gara di bilanciamento (solo admin)."""
        async with api.pool.acquire() as conn:
            async with conn.transaction():
                dito = await conn.fetchrow(
                    "SELECT team1_avg_elo, team2_avg_elo FROM diti_active WHERE id = $1",
                    dito_id
                )
                if not dito:
                    return await ctx.send("❌ Dito non trovato.")
                actual_diff = abs(dito['team1_avg_elo'] - dito['team2_avg_elo'])
                guesses = await conn.fetch(
                    "SELECT id, discord_id, guessed_diff FROM balance_guesses "
                    "WHERE dito_id = $1 AND reward_paid = FALSE",
                    dito_id
                )
                if not guesses:
                    return await ctx.send("Nessuna puntata per questa gara.")
                best = min(guesses, key=lambda g: abs(g['guessed_diff'] - actual_diff))
                reward = 30
                await conn.execute(
                    "UPDATE user_wallets SET balance = balance + $1 WHERE discord_id = $2",
                    reward, best['discord_id']
                )
                await conn.execute(
                    "UPDATE balance_guesses SET actual_diff = $1, reward_paid = TRUE "
                    "WHERE dito_id = $2", actual_diff, dito_id
                )
            user = bot.get_user(best['discord_id'])
            name = user.name if user else f"User {best['discord_id']}"
            await ctx.send(
                f"🎯 **Risultato Gara Dito #{dito_id}!**\n"
                f"Differenziale reale: **{actual_diff} ELO**\n"
                f"🏆 {name} vince **{reward} U** "
                f"(aveva detto {best['guessed_diff']}, scarto {abs(best['guessed_diff'] - actual_diff)})!"
            )

    # =========================================================================
    # !banco — Statistiche casa (solo admin)
    # =========================================================================
    @bot.command(name='banco')
    @is_bot_admin()
    async def banco_command(ctx):
        """Mostra gli incassi della casa e il saldo disponibile (solo admin)."""
        stats = await api.get_house_stats()
        house_wallet = await api.get_wallet(0)  # wallet reale del banco

        embed = discord.Embed(
            title="🏦 Banco (Casa)",
            description=f"💵 **Disponibile: {house_wallet} U**\n💰 Tasse accumulate: {stats['total']} U",
            color=discord.Color.dark_gold()
        )

        if stats['by_source']:
            src_text = "\n".join(
                f"- **{s}**: {t} U ({c} operazioni)" for s, t, c in stats['by_source']
            )
            embed.add_field(name="📊 Tasse per Fonte", value=src_text, inline=False)

        if stats['recent']:
            rec_text = "\n".join(
                f"- `{r[3].strftime('%d/%m %H:%M') if r[3] else '?'}` {r[0]} #{r[1]}: **{r[2]} U**"
                for r in stats['recent'][:5]
            )
            embed.add_field(name="🕐 Ultime Tasse", value=rec_text, inline=False)

        embed.set_footer(text="!sync_banco per versare le tasse dadi nel wallet | !premia per distribuire")
        await ctx.send(embed=embed)

    # =========================================================================
    # !reset_wallets — Reset tutti i wallet a 100 U (solo admin)
    # =========================================================================
    @bot.command(name='reset_wallets')
    @is_bot_admin()
    async def reset_wallets_command(ctx):
        """Riporta TUTTI i wallet a 100 U. Usare con cautela!"""
        await ctx.send("⚠️ Sei sicuro? Scrivi `confermo` per resettare tutti i wallet a 100 U.")

        def check(m):
            return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id

        try:
            response = await bot.wait_for('message', check=check, timeout=30)
            if response.content.lower() != 'confermo':
                return await ctx.send("❌ Reset annullato.")
        except asyncio.TimeoutError:
            return await ctx.send("⏰ Tempo scaduto. Reset annullato.")

        async with api.pool.acquire() as conn:
            await conn.execute("UPDATE user_wallets SET balance = 100")
            count = await conn.fetchrow("SELECT COUNT(*) as cnt FROM user_wallets")

        await ctx.send(f"✅ Reset completato! **{count['cnt']}** wallet riportati a **100 U**.")

    # =========================================================================
    # Error handler globale per i comandi features
    # =========================================================================
    @bot.event
    async def on_command_error(ctx, error):
        """Logga gli errori senza crashare il bot."""
        if isinstance(error, commands.CommandNotFound):
            return  # ignora comandi inesistenti
        if isinstance(error, commands.MissingPermissions):
            return await ctx.send("❌ Non hai i permessi per questo comando.", delete_after=10)
        if isinstance(error, (commands.BadArgument, commands.MemberNotFound)):
            return await ctx.send(f"❌ Argomento non valido. Usa `!help` per la sintassi corretta.", delete_after=10)
        # Logga l'errore
        print(f"❌ Errore comando '{ctx.command}': {error}")
        import traceback
        traceback.print_exc()
        await ctx.send(f"❌ Errore interno. Contatta un admin.", delete_after=10)

    # =========================================================================
    # !statbot — Statistiche globali (solo admin)
    # =========================================================================
    @bot.command(name='statbot')
    @is_bot_admin()
    async def statbot_command(ctx):
        """Statistiche globali del bot: utenti, U in circolo, diti."""
        async with api.pool.acquire() as conn:
            wallets = await conn.fetchrow("SELECT COUNT(*) as cnt, COALESCE(SUM(balance), 0) as total FROM user_wallets WHERE discord_id != 0")
            house_bal = await conn.fetchrow("SELECT balance FROM user_wallets WHERE discord_id = 0")
            diti = await conn.fetchrow("SELECT COUNT(*) as cnt FROM diti_active WHERE status IN ('betting', 'closed', 'resolved')")
            bets = await conn.fetchrow("SELECT COUNT(*) as cnt, COALESCE(SUM(amount), 0) as total FROM diti_bets")
            house_tax = await conn.fetchrow("SELECT COALESCE(SUM(amount), 0) as total FROM house_earnings")

        embed = discord.Embed(title="📊 Statistiche Bot", color=discord.Color.teal())
        embed.add_field(name="👥 Utenti con wallet", value=f"**{wallets['cnt']}**", inline=True)
        embed.add_field(name="💰 U in circolo", value=f"**{wallets['total']} U**", inline=True)
        embed.add_field(name="🏦 Banco (dadi)", value=f"**{house_bal['balance']} U**", inline=True)
        embed.add_field(name="🏦 Tasse (diti/lotto)", value=f"**{house_tax['total']} U**", inline=True)
        embed.add_field(name="🎲 Diti totali", value=f"**{diti['cnt']}**", inline=True)
        embed.add_field(name="🎫 Scommesse totali", value=f"**{bets['cnt']}** ({bets['total']} U)", inline=True)
        await ctx.send(embed=embed)

    # =========================================================================
    # !wallets — Lista completa wallet (solo admin)
    # =========================================================================
    @bot.command(name='wallets')
    @is_bot_admin()
    async def wallets_command(ctx):
        """Mostra TUTTI i wallet con dati completi, paginato (solo admin)."""
        async with api.pool.acquire() as conn:
            users = await conn.fetch("""
                SELECT w.discord_id, w.balance, w.last_daily, w.steam_id,
                       COALESCE(b.bets, 0) as bets, COALESCE(b.wagered, 0) as wagered,
                       COALESCE(b.wins, 0) as wins, COALESCE(b.losses, 0) as losses,
                       COALESCE(b.last_bet, NULL) as last_bet
                FROM user_wallets w
                LEFT JOIN (
                    SELECT discord_id, COUNT(*) as bets, SUM(amount) as wagered,
                           SUM(CASE WHEN db.prediction = da.winning_team THEN 1 ELSE 0 END) as wins,
                           SUM(CASE WHEN db.prediction != da.winning_team THEN 1 ELSE 0 END) as losses,
                           MAX(db.created_at) as last_bet
                    FROM diti_bets db
                    LEFT JOIN diti_active da ON db.dito_id = da.id
                    GROUP BY discord_id
                ) b ON w.discord_id = b.discord_id
                WHERE w.discord_id != 0
                ORDER BY w.balance DESC
            """)
            bank = await conn.fetchval("SELECT balance FROM user_wallets WHERE discord_id = 0")
        
        if not users:
            return await ctx.send("Nessun wallet.")
        
        total_u = sum(u['balance'] for u in users)
        
        # Paginazione: 15 utenti per pagina
        per_page = 15
        pages = []
        for p in range(0, len(users), per_page):
            page_users = users[p:p+per_page]
            lines = [f"{'#':>3} {'U':>7} {'Bets':>5} {'W/L':>8} {'WR':>5} {'Daily':>10} {'Steam':>18}"]
            lines.append("-" * 65)
            for i, u in enumerate(page_users, p+1):
                daily = u['last_daily'].strftime('%d/%m') if u['last_daily'] else '-'
                steam = u['steam_id'][:17] if u['steam_id'] else '-'
                wl = f"{u['wins']}/{u['losses']}" if (u['wins']+u['losses']) > 0 else '-'
                wr = f"{u['wins']/(u['wins']+u['losses'])*100:.0f}%" if (u['wins']+u['losses']) > 0 else '-'
                lines.append(f"{i:>3} {u['balance']:>7} {u['bets']:>5} {wl:>8} {wr:>5} {daily:>10} {steam:>18}")
            pages.append("```\n" + "\n".join(lines) + "```")
        
        # Invia prima pagina con riepilogo
        footer = f"👥 {len(users)} utenti | 💰 {total_u} U | 🏦 Banco: {bank} U | Pagina 1/{len(pages)}"
        await ctx.send(pages[0] + "\n" + footer)
        
        # Invia le altre pagine se necessario
        for idx, page in enumerate(pages[1:], 2):
            await ctx.send(page + f"\nPagina {idx}/{len(pages)}")

    # =========================================================================
    # !storico — Cronologia transazioni
    # =========================================================================
    @bot.command(name='storico')
    async def storico_command(ctx, member: discord.Member = None):
        """Mostra la cronologia delle scommesse e transazioni."""
        target = member or ctx.author

        async with api.pool.acquire() as conn:
            # Scommesse risolte
            bets = await conn.fetch(
                """SELECT b.amount, b.prediction, a.lobby_name, a.winning_team, a.status
                   FROM diti_bets b JOIN diti_active a ON b.dito_id = a.id
                   WHERE b.discord_id = $1 AND a.status IN ('resolved', 'cancelled')
                   ORDER BY b.id DESC LIMIT 10""",
                target.id
            )
            # Biglietti lotteria
            lotto = await conn.fetch(
                "SELECT amount, week_start FROM lottery_entries WHERE discord_id = $1 ORDER BY id DESC LIMIT 5",
                target.id
            )
            # Scommesse torneo
            t_bets = await conn.fetch(
                """SELECT tb.amount, tb.prediction, tm.name, tm.winner, tm.status
                   FROM tournament_bets tb JOIN tournament_matches tm ON tb.match_id = tm.id
                   WHERE tb.discord_id = $1 ORDER BY tb.id DESC LIMIT 5""",
                target.id
            )

        embed = discord.Embed(
            title=f"📜 Storico — {target.display_name}",
            color=discord.Color.blue()
        )

        if bets:
            lines = []
            for b in bets:
                if b['status'] == 'cancelled':
                    lines.append(f"♻️ Rimborso {b['amount']} U — `{b['lobby_name']}` (annullato)")
                elif b['prediction'] == b['winning_team']:
                    lines.append(f"✅ +{b['amount']} U — `{b['lobby_name']}` (vinto)")
                else:
                    lines.append(f"❌ -{b['amount']} U — `{b['lobby_name']}` (perso)")
            embed.add_field(name="🎲 Scommesse Dito", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="🎲 Scommesse Dito", value="*Nessuna*", inline=False)

        if t_bets:
            lines = []
            for b in t_bets:
                if b['status'] == 'resolved':
                    if b['prediction'] == b['winner']:
                        lines.append(f"✅ +{b['amount']} U — `{b['name']}` (vinto)")
                    else:
                        lines.append(f"❌ -{b['amount']} U — `{b['name']}` (perso)")
                else:
                    lines.append(f"⏳ {b['amount']} U — `{b['name']}` (in corso)")
            embed.add_field(name="🏟️ Torneo", value="\n".join(lines), inline=False)

        if lotto:
            lines = [f"🎟️ {b['amount']} U — sett. {b['week_start'].strftime('%d/%m')}" for b in lotto]
            embed.add_field(name="🎟️ Lotteria", value="\n".join(lines), inline=False)

        await ctx.send(embed=embed)

    # =========================================================================
    # !verifica / !ignora_risultato — Auto-risoluzione
    # =========================================================================
    @bot.command(name='verifica')
    @is_bot_admin()
    async def verifica_command(ctx, dito_id: int):
        """Forza il controllo del risultato per un dito specifico."""
        from bot import dito_profile_ids, CIV_NAMES
        team_info = dito_profile_ids.get(dito_id)
        if not team_info:
            return await ctx.send(f"❌ Nessun profile_id salvato per il Dito #{dito_id}.")
        match = await api.get_recent_match_for_profiles(team_info)
        if not match:
            return await ctx.send(f"📭 Nessuna partita trovata per il Dito #{dito_id}.")

        results = match.get("results", [])

        pid_to_dito_team = {}
        if isinstance(team_info, dict):
            for pid in team_info.get('t1_pids', []):
                pid_to_dito_team[pid] = 1
            for pid in team_info.get('t2_pids', []):
                pid_to_dito_team[pid] = 2

        team_wins = {}
        t1_lines, t2_lines, unknown_lines = [], [], []
        for r in results:
            pid = r.get("profile_id")
            tid = r.get("teamid", -1)
            won = r.get("resulttype") == 1
            if won:
                team_wins[tid] = team_wins.get(tid, 0) + 1

            civ = CIV_NAMES.get(r.get("civilization_id", 0), f"Civ{r.get('civilization_id',0)}")
            icon = "✅" if won else "❌"
            player_str = f"{icon} **{r['name']}** ({civ})"

            dt = pid_to_dito_team.get(pid, 0)
            if dt == 1:
                t1_lines.append(player_str)
            elif dt == 2:
                t2_lines.append(player_str)
            else:
                unknown_lines.append(player_str)

        winner = max(team_wins, key=team_wins.get) if team_wins else None
        dito_winner = None
        if winner is not None:
            for r in results:
                if r.get("teamid") == winner and r.get("resulttype") == 1:
                    dt = pid_to_dito_team.get(r.get("profile_id"), 0)
                    if dt in (1, 2):
                        dito_winner = dt
                        break
        if dito_winner is None:
            dito_winner = winner + 1 if winner is not None else "?"

        raw_map = match.get('mapname', '?').replace('.rms', '')
        mapname = '🎲 Custom' if raw_map == 'my map' else raw_map
        desc = match.get('description', '')
        msg = f"🔍 **Verifica Dito #{dito_id}** — Match `{match.get('match_id')}`\n"
        msg += f"🗺️ {mapname} | {desc}\n\n"
        msg += "🔵 **Team 1:**\n"
        msg += ("\n".join(t1_lines) if t1_lines else "*Nessuno*") + "\n\n"
        msg += "🔴 **Team 2:**\n"
        msg += ("\n".join(t2_lines) if t2_lines else "*Nessuno*")
        if unknown_lines:
            msg += "\n\n❓ **Altri:**\n" + "\n".join(unknown_lines)
        msg += f"\n\n🏆 Vince **Team {dito_winner}** → `!risolvi_dito {dito_id} {dito_winner}`"
        await ctx.send(msg)

    @bot.command(name='ignora_risultato')
    @is_bot_admin()
    async def ignora_risultato_command(ctx, dito_id: int):
        """Smette di cercare risultati per questo dito."""
        from bot import dito_profile_ids
        if dito_id in dito_profile_ids:
            del dito_profile_ids[dito_id]
            await ctx.send(f"🔕 Non cercherò più risultati per il Dito #{dito_id}.")
        else:
            await ctx.send(f"ℹ️ Il Dito #{dito_id} non era in monitoraggio.")

    # =========================================================================
    # !sync_banco — Sincronizza house_earnings dadi nel wallet del banco
    # =========================================================================
    @bot.command(name='sync_banco')
    @is_bot_admin()
    async def sync_banco_command(ctx):
        """Versa TUTTI gli incassi casa nel wallet del banco (solo admin)."""
        async with api.pool.acquire() as conn:
            total_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(amount), 0) as total FROM house_earnings"
            )
            total = total_row['total']
            if total <= 0:
                return await ctx.send("Nessun incasso da sincronizzare.")
            
            # Mostra breakdown per fonte
            by_source = await conn.fetch(
                "SELECT source, COALESCE(SUM(amount),0) as tot FROM house_earnings GROUP BY source"
            )
            breakdown = "\n".join([f"  {r['source']}: {r['tot']} U" for r in by_source])

            await conn.execute(
                "INSERT INTO user_wallets (discord_id, balance) VALUES (0, $1) "
                "ON CONFLICT (discord_id) DO UPDATE SET balance = user_wallets.balance + $1",
                total
            )
            await conn.execute("DELETE FROM house_earnings")
            
            house_bal = await conn.fetchrow("SELECT balance FROM user_wallets WHERE discord_id = 0")

        await ctx.send(f"🏦 **{total} U** versati nel banco!\n{breakdown}\n💰 Banco ora ha: **{house_bal['balance']} U**")

    # === AUTO-SYNC BANCO OGNI ORA ===
    @tasks.loop(hours=1)
    async def auto_sync_banco():
        try:
            async with api.pool.acquire() as conn:
                total = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM house_earnings")
                if total > 0:
                    await conn.execute(
                        "INSERT INTO user_wallets (discord_id, balance) VALUES (0, $1) "
                        "ON CONFLICT (discord_id) DO UPDATE SET balance = user_wallets.balance + $1",
                        total
                    )
                    await conn.execute("DELETE FROM house_earnings")
                    bal = await conn.fetchval("SELECT balance FROM user_wallets WHERE discord_id = 0")
                    print(f"🏦 Auto-sync banco: +{total} U | Saldo: {bal} U")
        except Exception as e:
            print(f"Auto-sync banco error: {e}")

    if not auto_sync_banco.is_running():
        auto_sync_banco.start()

    print("✅ Features: !profilo, !dadi, !lotto, !torneo, !tbet, !indovina, !banco, !reset_wallets, !statbot, !storico, !verifica, !ignora_risultato, !sync_banco")
