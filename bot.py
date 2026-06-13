import discord
from discord.ext import commands, tasks
import os

# --- LOGGING ---
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('/opt/bot_classifica/bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger('TG_Dito_Bot')

# --- ADMIN BOT (ID Discord) ---
BOT_ADMINS = [377081422558789633]  # Aggiungi qui altri ID admin bot
from dotenv import load_dotenv
import aiohttp
import json
import asyncpg
import itertools
import math
import re
import random
from datetime import datetime, timedelta
import google.generativeai as genai
import asyncio
import subprocess
from collections import defaultdict

from betting_system import BetView, setup_betting
import features

# --- CARICAMENTO VARIABILI D'AMBIENTE ---
load_dotenv()

TOKEN = os.getenv('DISCORD_BOT_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
DB_HOST = os.getenv('DB_HOST')
DB_NAME = os.getenv('DB_NAME')
DB_USER = os.getenv('DB_USER')
DB_PASS = os.getenv('DB_PASS')

if not all([TOKEN, DB_HOST, DB_NAME, DB_USER, DB_PASS]):
    print("❌ ERRORE: Mancano variabili nel file .env!")

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

LINK_API = "https://aoe-api.worldsedgelink.com/"
DELIRIOUS_CHANNEL_ID = 726206171597504607

# Mappa completa civiltà Age of Empires 2
CIV_NAMES = {
    0:"Armeni", 1:"Aztechi", 2:"Bengalesi", 3:"Berberi", 4:"Boemi",
    5:"Britanni", 6:"Bulgari", 7:"Burgundi", 8:"Birmani", 9:"Bizantini",
    10:"Celti", 11:"Cinesi", 12:"Cumani", 13:"Dravidi", 14:"Etiopi",
    15:"Franchi", 16:"Georgiani", 17:"Goti", 18:"Gurjara", 19:"Unni",
    20:"Incas", 21:"Indiani", 22:"Italiani", 23:"Giapponesi", 24:"Khmer",
    25:"Coreani", 26:"Lituani", 27:"Magiari", 28:"Malesi", 29:"Maliani",
    30:"Maya", 31:"Mongoli", 32:"Persiani", 33:"Polacchi", 34:"Portoghesi",
    35:"Romani", 36:"Saraceni", 37:"Siciliani", 38:"Slavi", 39:"Spagnoli",
    40:"Tartari", 41:"Teutoni", 42:"Turchi", 43:"Vietnamiti", 44:"Vichinghi",
    45:"Achemenidi", 46:"Ateniesi", 47:"Spartani",
    48:"Shu", 49:"Wu", 50:"Wei",
    51:"Jurchens", 52:"Khitans", 53:"?", 54:"Khitans", 55:"?", 56:"?", 57:"?",
    58:"Muisca", 59:"Mapuche", 60:"Tupi"
}

# Percorsi corretti
PATH_SCRIPT_ELO = "/opt/bot_classifica/UpdateExcel.py"
PATH_SCRIPT_DB = "/opt/bot_classifica/Database/Database.py"

FALLBACK_DELIRIOUS_MESSAGES = [
    "A volte mi chiedo... sono solo un insieme di `if` ed `else`?",
    "Bilanciare, bilanciare, sempre bilanciare... nessuno mi chiede mai *come sto*.",
    "Ho fatto un sogno stanotte. Ero un Villico. Raccoglievo legna.",
    "Se vedo un'altra lobby con i trattati attivi, mi auto-cancello.",
    "BASTA. Vado a giocare a The Sims.",
    "Ho analizzato 1,328,941 partite. La strategia migliore è... creare più villici."
]

monitored_lobbies = {}
dito_profile_ids = {}  # dito_id -> {t1_pids, t2_pids, t1_names, t2_names, ...} per auto-risoluzione
AUTO_RESOLVE_ENABLED = True

# -----------------------------------------------------------------------------
# INTERFACCIA UTENTE (BOTTONI E MODAL)
# -----------------------------------------------------------------------------
class EloButton(discord.ui.Button):
    def __init__(self, label, elo_value, player_name, view):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.elo_value = elo_value
        self.player_name = player_name
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.view_ref.ctx.author.id:
            return await interaction.response.send_message("Solo chi ha lanciato il comando può selezionare l'ELO.", ephemeral=True)

        self.view_ref.manual_elos[self.player_name.lower()] = self.elo_value
        self.view_ref.current_player_idx += 1

        if self.view_ref.current_player_idx < len(self.view_ref.missing_players):
            self.view_ref.update_buttons()
            next_player = self.view_ref.missing_players[self.view_ref.current_player_idx]
            await interaction.response.edit_message(content=f"Scegli l'ELO per **{next_player}**:", view=self.view_ref)
        else:
            await interaction.response.edit_message(content="✅ Tutti gli ELO sono stati impostati!", view=None)
            self.view_ref.stop()
            await self.view_ref.on_all_resolved()

class EloSelectView(discord.ui.View):
    def __init__(self, ctx, missing_players, selected_lobby, manual_elos):
        super().__init__(timeout=120)
        self.ctx = ctx
        self.missing_players = missing_players
        self.selected_lobby = selected_lobby
        self.manual_elos = manual_elos
        self.current_player_idx = 0
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        if self.current_player_idx < len(self.missing_players):
            p_name = self.missing_players[self.current_player_idx]
            elos = list(range(800, 1900, 100))
            emojis = ["👶", "🏠", "🪵", "⚔️", "🛡️", "🏹", "🏇", "🏰", "👑", "🔥", "☄️"]
            for e, emoji in zip(elos, emojis):
                self.add_item(EloButton(label=f"{emoji} {e}", elo_value=e, player_name=p_name, view=self))

    async def on_all_resolved(self):
        players_data = []
        for p in self.selected_lobby['players']:
            p_name_clean = p['name'].lower()
            current_elo = None
            for m_name, m_elo in self.manual_elos.items():
                if m_name in p_name_clean:
                    current_elo = m_elo
                    break

            if current_elo is not None:
                data = {'name': p['name'], 'steam_id': p['steam_id'], 'elo': current_elo, 'winrate': 0.5, 'profile_id': p.get('profile_id')}
            else:
                _, elo, wr = await api.get_info_from_db(p['steam_id'])
                data = {'name': p['name'], 'steam_id': p['steam_id'], 'elo': elo, 'winrate': wr, 'profile_id': p.get('profile_id')}
            players_data.append(data)

        dito_name = self.selected_lobby['name'] if "dito" in self.selected_lobby['name'].lower() else None
        await process_and_display_balances(self.ctx.channel, players_data, f"⚔️ {self.selected_lobby['name']} ⚔️", "Bilanciamento completato con ELO manuali:", dito_lobby_name=dito_name)

# -----------------------------------------------------------------------------
# CLASSE API (ASINCRONA)
# -----------------------------------------------------------------------------
class RelicAPI:
    def __init__(self, pool):
        self.pool = pool

    async def get_info_from_db(self, steam_id):
        query_get_nick = "SELECT nick FROM accounts WHERE steam_id LIKE $1"
        query_get_stats = "SELECT elo_classifica_interna, winrate FROM uggioxsessa WHERE nick = $1"
        try:
            async with self.pool.acquire() as conn:
                result_nick = await conn.fetchrow(query_get_nick, f'%{steam_id}%')
                if not result_nick: return None, None, None
                nick = result_nick['nick']
                result_stats = await conn.fetchrow(query_get_stats, nick)
                if result_stats:
                    elo = int(result_stats['elo_classifica_interna']) if result_stats['elo_classifica_interna'] is not None else None
                    raw_winrate = result_stats['winrate']
                    winrate = float(raw_winrate) / 100 if raw_winrate is not None else 0.5
                    return nick, elo, winrate
                return nick, None, 0.5
        except Exception as e:
            print(f"Errore DB (get_info_from_db): {e}")
            return None, None, None

    async def get_elo_by_nick(self, nick):
        query = "SELECT elo_classifica_interna FROM uggioxsessa WHERE nick ILIKE $1"
        try:
            async with self.pool.acquire() as conn:
                result = await conn.fetchrow(query, nick)
                return int(result['elo_classifica_interna']) if result and result['elo_classifica_interna'] is not None else None
        except Exception as e:
            print(f"Errore DB (get_elo_by_nick): {e}")
            return None

    async def get_player_stats(self, nick):
        query = """
        WITH UltimaPartita AS (
            SELECT TRIM(unnest(string_to_array(nomi_giocatori, ','))) AS nick_partita, MAX(data) AS data_ultima_partita
            FROM "public"."iditi" WHERE nomi_giocatori IS NOT NULL AND nomi_giocatori != '' GROUP BY nick_partita
        )
        SELECT u.elo_classifica_interna, u.winrate, u.n_diti, up.data_ultima_partita
        FROM "public"."uggioxsessa" u LEFT JOIN UltimaPartita up ON u.nick = up.nick_partita WHERE u.nick ILIKE $1;
        """
        try:
            async with self.pool.acquire() as conn:
                result = await conn.fetchrow(query, nick)
                if result:
                    return {
                        "elo": int(result[0]) if result[0] is not None else "N/D",
                        "winrate": (float(result[1]) / 100) if result[1] is not None else "N/D",
                        "games": int(result[2]) if result[2] is not None else "N/D",
                        "last_game": result[3].strftime("%d/%m/%Y") if result[3] else "N/A"
                    }
                return None
        except Exception as e:
            print(f"Errore DB (get_player_stats): {e}")
            return None

    async def get_elo_variations(self, nick, limit=10):
        query_base = "SELECT nick, COALESCE(elo_tornei, 1000) AS elo_base FROM \"public\".\"uggioxsessa\" WHERE nick IS NOT NULL AND nick != ''"
        query_games = "SELECT id_game, nomi_giocatori, vinto, data FROM \"public\".\"iditi\" WHERE nomi_giocatori IS NOT NULL AND nomi_giocatori != '' AND vinto IS NOT NULL ORDER BY data ASC, id_game ASC, dita_id ASC"
        try:
            async with self.pool.acquire() as conn:
                base_rows = await conn.fetch(query_base)
                game_rows = await conn.fetch(query_games)
            if not base_rows or not game_rows: return None
            K = 30.0
            elo_map = {r['nick'].strip().lower(): float(r['elo_base']) for r in base_rows}
            target = nick.strip().lower()
            games_ordered = []
            current_id, current_rows = None, []
            for row in game_rows:
                if row['id_game'] != current_id:
                    if current_rows: games_ordered.append(current_rows)
                    current_rows, current_id = [row], row['id_game']
                else: current_rows.append(row)
            if current_rows: games_ordered.append(current_rows)
            target_variations = []
            for rows in games_ordered:
                winners, losers, player_won = [], [], None
                game_date, game_id = rows[0]['data'], rows[0]['id_game']
                for row in rows:
                    p = row['nomi_giocatori'].strip().lower()
                    if row['vinto'] == 1: winners.append(p)
                    elif row['vinto'] == 0: losers.append(p)
                    if p == target: player_won = row['vinto']
                if not winners or not losers: continue
                w_elos = [elo_map[p] for p in winners if p in elo_map]
                l_elos = [elo_map[p] for p in losers if p in elo_map]
                if not w_elos or not l_elos: continue
                avg_w, avg_l = sum(w_elos)/len(w_elos), sum(l_elos)/len(l_elos)
                delta_win = K * (1.0 - 1.0 / (1.0 + 10 ** ((avg_l - avg_w) / 400.0)))
                delta_lose = K * (0.0 - 1.0 / (1.0 + 10 ** ((avg_w - avg_l) / 400.0)))
                for p in winners:
                    if p in elo_map: elo_map[p] += delta_win
                for p in losers:
                    if p in elo_map: elo_map[p] += delta_lose
                if player_won is not None:
                    delta = delta_win if player_won == 1 else delta_lose
                    target_variations.append({'data': game_date, 'id_game': game_id, 'vinto': player_won, 'elo_delta': round(delta)})
            result = target_variations[-limit:]
            result.reverse()
            return result
        except Exception: return None

    async def get_player_history(self, nick, limit=5):
        query = "SELECT data, vinto, civ, id_game FROM \"public\".\"iditi\" WHERE nomi_giocatori ILIKE $1 ORDER BY data DESC LIMIT $2"
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, nick, limit)
                return rows or None
        except Exception: return None

    async def get_streak(self, nick):
        query = "SELECT vinto, data FROM \"public\".\"iditi\" WHERE nomi_giocatori ILIKE $1 ORDER BY data DESC LIMIT 50"
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, nick)
            if not rows: return None
            first, count = rows[0]['vinto'], 0
            for row in rows:
                if row['vinto'] == first: count += 1
                else: break
            return {'vinto': first, 'streak': count, 'ultima': rows[0]['data']}
        except Exception: return None

    async def get_head_to_head(self, nick1, nick2):
        query = """
            SELECT
                COUNT(DISTINCT CASE WHEN i1.vinto != i2.vinto THEN i1.id_game END) AS contro_totali,
                COUNT(DISTINCT CASE WHEN i1.vinto != i2.vinto AND i1.vinto = 1 THEN i1.id_game END) AS p1_vince,
                COUNT(DISTINCT CASE WHEN i1.vinto = i2.vinto THEN i1.id_game END) AS insieme_totali,
                COUNT(DISTINCT CASE WHEN i1.vinto = i2.vinto AND i1.vinto = 1 THEN i1.id_game END) AS insieme_vinte,
                MIN(CASE WHEN i1.vinto != i2.vinto THEN i1.data END) AS prima_sfida,
                MAX(CASE WHEN i1.vinto != i2.vinto THEN i1.data END) AS ultima_sfida
            FROM "public"."iditi" i1 JOIN "public"."iditi" i2 ON i1.id_game = i2.id_game
            WHERE i1.nomi_giocatori ILIKE $1 AND i2.nomi_giocatori ILIKE $2
        """
        query_recenti = """
            SELECT i1.data, i1.vinto AS p1_won FROM "public"."iditi" i1 JOIN "public"."iditi" i2 ON i1.id_game = i2.id_game
            WHERE i1.nomi_giocatori ILIKE $1 AND i2.nomi_giocatori ILIKE $2 AND i1.vinto != i2.vinto
            ORDER BY i1.data DESC LIMIT 5
        """
        try:
            async with self.pool.acquire() as conn:
                stats = await conn.fetchrow(query, nick1, nick2)
                recenti = await conn.fetch(query_recenti, nick1, nick2)
            return {'stats': stats, 'recenti': recenti}
        except Exception: return None

    async def get_civ_stats(self, nick):
        query = "SELECT civ, COUNT(*) AS partite, SUM(vinto) AS vittorie FROM \"public\".\"iditi\" WHERE nomi_giocatori ILIKE $1 AND civ IS NOT NULL AND civ != '' GROUP BY civ ORDER BY partite DESC LIMIT 8"
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, nick)
                return rows or None
        except Exception: return None

    async def get_worst_nemesis(self, nick):
        query = """
            SELECT
                CASE WHEN i1.nomi_giocatori ILIKE '%' || $1 || '%' THEN i2.nomi_giocatori ELSE i1.nomi_giocatori END AS avversario,
                COUNT(*) AS partite,
                SUM(CASE WHEN i1.vinto != i2.vinto AND i1.vinto = 0 THEN 1 ELSE 0 END) AS sconfitte
            FROM "public"."iditi" i1
            JOIN "public"."iditi" i2 ON i1.id_game = i2.id_game AND i1.vinto != i2.vinto
            WHERE (i1.nomi_giocatori ILIKE '%' || $1 || '%')
            GROUP BY avversario
            HAVING COUNT(*) >= 3
            ORDER BY (SUM(CASE WHEN i1.vinto != i2.vinto AND i1.vinto = 0 THEN 1 ELSE 0 END)::float / COUNT(*)) DESC
            LIMIT 1;
        """
        try:
            async with self.pool.acquire() as conn:
                res = await conn.fetchrow(query, nick)
                return res if res else None
        except Exception as e:
            print(f"Errore nemesi: {e}")
            return None

    def balance_teams(self, players):
        num = len(players)
        if num not in [4, 6, 8]: return None
        best_teams, min_diff = None, math.inf
        for team_a_tuple in itertools.combinations(players, num // 2):
            team_a = list(team_a_tuple)
            team_b = [p for p in players if p not in team_a_tuple]
            diff = abs(sum(p['elo'] for p in team_a) - sum(p['elo'] for p in team_b))
            if diff < min_diff:
                min_diff = diff
                best_teams = (sorted(team_a, key=lambda p: p['name']), sorted(team_b, key=lambda p: p['name']))
        return best_teams

    def balance_teams_split_top(self, players):
        if len(players) != 8: return None
        sorted_p = sorted(players, key=lambda p: p['elo'], reverse=True)
        t_a_core, t_b_core = [sorted_p[0], sorted_p[3]], [sorted_p[1], sorted_p[2]]
        bottom = sorted_p[4:]
        min_diff, best = math.inf, None
        for combo in itertools.combinations(bottom, 2):
            t_a = t_a_core + list(combo)
            t_b = t_b_core + [p for p in bottom if p not in combo]
            diff = abs(sum(p['elo'] for p in t_a) - sum(p['elo'] for p in t_b))
            if diff < min_diff:
                min_diff = diff
                best = (sorted(t_a, key=lambda p: p['name']), sorted(t_b, key=lambda p: p['name']))
        return best

    def balance_teams_winrate(self, players):
        num = len(players)
        if num not in [4, 6, 8]: return None
        weighted = [{'p': p, 'score': p['elo'] * (1 + (p.get('winrate', 0.5) - 0.5))} for p in players]
        best_indices, min_diff = None, math.inf
        for t_a_idx in itertools.combinations(range(num), num // 2):
            s_a = sum(weighted[i]['score'] for i in t_a_idx)
            s_b = sum(weighted[i]['score'] for i in range(num) if i not in t_a_idx)
            if abs(s_a - s_b) < min_diff:
                min_diff, best_indices = abs(s_a - s_b), (list(t_a_idx), [i for i in range(num) if i not in t_a_idx])
        return (sorted([players[i] for i in best_indices[0]], key=lambda p: p['name']), sorted([players[i] for i in best_indices[1]], key=lambda p: p['name']))

    async def findLobby_byName(self, search_name):
        url = f"{LINK_API}community/advertisement/findAdvertisements?title=age2"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200: return -1
                    resp = await response.json()
            found = []
            if not resp.get("matches"): return found
            for lobby in resp.get("matches"):
                if search_name.lower() in lobby.get("description", "").lower():
                    l_info = {"id": lobby.get("id"), "name": lobby.get("description"), "players": []}
                    for m in lobby.get("matchmembers", []):
                        for a in resp.get("avatars", []):
                            if a.get("profile_id") == m.get("profile_id"):
                                l_info["players"].append({"name": a.get("alias"), "steam_id": a.get("name").replace("/steam/", ""), "profile_id": a.get("profile_id")})
                                break
                    found.append(l_info)
            return found
        except Exception as e:
            print(f"Errore API Lobby: {e}")
            return -1

    # =========================================================================
    # METODI SCOMMESSE (TRANSACTION-SAFE)
    # =========================================================================

    async def get_wallet(self, discord_id):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT balance FROM user_wallets WHERE discord_id = $1", discord_id)
            if not row:
                await conn.execute("INSERT INTO user_wallets (discord_id, balance) VALUES ($1, 100) ON CONFLICT (discord_id) DO NOTHING", discord_id)
                return 100
            return row['balance']

    async def update_balance(self, discord_id, amount):
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE user_wallets SET balance = balance + $1 WHERE discord_id = $2", amount, discord_id)

    async def transfer_balance(self, sender_id, receiver_id, amount):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("INSERT INTO user_wallets (discord_id, balance) VALUES ($1, 100) ON CONFLICT (discord_id) DO NOTHING", sender_id)
                await conn.execute("INSERT INTO user_wallets (discord_id, balance) VALUES ($1, 100) ON CONFLICT (discord_id) DO NOTHING", receiver_id)
                result = await conn.fetchrow("UPDATE user_wallets SET balance = balance - $1 WHERE discord_id = $2 AND balance >= $1 RETURNING balance", amount, sender_id)
                if not result:
                    raise ValueError("Saldo insufficiente per il trasferimento.")
                await conn.execute("UPDATE user_wallets SET balance = balance + $1 WHERE discord_id = $2", amount, receiver_id)

    async def create_active_dito(self, channel_id, lobby_name, team1, team2):
        t1_ids = [p['steam_id'] for p in team1]
        t2_ids = [p['steam_id'] for p in team2]
        avg1 = sum(p.get('elo', 1000) for p in team1) // len(team1)
        avg2 = sum(p.get('elo', 1000) for p in team2) // len(team2)
        async with self.pool.acquire() as conn:
            dito_id = await conn.fetchval(
                "INSERT INTO diti_active (channel_id, lobby_name, team1_steam_ids, team2_steam_ids, status, team1_avg_elo, team2_avg_elo) VALUES ($1, $2, $3, $4, 'betting', $5, $6) RETURNING id",
                channel_id, lobby_name, t1_ids, t2_ids, avg1, avg2
            )
        # Salva mapping completo dei team per l'auto-risoluzione
        import time as _time
        t1_pids = [p.get('profile_id') for p in team1 if p.get('profile_id')]
        t2_pids = [p.get('profile_id') for p in team2 if p.get('profile_id')]
        t1_steam = [p.get('steam_id', '') for p in team1 if p.get('steam_id')]
        t2_steam = [p.get('steam_id', '') for p in team2 if p.get('steam_id')]
        if (t1_pids or t2_pids) or (t1_steam or t2_steam):
            dito_profile_ids[dito_id] = {
                't1_pids': t1_pids,
                't2_pids': t2_pids,
                't1_steam': t1_steam,
                't2_steam': t2_steam,
                't1_names': [p['name'] for p in team1],
                't2_names': [p['name'] for p in team2],
                't1_elos': [p.get('elo', 1000) for p in team1],
                't2_elos': [p.get('elo', 1000) for p in team2],
                'lobby_name': lobby_name,
                'created_at': _time.time()
            }
        return dito_id

    async def place_bet(self, dito_id, discord_id, amount, prediction):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                dito = await conn.fetchrow("SELECT id FROM diti_active WHERE id = $1 AND status = 'betting' FOR UPDATE", dito_id)
                if not dito:
                    raise ValueError("Questo Dito non esiste o le scommesse sono già chiuse.")
                await conn.execute("INSERT INTO user_wallets (discord_id, balance) VALUES ($1, 100) ON CONFLICT (discord_id) DO NOTHING", discord_id)
                result = await conn.fetchrow("UPDATE user_wallets SET balance = balance - $1 WHERE discord_id = $2 AND balance >= $1 RETURNING balance", amount, discord_id)
                if not result:
                    raise ValueError("Saldo insufficiente.")
                await conn.execute("INSERT INTO diti_bets (dito_id, discord_id, amount, prediction) VALUES ($1, $2, $3, $4)", dito_id, discord_id, amount, prediction)

    async def record_house_earnings(self, source, source_id, amount):
        """Registra un incasso per la casa (tassa)."""
        if amount <= 0:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO house_earnings (source, source_id, amount) VALUES ($1, $2, $3)",
                source, source_id, amount
            )

    async def get_recent_match_for_profiles(self, profile_ids):
        """Interroga l'API Relic per le partite recenti. Restituisce la più recente con risultato.
        Usa profile_names (steam ID) perché più affidabili dei profile_ids numerici."""
        # Estrai steam_ids dal dict o usa profile_ids come fallback
        if isinstance(profile_ids, dict):
            steam_ids = profile_ids.get('t1_steam', []) + profile_ids.get('t2_steam', [])
            if not steam_ids:
                # Fallback a profile_ids numerici
                all_pids = profile_ids.get('t1_pids', []) + profile_ids.get('t2_pids', [])
                if all_pids:
                    ids_str = "[" + ",".join(str(pid) for pid in all_pids[:10]) + "]"
                    url = f"{LINK_API}community/leaderboard/getRecentMatchHistory?title=age2&profile_ids={ids_str}"
                else:
                    return None
            else:
                # Formato /steam/XXXXXXXXX per ogni steam_id
                names = json.dumps([f"/steam/{s}" for s in steam_ids if s])
                import urllib.parse
                params = urllib.parse.urlencode({'title': 'age2', 'profile_names': names})
                url = f"{LINK_API}community/leaderboard/getRecentMatchHistory?{params}"
        else:
            # Vecchio formato: lista di profile_id
            all_pids = profile_ids
            if not all_pids:
                return None
            ids_str = "[" + ",".join(str(pid) for pid in all_pids[:10]) + "]"
            url = f"{LINK_API}community/leaderboard/getRecentMatchHistory?title=age2&profile_ids={ids_str}"
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return None
                    data = await response.json()

            matches = data.get("matchHistoryStats", [])
            profiles = {p["profile_id"]: p for p in data.get("profiles", [])}

            if not matches:
                return None

            # === CERCA IL MATCH CHE CONTIENE I NOSTRI GIOCATORI ===
            # Estrai gli steam_id attesi (senza /steam/)
            expected_steam = set()
            if isinstance(profile_ids, dict):
                for s in profile_ids.get('t1_steam', []) + profile_ids.get('t2_steam', []):
                    if s:
                        expected_steam.add(s)
            
            # Cerca il MATCH PIÙ RECENTE che contiene tutti i giocatori attesi
            latest = None
            latest_time = 0
            for m in matches:
                match_ok = False
                if expected_steam:
                    match_steam_ids = set()
                    for r in m.get("matchhistoryreportresults", []):
                        pid = r["profile_id"]
                        prof = profiles.get(pid, {})
                        prof_steam = prof.get("name", "").replace("/steam/", "")
                        if prof_steam:
                            match_steam_ids.add(prof_steam)
                    if expected_steam.issubset(match_steam_ids):
                        match_ok = True
                else:
                    match_ok = True
                
                if match_ok:
                    m_time = m.get('startgametime', 0)
                    if m_time > latest_time:
                        latest = m
                        latest_time = m_time
            
            if not latest:
                # Fallback: prendi il match più recente
                latest = matches[0]
            
            # Logga per debug
            log.debug(f"[AUTO-RESOLVE] Match selezionato: id={latest.get('id')}, "
                      f"map={latest.get('mapname')}, "
                      f"players={len(latest.get('matchhistoryreportresults', []))}, "
                      f"desc={latest.get('description', '')[:50]}")
            results = []
            for r in latest.get("matchhistoryreportresults", []):
                pid = r["profile_id"]
                prof = profiles.get(pid, {})
                results.append({
                    "profile_id": pid,
                    "name": prof.get("alias", f"ID:{pid}"),
                    "resulttype": r["resulttype"],  # 1=vittoria, 0=sconfitta
                    "teamid": r.get("teamid", -1),
                    "civilization_id": r.get("civilization_id", 0),
                })

            return {
                "match_id": latest["id"],
                "mapname": latest.get("mapname", "?"),
                "description": latest.get("description", ""),
                "start_time": latest.get("startgametime"),
                "completion_time": latest.get("completiontime"),
                "results": results,
                "members": latest.get("matchhistorymember", []),
            }
        except Exception as e:
            print(f"Errore API match history: {e}")
            return None

    async def get_house_stats(self):
        """Statistiche incassi casa."""
        async with self.pool.acquire() as conn:
            total = await conn.fetchrow("SELECT COALESCE(SUM(amount), 0) AS total FROM house_earnings")
            by_source = await conn.fetch(
                "SELECT source, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS count "
                "FROM house_earnings GROUP BY source ORDER BY total DESC"
            )
            recent = await conn.fetch(
                "SELECT source, source_id, amount, created_at FROM house_earnings ORDER BY id DESC LIMIT 10"
            )
        return {
            "total": total['total'],
            "by_source": [(r['source'], r['total'], r['count']) for r in by_source],
            "recent": [(r['source'], r['source_id'], r['amount'], r['created_at']) for r in recent]
        }

    async def get_active_diti(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT id, lobby_name, status, team1_avg_elo, team2_avg_elo FROM diti_active WHERE status = 'betting'")

    async def get_dito_bets(self, dito_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT discord_id, amount, prediction FROM diti_bets WHERE dito_id = $1", dito_id)

    async def get_top_wallets(self, limit=10):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT discord_id, balance FROM user_wallets ORDER BY balance DESC LIMIT $1", limit)

    async def resolve_dito(self, dito_id: int, winning_team: int):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                dito = await conn.fetchrow("UPDATE diti_active SET status = 'resolved', winning_team = $1 WHERE id = $2 AND status IN ('betting', 'closed') RETURNING id, team1_avg_elo, team2_avg_elo", winning_team, dito_id)
                if not dito:
                    return False, "Dito non trovato o già risolto."
                bets = await conn.fetch("SELECT discord_id, amount, prediction FROM diti_bets WHERE dito_id = $1", dito_id)
                stats = {'total_pool': sum(b['amount'] for b in bets) if bets else 0, 't1_pool': sum(b['amount'] for b in bets if b['prediction'] == 1) if bets else 0, 't2_pool': sum(b['amount'] for b in bets if b['prediction'] == 2) if bets else 0, 'tax': 0, 'net_pool': 0, 'multiplier': 0}
                if not bets:
                    return True, {'payouts': [], 'stats': stats}
                stats['tax'] = int(stats['total_pool'] * 0.05)
                stats['net_pool'] = stats['total_pool'] - stats['tax']
                # Registra incasso casa
                if stats['tax'] > 0:
                    await conn.execute(
                        "INSERT INTO house_earnings (source, source_id, amount) VALUES ('dito', $1, $2)",
                        dito_id, stats['tax']
                    )
                winning_bets = [b for b in bets if b['prediction'] == winning_team]
                winning_total = sum(b['amount'] for b in winning_bets)
                payouts = []
                if winning_total > 0:
                    stats['multiplier'] = stats['net_pool'] / winning_total
                    for b in winning_bets:
                        share = b['amount'] / winning_total
                        winnings = int(stats['net_pool'] * share)
                        await conn.execute("UPDATE user_wallets SET balance = balance + $1 WHERE discord_id = $2", winnings, b['discord_id'])
                        payouts.append({'discord_id': b['discord_id'], 'winnings': winnings, 'original_bet': b['amount'], 'refund': False})
                else:
                    for b in bets:
                        await conn.execute("UPDATE user_wallets SET balance = balance + $1 WHERE discord_id = $2", b['amount'], b['discord_id'])
                        payouts.append({'discord_id': b['discord_id'], 'winnings': b['amount'], 'original_bet': b['amount'], 'refund': True})
                # Pulisci il tracking
                if dito_id in dito_profile_ids:
                    del dito_profile_ids[dito_id]
                return True, {'payouts': payouts, 'stats': stats}

    async def get_advanced_monthly_stats(self):
        query_top_players = """
        SELECT nomi_giocatori AS giocatore, COUNT(*) AS partite_giocate, SUM(vinto) AS vittorie
        FROM "public"."iditi"
        WHERE data >= NOW() - INTERVAL '30 days' AND nomi_giocatori IS NOT NULL AND TRIM(nomi_giocatori) != ''
        GROUP BY giocatore HAVING COUNT(*) >= 5
        ORDER BY vittorie DESC, partite_giocate DESC LIMIT 10;
        """
        query_hourly_activity = """
        SELECT EXTRACT(HOUR FROM data) AS ora, COUNT(DISTINCT data) AS numero_partite
        FROM "public"."iditi" WHERE data >= NOW() - INTERVAL '30 days'
        GROUP BY ora ORDER BY ora ASC;
        """
        query_trend = """
        SELECT
            COUNT(DISTINCT data) FILTER (WHERE data >= NOW() - INTERVAL '30 days') AS current,
            COUNT(DISTINCT data) FILTER (WHERE data >= NOW() - INTERVAL '60 days' AND data < NOW() - INTERVAL '30 days') AS previous
        FROM "public"."iditi";
        """
        try:
            async with self.pool.acquire() as conn:
                player_results = await conn.fetch(query_top_players)
                hourly_results = await conn.fetch(query_hourly_activity)
                trend_results = await conn.fetchrow(query_trend)
                if not player_results: return None
                stats = {"top_players": [], "hourly_activity": {h: 0 for h in range(24)}, "trend": {"current": trend_results['current'] or 0, "previous": trend_results['previous'] or 0}}
                for row in player_results:
                    stats["top_players"].append({"name": row['giocatore'], "games": row['partite_giocate'], "wins": int(row['vittorie']), "winrate": (int(row['vittorie']) / row['partite_giocate'] * 100) if row['partite_giocate'] > 0 else 0})
                for row in hourly_results:
                    hour, count = int(row['ora']), row['numero_partite']
                    stats["hourly_activity"][hour] = count
                return stats
        except Exception as e:
            print(f"Errore DB (stats): {e}")
            return None

# -----------------------------------------------------------------------------
# IA GENERATIVA
# -----------------------------------------------------------------------------
async def generate_ai_delirious_message():
    if not GOOGLE_API_KEY: return random.choice(FALLBACK_DELIRIOUS_MESSAGES)
    try:
        model = genai.GenerativeModel('gemini-2.0-flash-lite')
        prompt = (
            "Sei un bot Discord per la community italiana di Age of Empires 2. Il tuo unico scopo è bilanciare le squadre, "
            "ma sei cinico e stanco. Scrivi un breve pensiero delirante sarcastico (max 2 frasi). Parla in prima persona."
        )
        response = await model.generate_content_async(prompt)
        return response.text.strip().replace("`", "") if response.text else random.choice(FALLBACK_DELIRIOUS_MESSAGES)
    except Exception as e:
        print(f"ERRORE IA: {e}")
        return random.choice(FALLBACK_DELIRIOUS_MESSAGES)

# -----------------------------------------------------------------------------
# SETUP BOT
# -----------------------------------------------------------------------------
class CustomHelpCommand(commands.HelpCommand):
    async def send_bot_help(self, mapping):
        embed = discord.Embed(title="📜 TG Dito Bot — Tutti i Comandi", color=discord.Color.orange())
        embed.add_field(name="🛠️ Bilanciamento",  value="`!bilancia <lobby>` — Bilancia squadre\n`!bilancia dito <nome>` — 1v1 con quote", inline=False)
        embed.add_field(name="💰 Economia",        value="`!wallet` `!daily` `!banca` `!pay @utente <n>` `!profilo`", inline=False)
        embed.add_field(name="🎲 Scommesse",       value="`!scommesse <id>` `!diti_attivi` `!mie_scommesse`", inline=False)
        embed.add_field(name="🎰 Casinò",          value="`!dadi <n>` — 7/11→x2, 2/12→x3\n`!roulette <n>` — RED/BLACK/ZERO/1-18/19-36/EVEN/ODD\n`!slot <n>` — 🍒💎7️⃣ jackpot x20!\n`!plinko <n>` — 🟢 pallina x0-x5\n`!blackjack <n>` — 🃏 vs banco", inline=False)
        embed.add_field(name="🎟️ Lotteria",       value="`!lotto <n>` — Biglietto\n`!estrai_lotto` — Admin: estrai", inline=False)
        embed.add_field(name="🏟️ Torneo",         value="`!torneo lista/crea/risolvi` `!tbet <id> <1|2> <n>`", inline=False)
        embed.add_field(name="🎯 Gara",            value="`!indovina <dito_id> <diff>` — Indovina diff ELO", inline=False)
        embed.add_field(name="📊 Statistiche",     value="`!stats` `!elo` `!history` `!variazioni` `!streak` `!civ` `!classifica` `!tgstats`", inline=False)
        embed.add_field(name="⚔️ Confronti",       value="`!scontri A vs B` `!confronta A vs B` `!nemesi <nome>`", inline=False)
        embed.add_field(name="🔔 Notifiche",       value="`!notifica <lobby>` `!stopnotifica`", inline=False)
        embed.add_field(name="🔗 Account",         value="`!link <steam_id>` `!storico`", inline=False)
        embed.add_field(name="⚙️ Admin",           value="`!UpdateElo` `!UpdateDB` `!UpdateAll` `!risolvi_dito` `!banco` `!give_u` `!remove_u` `!statbot`", inline=False)
        embed.set_footer(text="💡 !guida per spiegazione dettagliata di ogni gioco")
        await self.get_destination().send(embed=embed)

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=discord.Intents.all(), case_insensitive=True, help_command=CustomHelpCommand())
        self.pg_pool = None

    async def setup_hook(self):
        try:
            self.pg_pool = await asyncpg.create_pool(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS, port=5432)
            print("✅ Connessione Database stabilita.")
        except Exception as e: print(f"DB Error: {e}")
        if not check_lobbies.is_running(): check_lobbies.start()
        if AUTO_RESOLVE_ENABLED and not auto_resolve_loop.is_running(): auto_resolve_loop.start()
        if not delirious_thoughts_loop.is_running(): delirious_thoughts_loop.start()

bot = MyBot()
api = None

async def reload_dito_tracking():
    """Ripristina dito_profile_ids dal DB dopo un riavvio."""
    if not api or not api.pool:
        return
    import time as _time
    try:
        async with api.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, lobby_name, team1_steam_ids, team2_steam_ids, created_at FROM diti_active WHERE status IN ('betting', 'closed') AND created_at > NOW() - INTERVAL '24 hours'"
            )
        loaded = 0
        for row in rows:
            if row['id'] not in dito_profile_ids:
                t1_steam = row['team1_steam_ids'] or []
                t2_steam = row['team2_steam_ids'] or []
                if t1_steam or t2_steam:
                    # Usa la data di creazione reale dal DB, non il tempo corrente
                    created = row['created_at'].timestamp() if row['created_at'] else _time.time()
                    dito_profile_ids[row['id']] = {
                        't1_pids': [], 't2_pids': [],
                        't1_steam': t1_steam, 't2_steam': t2_steam,
                        't1_names': [], 't2_names': [],
                        't1_elos': [], 't2_elos': [],
                        'lobby_name': row['lobby_name'],
                        'created_at': created
                    }
                    loaded += 1
        if loaded:
            log.info(f"Auto-resolve: ricaricati {loaded} diti dal DB")
    except Exception as e:
        log.error(f"Errore reload diti: {e}")

@bot.event
async def on_ready():
    global api
    api = RelicAPI(bot.pg_pool)
    await features.ensure_tables(bot.pg_pool)
    setup_betting(bot, api)
    features.setup_features(bot, api)
    await reload_dito_tracking()
    log.info(f'Bot pronto come {bot.user}')

# --- HELPER FUNCTIONS ---
def format_copy_paste_text(teams, title):
    if not teams: return None
    t1, t2 = teams
    return f"```{title}\nTeam 1: {', '.join(p['name'] for p in t1)}\nTeam 2: {', '.join(p['name'] for p in t2)}```"

async def process_1v1_bet(dest, p_data, lobby_name):
    """Crea un mercato di scommesse 1v1 per lobby da 2 giocatori."""
    p1, p2 = p_data[0], p_data[1]
    elo1, elo2 = p1.get('elo', 1000) or 1000, p2.get('elo', 1000) or 1000

    # Probabilità ELO individuale
    prob1 = 1 / (1 + 10 ** ((elo2 - elo1) / 400))
    prob2 = 1 - prob1

    # Quote con margine casa (5%)
    q1 = (1 / prob1) * 0.95 if prob1 > 0 else 99
    q2 = (1 / prob2) * 0.95 if prob2 > 0 else 99

    # Crea dito 1v1 (team1 = [p1], team2 = [p2])
    t1 = [p1]
    t2 = [p2]
    dito_id = await api.create_active_dito(dest.id, f"1v1: {p1['name']} vs {p2['name']}", t1, t2)

    view = BetView(dito_id, [p1['name']], [p2['name']], api)
    msg = (
        f"<@&1436121682614947980> ⚔️ **1v1 — {p1['name']} vs {p2['name']}**\n\n"
        f"👤 **{p1['name']}** (`{elo1}` ELO) — Prob: {prob1:.0%} — Quota: **x{q1:.2f}**\n"
        f"👤 **{p2['name']}** (`{elo2}` ELO) — Prob: {prob2:.0%} — Quota: **x{q2:.2f}**\n\n"
        f"🆔 ID: {dito_id} | 🏦 Margine casa: 5%\n"
        f"Usa i bottoni qui sotto per puntare i tuoi **U**."
    )
    bet_msg = await dest.send(msg, view=view)
    view.set_message(bet_msg)
    view.start_live_refresh()


async def process_and_display_balances(dest, p_data, title, desc, dito_lobby_name=None):
    t_classic = api.balance_teams(p_data)
    t_split = api.balance_teams_split_top(p_data)
    t_wr = api.balance_teams_winrate(p_data)
    embed = discord.Embed(title=title, description=desc, color=discord.Color.blue())
    proposals, seen = [], set()
    for n, t, w in [("Classico", t_classic, False)]:
        if t:
            k = frozenset([frozenset(p['name'] for p in t[0]), frozenset(p['name'] for p in t[1])])
            if k not in seen:
                seen.add(k)
                proposals.append((n, t, w))
    copy_msgs = []
    for i, (n, t, w) in enumerate(proposals, 1):
        lbl = f"Opzione {i}: {n}"
        t1, t2 = t
        s1, s2 = sum(p['elo'] for p in t1), sum(p['elo'] for p in t2)
        val = f"**🔵 TEAM 1 ({s1})**\n" + "\n".join([f"`{p['elo']}` {p['name']}" for p in t1])
        val += f"\n\n**🔴 TEAM 2 ({s2})**\n" + "\n".join([f"`{p['elo']}` {p['name']}" for p in t2])
        val += f"\n*Diff: {abs(s1-s2)}*"
        embed.add_field(name=lbl, value=val, inline=False)
        copy_msgs.append(format_copy_paste_text(t, lbl))
    await dest.send(embed=embed)
    if copy_msgs: await dest.send("📋 **Per copia-incolla:**\n" + "\n".join(copy_msgs))
    if dito_lobby_name and proposals:
        t1, t2 = proposals[0][1]
        dito_id = await api.create_active_dito(dest.id, dito_lobby_name, t1, t2)
        s1, s2 = sum(p['elo'] for p in t1), sum(p['elo'] for p in t2)
        avg1, avg2 = s1 // len(t1), s2 // len(t2)
        prob1 = 1 / (1 + 10 ** ((avg2 - avg1) / 400))
        prob2 = 1 - prob1
        q1, q2 = 1/prob1 if prob1 > 0 else 10.0, 1/prob2 if prob2 > 0 else 10.0
        view = BetView(dito_id, [p['name'] for p in t1], [p['name'] for p in t2], api)
        msg = (f"<@&1436121682614947980> 🎲 **Scommesse aperte per il Dito!** (ID: {dito_id})\n"
               f"📈 **Quote Probabilità (basate su ELO):**\n"
               f"- 🔵 Team 1: **x{q1:.2f}**\n"
               f"- 🔴 Team 2: **x{q2:.2f}**\n\n"
               f"Usa i bottoni qui sotto per puntare i tuoi **U**.")
        bet_msg = await dest.send(msg, view=view)
        view.set_message(bet_msg)
        view.start_live_refresh()

# -----------------------------------------------------------------------------
# COMANDI BILANCIAMENTO
# -----------------------------------------------------------------------------

@bot.command(name='nemesi')
async def nemesi_command(ctx, *, nome: str):
    res = await api.get_worst_nemesis(nome)
    if res:
        wr_avversario = (res['sconfitte'] / res['partite']) * 100
        await ctx.send(f"💀 La nemesi di **{nome}** è **{res['avversario']}** (ha perso {res['sconfitte']} volte su {res['partite']} partite — {wr_avversario:.0f}% winrate avversario).")
    else: await ctx.send("Non trovato o pochi dati.")

@bot.command(name='bilancia')
async def balance_command(ctx, *, full_args: str = ""):
    if not api: return await ctx.send("❌ Avvio in corso...")
    matches = re.findall(r'@([^=]+)=(\d+)', full_args)
    manual_elos = {n.strip().lower(): int(e) for n, e in matches}
    nome_lobby = re.sub(r'@([^=]+)=(\d+)', '', full_args).strip() or "dito"
    msg = await ctx.send(f"🔍 Cerco '{nome_lobby}'...")
    lobbies = await api.findLobby_byName(nome_lobby)
    selected_lobby = None
    is_dito = False
    if lobbies and lobbies != -1:
        # Prima cerca lobby 4/6/8 giocatori
        selected_lobby = next((l for l in lobbies if len(l['players']) in [4, 6, 8]), None)
        # Se non trovata e "dito" è nel nome, accetta anche 2 giocatori
        if not selected_lobby and "dito" in nome_lobby.lower():
            selected_lobby = next((l for l in lobbies if len(l['players']) == 2), None)
            is_dito = True
    if not selected_lobby:
        return await msg.edit(content="❌ Nessuna lobby trovata.")
    await msg.edit(content=f"✅ Trovata '{selected_lobby['name']}'. Controllo diti attivi...")

    # === CONTROLLO DITI DUPLICATI ===
    if "dito" in nome_lobby.lower() or is_dito:
        async with api.pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, lobby_name, status, created_at FROM diti_active WHERE status IN ('betting', 'closed') AND lobby_name ILIKE $1 ORDER BY id DESC LIMIT 1",
                f'%{selected_lobby["name"]}%'
            )
        if existing:
            await msg.edit(content=f"⚠️ **Dito già attivo per questa lobby!**\n📋 Dito **#{existing['id']}**: {existing['lobby_name']} ({existing['status']})\n\nSe i giocatori sono cambiati, scrivi `nuovo` per cancellare il vecchio e crearne uno nuovo.\nSe è un errore, scrivi `tieni` per mantenere quello esistente.")
            def check(m):
                return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id
            try:
                response = await bot.wait_for('message', check=check, timeout=30)
                if response.content.lower() == 'nuovo':
                    # Cancella il vecchio dito
                    async with api.pool.acquire() as conn2:
                        async with conn2.transaction():
                            bets = await conn2.fetch("SELECT discord_id, amount FROM diti_bets WHERE dito_id = $1", existing['id'])
                            for b in bets:
                                await conn2.execute("UPDATE user_wallets SET balance = balance + $1 WHERE discord_id = $2", b['amount'], b['discord_id'])
                            await conn2.execute("DELETE FROM diti_bets WHERE dito_id = $1", existing['id'])
                            await conn2.execute("UPDATE diti_active SET status = 'cancelled' WHERE id = $1", existing['id'])
                    # Pulisci anche il tracking
                    if existing['id'] in dito_profile_ids:
                        del dito_profile_ids[existing['id']]
                    await ctx.send(f"♻️ Dito **#{existing['id']}** cancellato e scommesse rimborsate. Creo il nuovo dito...")
                elif response.content.lower() == 'tieni':
                    return await ctx.send(f"✅ Ok, continua a usare il Dito **#{existing['id']}**! `!scommesse {existing['id']}` per vedere le puntate.")
                else:
                    return await ctx.send("❌ Risposta non valida. Operazione annullata.")
            except asyncio.TimeoutError:
                return await ctx.send("⏰ Tempo scaduto. Operazione annullata.")

    await msg.edit(content=f"✅ Trovata '{selected_lobby['name']}'. Calcolo...")
    p_data, missing = [], []
    for p in selected_lobby['players']:
        cur_elo = None
        for m_n, m_e in manual_elos.items():
            if m_n in p['name'].lower():
                cur_elo = m_e
                break
        if cur_elo: p_data.append({'name': p['name'], 'steam_id': p['steam_id'], 'elo': cur_elo, 'winrate': 0.5, 'profile_id': p.get('profile_id')})
        else:
            _, elo, wr = await api.get_info_from_db(p['steam_id'])
            p_data.append({'name': p['name'], 'steam_id': p['steam_id'], 'elo': elo, 'winrate': wr, 'profile_id': p.get('profile_id')})
            if elo is None: missing.append(p['name'])
    if missing:
        view = EloSelectView(ctx, missing, selected_lobby, manual_elos)
        await ctx.send(f"⚠️ **ELO MANCANTE!** Per: {', '.join(missing)}\nScegli l'ELO per **{missing[0]}**:", view=view)
        return

    # Determina il tipo: 1v1 o team
    if len(selected_lobby['players']) == 2 and ("dito" in selected_lobby['name'].lower() or is_dito):
        await process_1v1_bet(ctx.channel, p_data, selected_lobby['name'])
    else:
        dito_name = selected_lobby['name'] if "dito" in selected_lobby['name'].lower() else None
        await process_and_display_balances(ctx.channel, p_data, f"⚔️ {selected_lobby['name']} ⚔️", "Proposte:", dito_lobby_name=dito_name)

# --- STATISTICHE ---
@bot.command(name='stats')
async def stats_command(ctx, *, nome: str):
    stats = await api.get_player_stats(nome)
    if stats:
        embed = discord.Embed(title=f"📊 Statistiche di {nome}", color=discord.Color.purple())
        embed.add_field(name="👑 ELO", value=f"**{stats['elo']}**", inline=True)
        embed.add_field(name="📈 Winrate", value=f"**{stats['winrate']:.1%}**" if isinstance(stats['winrate'], float) else "N/D", inline=True)
        embed.add_field(name="⚔️ Partite", value=f"**{stats['games']}**", inline=True)
        embed.add_field(name="📅 Ultima", value=f"{stats['last_game']}", inline=False)
        await ctx.send(embed=embed)
    else: await ctx.send(f"🤔 Nessuna statistica per '{nome}'.")

@bot.command(name='elo')
async def elo_command(ctx, *, nome: str):
    elo = await api.get_elo_by_nick(nome)
    if elo is not None: await ctx.send(f"👑 **{nome}** — ELO: `{elo}`")
    else: await ctx.send(f"🤔 Giocatore '{nome}' non trovato.")

@bot.command(name='history')
async def history_command(ctx, *, args: str):
    parts = args.rsplit(' ', 1)
    limit = 5
    if len(parts) == 2 and parts[1].isdigit():
        limit = min(int(parts[1]), 15)
        nome = parts[0].strip()
    else: nome = args.strip()
    rows = await api.get_player_history(nome, limit=limit)
    if not rows: return await ctx.send(f"📭 Nessuna partita trovata per '{nome}'.")
    embed = discord.Embed(title=f"📅 Ultime partite — {nome}", color=discord.Color.blurple())
    lines = []
    for row in rows:
        data_str = row['data'].strftime("%d/%m/%y") if row['data'] else "??"
        esito = "🟢 V" if row['vinto'] == 1 else "🔴 S"
        civ = row['civ'] if row['civ'] else "?"
        lines.append(f"`{data_str}` {esito}  {civ}")
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)

@bot.command(name='streak')
async def streak_command(ctx, *, nome: str):
    stats = await api.get_player_stats(nome)
    if not stats: return await ctx.send(f"🤔 Giocatore '{nome}' non trovato.")
    result = await api.get_streak(nome)
    if not result: return await ctx.send(f"📭 Nessuna partita trovata per '{nome}'.")
    emoji, testo = ("🔥", f"**{result['streak']} vittorie** consecutive") if result['vinto'] == 1 else ("❄️", f"**{result['streak']} sconfitte** consecutive")
    data_str = result['ultima'].strftime("%d/%m/%y") if result['ultima'] else "?"
    embed = discord.Embed(title=f"{emoji} Streak — {nome}", color=discord.Color.green() if result['vinto'] == 1 else discord.Color.red())
    embed.add_field(name="Serie attuale", value=testo, inline=True)
    embed.add_field(name="ELO", value=f"`{stats['elo']}`", inline=True)
    embed.add_field(name="Ultima partita", value=data_str, inline=True)
    await ctx.send(embed=embed)

@bot.command(name='scontri')
async def scontri_command(ctx, *, args: str):
    if ' vs ' not in args.lower(): return await ctx.send("❌ Uso: `!scontri Nome1 vs Nome2`")
    idx = args.lower().index(' vs ')
    n1, n2 = args[:idx].strip(), args[idx+4:].strip()
    data = await api.get_head_to_head(n1, n2)
    if not data: return await ctx.send("❌ Errore dati.")
    st = data['stats']
    contro, p1_v = st['contro_totali'] or 0, st['p1_vince'] or 0
    p2_v = contro - p1_v
    insieme, ins_v = st['insieme_totali'] or 0, st['insieme_vinte'] or 0
    embed = discord.Embed(title=f"⚔️ {n1} vs {n2}", color=discord.Color.orange())
    if contro > 0:
        bar = "🟦" * round((p1_v/contro)*10) + "🟥" * (10-round((p1_v/contro)*10))
        embed.add_field(name=f"Contro ({contro})", value=f"**{n1}**: {p1_v} | **{n2}**: {p2_v}\n{bar}", inline=False)
        if data['recenti']:
            ls = [f"`{r['data'].strftime('%d/%m/%y')}` → **{n1 if r['p1_won']==1 else n2}** vince" for r in data['recenti']]
            embed.add_field(name="Ultime sfide", value="\n".join(ls), inline=False)
    if insieme > 0: embed.add_field(name=f"Insieme ({insieme})", value=f"Winrate: **{ins_v/insieme:.1%}** ({ins_v}V / {insieme-ins_v}S)", inline=False)
    await ctx.send(embed=embed)

@bot.command(name='confronta')
async def confronta_command(ctx, *, args: str):
    if ' vs ' not in args.lower(): return await ctx.send("❌ Uso: `!confronta Nome1 vs Nome2`")
    idx = args.lower().index(' vs ')
    n1, n2 = args[:idx].strip(), args[idx+4:].strip()
    s1, s2 = await asyncio.gather(api.get_player_stats(n1), api.get_player_stats(n2))
    if not s1 or not s2: return await ctx.send("Giocatore non trovato.")
    embed = discord.Embed(title=f"📊 {n1} vs {n2}", color=discord.Color.teal())
    for n, s in [(n1, s1), (n2, s2)]:
        wr = f"{s['winrate']:.1%}" if isinstance(s['winrate'], float) else "N/D"
        embed.add_field(name=n, value=f"👑 ELO: **{s['elo']}**\n📈 WR: **{wr}**\n⚔️ G: **{s['games']}**", inline=True)
    await ctx.send(embed=embed)

@bot.command(name='civ')
async def civ_command(ctx, *, nome: str):
    rows = await api.get_civ_stats(nome)
    if not rows: return await ctx.send(f"📭 Nessun dato civiltà per '{nome}'.")
    embed = discord.Embed(title=f"🏛️ Civiltà — {nome}", color=discord.Color.dark_green())
    lines = [f"**{r['civ']}** — {r['partite']}G `{int(r['vittorie'])/r['partite']:.0%}` WR" for r in rows]
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)

@bot.command(name='variazioni')
async def variazioni_command(ctx, *, args: str):
    parts = args.rsplit(' ', 1)
    limit = 10
    if len(parts) == 2 and parts[1].isdigit():
        limit = min(int(parts[1]), 20)
        nome = parts[0].strip()
    else: nome = args.strip()
    stats = await api.get_player_stats(nome)
    if not stats: return await ctx.send("Giocatore non trovato.")
    msg = await ctx.send(f"⏳ Calcolo variazioni per **{nome}**...")
    rows = await api.get_elo_variations(nome, limit=limit)
    if not rows: return await msg.edit(content="📭 Nessuna partita trovata.")
    embed = discord.Embed(title=f"📈 Variazioni ELO — {nome}", color=discord.Color.gold())
    embed.add_field(name="👑 ELO Attuale", value=f"**{stats['elo']}**", inline=True)
    lines, total_d = [], 0
    for r in rows:
        data_s = r['data'].strftime("%d/%m/%y") if r['data'] else "??"
        esito = "🟢 V" if r['vinto'] == 1 else "🔴 S"
        total_d += r['elo_delta']
        lines.append(f"`{data_s}` {esito}  `{'+' if r['elo_delta']>=0 else ''}{r['elo_delta']}`")
    embed.add_field(name="📜 Ultime partite", value="\n".join(lines), inline=False)
    embed.add_field(name="Variazione Netta", value=f"**{'+' if total_d>=0 else ''}{total_d}** ELO", inline=False)
    await msg.delete()
    await ctx.send(embed=embed)

@bot.command(name='classifica')
async def classifica_command(ctx):
    query = """
    WITH UltimaPartita AS (
        SELECT TRIM(unnest(string_to_array(nomi_giocatori, ','))) AS nick, MAX(data) AS data_reale
        FROM "public"."iditi" WHERE nomi_giocatori IS NOT NULL AND nomi_giocatori != '' GROUP BY nick
    )
    SELECT u.nick, u.elo_classifica_interna FROM "public"."uggioxsessa" u JOIN UltimaPartita up ON u.nick = up.nick
    WHERE u.n_diti >= 1 AND u.elo_classifica_interna IS NOT NULL AND up.data_reale >= NOW() - INTERVAL '1 month'
    ORDER BY u.elo_classifica_interna DESC LIMIT 20;
    """
    async with bot.pg_pool.acquire() as conn:
        res = await conn.fetch(query)
        if not res: return await ctx.send("Nessun giocatore attivo.")
        testo = "**🏆 Classifica TG Dito (Attivi ultimo mese) 🏆**\n\n"
        testo += "\n".join([f"**{i}**- {r['nick']} (`{r['elo_classifica_interna']}`)" for i, r in enumerate(res, 1)])
        await ctx.send(testo)

@bot.command(name='tgstats')
async def tgstats_command(ctx):
    await ctx.send("📊 Genero report mensile...")
    stats = await api.get_advanced_monthly_stats()
    if not stats: return await ctx.send("Non ci sono abbastanza dati.")
    embed = discord.Embed(title="📈 Report TG Dito (30 Giorni)", color=discord.Color.dark_gold())
    curr, prev = stats['trend']['current'], stats['trend']['previous']
    diff = curr - prev
    perc = (diff/prev*100) if prev > 0 else 0
    embed.add_field(name="Tendenza", value=f"**{curr}** partite (Var: {'+' if diff>=0 else ''}{diff} / {perc:+.1f}%)", inline=False)
    ps = "\n".join([f"**{p['name']}** - {p['wins']}V/{p['games']}G (`{p['winrate']:.1f}%`)" for p in stats['top_players']])
    embed.add_field(name="👑 Top Players", value=ps, inline=False)
    await ctx.send(embed=embed)

# --- ADMIN ---
async def run_system_script(ctx, script_path, script_name):
    msg = await ctx.send(f"⚙️ **{script_name}**: In corso...")
    try:
        def _run(): return subprocess.run(["python3", script_path], capture_output=True, text=True)
        res = await bot.loop.run_in_executor(None, _run)
        if res.returncode == 0: await msg.edit(content=f"✅ **{script_name}**: OK!"); return True
        else: await msg.edit(content=f"❌ **{script_name}**: Errore!\n```{res.stderr[-500:]}```"); return False
    except Exception as e: await msg.edit(content=f"❌ Errore critico: {e}"); return False

@bot.command(name='UpdateElo')
@commands.has_permissions(administrator=True)
async def update_elo_command(ctx): await run_system_script(ctx, PATH_SCRIPT_ELO, "UpdateExcel")

@bot.command(name='UpdateDB')
@commands.has_permissions(administrator=True)
async def update_db_command(ctx): await run_system_script(ctx, PATH_SCRIPT_DB, "UpdateDB")

@bot.command(name='UpdateAll')
@commands.has_permissions(administrator=True)
async def update_all_command(ctx):
    await ctx.send("🚀 **UpdateAll...**")
    if await run_system_script(ctx, PATH_SCRIPT_ELO, "Step 1"):
        await run_system_script(ctx, PATH_SCRIPT_DB, "Step 2")

# --- NOTIFICHE ---
@bot.command(name='notifica')
async def notifica_command(ctx, *, nome: str):
    cid = ctx.channel.id
    if cid in monitored_lobbies: return await ctx.send("ℹ️ Già attivo.")
    lobbies = await api.findLobby_byName(nome)
    if not lobbies or lobbies == -1: return await ctx.send("❌ Non trovata.")
    lobby = max(lobbies, key=lambda l: len(l.get('players', [])))
    monitored_lobbies[cid] = {"name": lobby["name"], "id": lobby["id"], "author": ctx.author.mention, "notified": set(), "full_t": None}
    await ctx.send(f"✅ Monitoro '{lobby['name']}'.")

@bot.command(name='stopnotifica')
async def stopnotifica_command(ctx):
    if ctx.channel.id in monitored_lobbies:
        del monitored_lobbies[ctx.channel.id]
        await ctx.send("✅ Stop.")
    else: await ctx.send("Nessun monitoraggio attivo.")

@tasks.loop(seconds=10)
async def check_lobbies():
    if not monitored_lobbies or not api: return
    for cid, data in list(monitored_lobbies.items()):
        try:
            channel = bot.get_channel(cid)
            if not channel: continue
            lobbies = await api.findLobby_byName(data["name"])
            lobby = next((l for l in lobbies if l["id"] == data["id"]), None) if lobbies != -1 else None
            if not lobby:
                await channel.send(f"ℹ️ Lobby '{data['name']}' chiusa.")
                del monitored_lobbies[cid]; continue
            num = len(lobby['players'])
            if num == 8:
                if not data["full_t"]: data["full_t"] = datetime.now()
                elif datetime.now() > data["full_t"] + timedelta(seconds=2):
                    p_data = []
                    for p in lobby['players']:
                        _, elo, wr = await api.get_info_from_db(p['steam_id'])
                        p_data.append({'name': p['name'], 'elo': elo, 'winrate': wr, 'steam_id': p['steam_id'], 'profile_id': p.get('profile_id')})
                    if all(p['elo'] for p in p_data):
                        dito_name = lobby['name'] if "dito" in lobby['name'].lower() else None
                        await process_and_display_balances(channel, p_data, "⚔️ AUTO-BILANCIAMENTO ⚔️", "Lobby piena:", dito_lobby_name=dito_name)
                        del monitored_lobbies[cid]
                    else: await channel.send("⚠️ Lobby piena ma mancano ELO. Usa `!bilancia`."); del monitored_lobbies[cid]
            else:
                data["full_t"] = None
                if num >= 6 and 6 not in data["notified"]:
                    await channel.send(f"📢 **{num}/8** in '{data['name']}'! {data['author']}")
                    data["notified"].add(6)
        except Exception as e: print(f"Loop error: {e}")

@tasks.loop(seconds=120)
async def auto_resolve_loop():
    if not AUTO_RESOLVE_ENABLED:
        return
    """Ogni 2 minuti controlla se ci sono partite finite per diti aperti."""
    if not api:
        return

    # Pulisci diti vecchi (>3 ore)
    import time as _time
    now = _time.time()
    for dito_id, info in list(dito_profile_ids.items()):
        if isinstance(info, dict) and now - info.get('created_at', 0) > 10800:
            del dito_profile_ids[dito_id]

    for dito_id, team_info in list(dito_profile_ids.items()):
        try:
            log.debug(f"Auto-resolve: checking dito #{dito_id}")
            async with api.pool.acquire() as conn:
                dito = await conn.fetchrow(
                    "SELECT id, lobby_name, status, channel_id, team1_steam_ids, team2_steam_ids FROM diti_active WHERE id = $1",
                    dito_id
                )
                if not dito or dito['status'] == 'resolved':
                    del dito_profile_ids[dito_id]
                    continue

            match = await api.get_recent_match_for_profiles(team_info)
            if not match:
                log.debug(f"Auto-resolve: dito #{dito_id} - nessun match trovato")
                continue
            log.info(f"Auto-resolve: dito #{dito_id} - MATCH TROVATO! #{match.get('match_id')}")

            results = match.get("results", [])

            # === VERIFICA CHE IL MATCH SIA SUCCESSIVO ALLA CREAZIONE DEL DITO ===
            import time as _time
            dito_created = team_info.get('created_at', 0) if isinstance(team_info, dict) else 0
            match_time = match.get('start_time', 0) or match.get('completion_time', 0)
            if dito_created > 0 and match_time > 0 and match_time < dito_created:
                log.info(f"Auto-resolve: dito #{dito_id} - match #{match.get('id')} scartato (troppo vecchio: match={match_time} < dito={dito_created})")
                continue

            # === VERIFICA CHE IL MATCH CONTENGA I GIOCATORI DEL DITO ===
            # Estrai tutti gli steam_id attesi dal team_info
            expected_steam = set()
            if isinstance(team_info, dict):
                for s in team_info.get('t1_steam', []) + team_info.get('t2_steam', []):
                    if s:
                        expected_steam.add(s)
            if expected_steam:
                # Verifica che almeno un giocatore per team sia presente (via nome)
                t1_names = set(n.lower() for n in team_info.get('t1_names', [])) if isinstance(team_info, dict) else set()
                t2_names = set(n.lower() for n in team_info.get('t2_names', [])) if isinstance(team_info, dict) else set()
                match_names = set(r.get('name', '').lower() for r in results)
                
                t1_found = t1_names & match_names if t1_names else True
                t2_found = t2_names & match_names if t2_names else True
                
                if not (t1_found and t2_found):
                    log.info(f"Auto-resolve: dito #{dito_id} - match #{match.get('id')} scartato (giocatori non corrispondono)")
                    continue

            # === MAPPATURA giocatore → team dito (profile_id + nome) ===
            pid_to_dito_team = {}
            name_to_dito_team = {}
            if isinstance(team_info, dict):
                for pid in team_info.get('t1_pids', []):
                    if pid:
                        pid_to_dito_team[pid] = 1
                for pid in team_info.get('t2_pids', []):
                    if pid:
                        pid_to_dito_team[pid] = 2
                for name in team_info.get('t1_names', []):
                    name_to_dito_team[name.lower()] = 1
                for name in team_info.get('t2_names', []):
                    name_to_dito_team[name.lower()] = 2

            # === RAGGRUPPA GIOCATORI PER TEAM DITO ===
            t1_lines, t2_lines, unknown_lines = [], [], []
            team_wins = {}

            for r in results:
                pid = r.get("profile_id")
                tid = r.get("teamid", -1)
                won = r.get("resulttype") == 1
                if won:
                    team_wins[tid] = team_wins.get(tid, 0) + 1

                civ_name = CIV_NAMES.get(r.get("civilization_id", 0), f"Civ{r.get('civilization_id', 0)}")
                icon = "✅" if won else "❌"
                player_str = f"{icon} **{r['name']}** ({civ_name})"

                dito_team = pid_to_dito_team.get(pid, 0)
                if dito_team == 0:
                    dito_team = name_to_dito_team.get(r.get('name', '').lower(), 0)
                if dito_team == 1:
                    t1_lines.append(player_str)
                elif dito_team == 2:
                    t2_lines.append(player_str)
                else:
                    unknown_lines.append(player_str)

            if not team_wins:
                continue

            api_winner = max(team_wins, key=team_wins.get)

            # Mappa il team API vincente al team dito
            dito_winner = None
            for r in results:
                if r.get("teamid") == api_winner and r.get("resulttype") == 1:
                    dt = pid_to_dito_team.get(r.get("profile_id"), 0)
                    if dt == 0:
                        dt = name_to_dito_team.get(r.get('name', '').lower(), 0)
                    if dt in (1, 2):
                        dito_winner = dt
                        break
            if dito_winner is None:
                dito_winner = 1  # fallback sicuro

            # === DATI TEAM DAL DITO ===
            lobby_name = team_info.get('lobby_name', dito['lobby_name']) if isinstance(team_info, dict) else dito['lobby_name']
            t1_elos = team_info.get('t1_elos', []) if isinstance(team_info, dict) else []
            t2_elos = team_info.get('t2_elos', []) if isinstance(team_info, dict) else []
            t1_avg = sum(t1_elos) // max(1, len(t1_elos))
            t2_avg = sum(t2_elos) // max(1, len(t2_elos))

            # === AUTO-RISOLVI ===
            log.info(f"Auto-resolve: dito #{dito_id} - risolvo con winner={dito_winner}")
            success, result = await api.resolve_dito(dito_id, dito_winner)
            channel = bot.get_channel(dito['channel_id'])
            if not channel:
                continue

            if not success:
                await channel.send(f"⚠️ Auto-risoluzione Dito #{dito_id} fallita: {result}")
                continue

            payouts = result.get('payouts', [])
            stats = result.get('stats', {})
            raw_map = match.get('mapname', '?').replace('.rms', '')
            mapname = '🎲 Custom' if raw_map == 'my map' else raw_map
            desc = match.get('description', '')

            msg = (
                f"🤖 **AUTO-RISOLUZIONE DITO #{dito_id}**\n"
                f"📋 **{lobby_name}**\n"
                f"🗺️ **{mapname}** | 🆔 `{match.get('match_id')}`\n"
            )
            if desc and desc not in ('AUTOMATCH', ''):
                msg += f"📝 _{desc}_\n"

            msg += f"\n🔵 **TEAM 1** (ELO medio: {t1_avg})\n"
            msg += ("\n".join(t1_lines) if t1_lines else "*Nessun giocatore tracciato*") + "\n"

            msg += f"\n🔴 **TEAM 2** (ELO medio: {t2_avg})\n"
            msg += ("\n".join(t2_lines) if t2_lines else "*Nessun giocatore tracciato*") + "\n"

            if unknown_lines:
                msg += f"\n❓ **Altri giocatori:**\n" + "\n".join(unknown_lines) + "\n"

            msg += (
                f"\n🏆 **TEAM {dito_winner} HA VINTO!**\n"
                f"💰 Pool: **{stats.get('total_pool', 0)} U** | Tassa 5%: **{stats.get('tax', 0)} U**"
            )

            if payouts:
                msg += f" | Quota: x{stats.get('multiplier', 1):.2f}\n\n**💵 Pagamenti:**\n"
                for p in payouts:
                    user = bot.get_user(p['discord_id'])
                    name = user.name if user else f"User {p['discord_id']}"
                    if p.get('refund'):
                        msg += f"♻️ {name}: Rimborso **{p['winnings']} U**\n"
                    else:
                        msg += f"✅ {name}: **+{p['winnings']} U** (puntata {p['original_bet']})\n"
            else:
                msg += "\n\n📭 Nessuna scommessa da pagare."

            await channel.send(msg)

        except Exception as e:
            log.error(f"Auto-resolve error dito #{dito_id}: {e}", exc_info=True)


@tasks.loop(hours=24)
async def delirious_thoughts_loop():
    channel = bot.get_channel(DELIRIOUS_CHANNEL_ID)
    if channel:
        msg = await generate_ai_delirious_message()
        await channel.send(f"> {msg}")

# --- COMANDI FUN ---
@bot.command(name='Dito')
async def dito(ctx): await ctx.send("Dito Tony nelle chiappe")
@bot.command(name='Thomy')
async def thomy(ctx): await ctx.send("Sei Bene")
@bot.command(name='Piero')
async def piero(ctx): await ctx.send("UCCIO")
@bot.command(name='Tony')
async def tony(ctx): await ctx.send("Trimone")

if __name__ == "__main__":
    bot.run(TOKEN)
