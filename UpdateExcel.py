import time
import pandas as pd
import numpy as np
import sys
#import ROOT



#from flask import Flask, request
from dotenv import load_dotenv
import gspread
from os import getenv
from google.oauth2.service_account import Credentials

import matplotlib.pyplot as plt

# --- IMPOSTAZIONI DELLO SCRIPT ---
PLOT = False
# Impostato a True per avere un output dettagliato durante l'esecuzione
# Si può attivare anche passando --debug come argomento
DEBUG = "--debug" in sys.argv or True
# Se True, ricalcola tutti gli ELO partendo da "Elo Tornei".
# Se False, parte dagli ultimi ELO calcolati e aggiorna solo i nuovi games.
START_FROM_SCRATCH = True
ELO_K = 30. # Fattore K per il calcolo dell'ELO

# Si ferma dopo aver analizzato un tot di righe del foglio "iDiti", utile per testare
# Imposta a -1 per analizzare tutto il foglio
stop_analysis = -1
#app = Flask(__name__)

load_dotenv()
# Autenticazione con Google API
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]

try:
    credentials = Credentials.from_service_account_file(
        getenv("CREDENTIALS_PATH"), scopes=SCOPES
    )
    gc = gspread.authorize(credentials)
    print("✅ Autenticazione con Google Sheets avvenuta con successo.")
except Exception as e:
    print(f"❌ Errore durante l'autenticazione con Google: {e}")
    exit()


# Accede al file di Google Sheets (con sistema di retry)
for i in range(5):
    try:
        sheet_classifica_interna = gc.open("prova_bot_1")
        print(f"✅ Accesso al foglio Google '{sheet_classifica_interna.title}' riuscito.")
        break
    except Exception as e:
        if i < 4:
            print(f"⚠️ Errore Google API (probabile 503). Riprovo tra 10 secondi... ({i+1}/5)")
            import time
            time.sleep(10)
        else:
            print(f"❌ Errore critico dopo 5 tentativi: {e}")
            exit()

#@app.route('/read_sheet', methods=['GET'])
def read_sheet(worksheet, selected_headers=None):
    """Legge un worksheet e lo restituisce come DataFrame pandas."""
    try:
        raw_data = worksheet.get_all_values()
        all_headers = raw_data[0]
        if selected_headers is None:
            seen = set()
            headers = [h if h and h not in seen and not seen.add(h) else None for h in all_headers]
        else:
            headers = selected_headers
        data_rows = raw_data[1:]
        data = pd.DataFrame(data_rows, columns=headers)
        data = data.loc[:, data.columns.notna()]
        print(f"  -> Letto il worksheet '{worksheet.title}' con {len(data)} righe.")
        return data
    except Exception as e:
        print(f"❌ Errore durante la lettura del worksheet '{worksheet.title}': {e}")
        return pd.DataFrame()


def write_sheet(worksheet, dataframe):
    """Scrive un DataFrame pandas su un worksheet di Google Sheets."""
    try:
        print(f"\n📝 Inizio scrittura su worksheet '{worksheet.title}'...")
        # Correzione per gestire i tipi di dati di NumPy (come int64)
        df_cleaned = dataframe.astype(object).where(pd.notna(dataframe), None)
        data_to_write = [df_cleaned.columns.tolist()] + df_cleaned.values.tolist()
        worksheet.clear()
        worksheet.update(range_name="A1", values=data_to_write)
        print(f"✅ Scrittura completata con successo su '{worksheet.title}'.")
    except Exception as e:
        print(f"❌ Errore durante la scrittura sul worksheet '{worksheet.title}': {e}")


# --- LETTURA DEI DATI ---
print("\n--- Inizio lettura dati dai worksheet ---")
elo_df = read_sheet(sheet_classifica_interna.worksheet("PersoneDitate"))
persone_df = read_sheet(sheet_classifica_interna.worksheet("Persone"))
diti_df = read_sheet(sheet_classifica_interna.worksheet("iDiti"))
print("--- Lettura dati completata ---\n")


# --- CONTROLLO DI SICUREZZA ---
if elo_df.empty:
    print("❌ ERRORE CRITICO: Il worksheet 'PersoneDitate' è vuoto o inaccessibile.")
    print("▶️ SOLUZIONE: Assicurati di aver condiviso il Google Sheet con l'email del service account (con permessi da 'Editor').")
    exit()

# --- PREPARAZIONE DATI INIZIALI ---
if START_FROM_SCRATCH:
    print("▶️ Modalità START_FROM_SCRATCH: gli ELO verranno ricalcolati da zero partendo da 'Elo Tornei'.")
    starting_elo = np.array(elo_df["Elo Tornei"].str.replace(',', '.').replace('', '0'), dtype='float')
else:
    print("▶️ Modalità aggiornamento: gli ELO verranno aggiornati partendo da 'Elo Classifica Interna'.")
    starting_elo = np.array(elo_df["Elo Classifica Interna"].str.replace(',', '.').replace('', '0'), dtype='float')

updated_elo = starting_elo.copy() # Usiamo .copy() per non modificare l'array originale
nicks = np.array(elo_df["Nick"], dtype='str')

last_game_analyzed = -1
if not START_FROM_SCRATCH:
    # Assicuriamoci che la colonna esista e non sia vuota
    if "Last Game ID Checked" in elo_df.columns and not elo_df["Last Game ID Checked"].iloc[0] == '':
        last_game_analyzed = np.array(elo_df["Last Game ID Checked"].replace('', '-1'), dtype='int')[0]
print(f"ℹ️ Ultimo ID game già analizzato: {last_game_analyzed}\n")


# Conversione delle colonne di 'diti_df' in array numpy per maggiore efficienza
diti_nick_players = np.array(diti_df["Nomi Giocatori"],dtype='str')
diti_ids_games = np.array(diti_df["ID game"],dtype='int')
diti_vinto = np.array(diti_df["vinto"],dtype='int')
diti_game_length = np.array(diti_df["Durata in game (Minuti)"],dtype='int')

if DEBUG:
    print("--- Stato ELO iniziale ---")
    for i in range(len(starting_elo)):
        print(f"  - {nicks[i]}: {starting_elo[i]}")
    print("--------------------------\n")

# --- FUNZIONI DI CALCOLO ELO ---

def get_index_elo_players(team_players, nicks):
    """
    Trova gli indici dei giocatori nell'array 'nicks' principale.
    Se un giocatore non viene trovato, restituisce None per segnalare che la partita va saltata.
    """
    indexes = []
    for player in team_players:
        found = False
        for i, nick in enumerate(nicks):
            if nick == player:
                indexes.append(i)
                found = True
                break
        if not found:
            if DEBUG:
                print(f"⚠️ ATTENZIONE: Giocatore '{player}' non trovato nell'elenco principale ('PersoneDitate'). La partita verrà SALTATA.")
            return None
    return indexes

def get_avg_elo(indexes, current_elo):
    """Calcola l'ELO medio di un team basandosi sugli indici dei giocatori."""
    if not indexes: # Se la lista di indici è vuota
        return 0.
    avg_elo = 0.
    for i in indexes:
        avg_elo += current_elo[i]
    return avg_elo / len(indexes)

def compute_elo_diff(elo_opponents, elo_player, outcome, K):
    """Calcola la variazione di ELO per un singolo giocatore."""
    p1 = 1.0 / (1.0 + np.power(10, (elo_opponents - elo_player) / 400))
    return K * (outcome - p1)

def process_game(game_id, winners, losers):
    """Calcola e aggiorna l'ELO per una singola partita."""
    global updated_elo, games_processed_count
    
    if not winners or not losers:
        if DEBUG:
            print(f"\n--- Elaborazione Game ID: {game_id} ---")
            print("  -> ⏭️ Partita saltata: uno dei due team è vuoto o non ha giocatori validi.")
        return

    index_winners = get_index_elo_players(winners, nicks)
    index_losers  = get_index_elo_players(losers, nicks)

    if index_winners is None or index_losers is None:
        if DEBUG:
            print(f"\n--- Elaborazione Game ID: {game_id} ---")
            print("  -> ⏭️ Partita saltata: uno o più giocatori non sono presenti in 'PersoneDitate'.")
        return

    if DEBUG:
        print(f"\n--- Elaborazione Game ID: {game_id} ---")
        print(f"  Vincitori: {winners}")
        print(f"  Perdenti:  {losers}")

    avg_elo_winners = get_avg_elo(index_winners, updated_elo)
    avg_elo_losers  = get_avg_elo(index_losers, updated_elo)
    
    if DEBUG:
        print(f"  ELO Medio Vincitori: {avg_elo_winners:.2f}")
        print(f"  ELO Medio Perdenti:  {avg_elo_losers:.2f}")

    elo_change_for_winners = compute_elo_diff(avg_elo_losers, avg_elo_winners, 1, ELO_K)
    elo_change_for_losers = compute_elo_diff(avg_elo_winners, avg_elo_losers, 0, ELO_K)

    if DEBUG:
        print(f"  Variazione ELO calcolata per i vincitori: {elo_change_for_winners:+.2f}")
        print(f"  Variazione ELO calcolata per i perdenti: {elo_change_for_losers:+.2f}")
        print("  Aggiornamento ELO giocatori:")

    for k in index_winners:
        if DEBUG: print(f"    - {nicks[k]} (Vincitore): {updated_elo[k]:.2f} -> {updated_elo[k] + elo_change_for_winners:.2f} ({elo_change_for_winners:+.2f})")
        updated_elo[k] += elo_change_for_winners
    for k in index_losers:
        if DEBUG: print(f"    - {nicks[k]} (Perdente):  {updated_elo[k]:.2f} -> {updated_elo[k] + elo_change_for_losers:.2f} ({elo_change_for_losers:+.2f})")
        updated_elo[k] += elo_change_for_losers
    
    games_processed_count += 1

# --- CICLO PRINCIPALE DI ELABORAZIONE GAMES ---
last_id_game = 0
team_winners, team_losers = [], []
n_of_diti = len(diti_ids_games)
games_processed_count = 0

print(f"--- Inizio elaborazione di {n_of_diti} righe di partite ---\n")

for i in range(n_of_diti):
    # Stampa una barra di progresso
    if i > 0 and n_of_diti > 100 and i % int(n_of_diti/100) == 0: print(f"🔄 Progresso: {int(i/n_of_diti*100)} % delle righe analizzate", end='\r')

    # Interrompe l'analisi se si raggiunge il limite (per test)
    if stop_analysis != -1 and i >= stop_analysis:
        print(f"\nℹ️ Analisi interrotta al limite di {stop_analysis} righe.")
        break

    # Controlla se il game è già stato analizzato in una sessione precedente
    if last_game_analyzed >= diti_ids_games[i]:
        continue

    # Inizializza last_id_game con il primo ID valido
    if i == 0 or last_id_game == 0:
        last_id_game = diti_ids_games[i]

    # Quando l'ID del game cambia, significa che abbiamo raccolto tutti i giocatori
    # del game precedente e possiamo calcolare il nuovo ELO.
    if last_id_game != diti_ids_games[i]:
        process_game(last_id_game, team_winners, team_losers)
        # Resetta le liste per la prossima partita
        team_winners, team_losers = [], []

    # Aggiunge il giocatore della riga corrente al team corretto
    if diti_vinto[i] == 1:
        team_winners.append(diti_nick_players[i])
    elif diti_vinto[i] == 0:
        team_losers.append(diti_nick_players[i])

    # Aggiorna l'ID dell'ultima partita analizzata
    last_id_game = diti_ids_games[i]

# Elabora l'ultima partita rimasta in sospeso
if team_winners or team_losers:
    process_game(last_id_game, team_winners, team_losers)

print(f"\n\n--- Elaborazione terminata. {games_processed_count} nuove partite sono state processate. ---\n")


# --- AGGIORNAMENTO DEL DATAFRAME E SCRITTURA SU GOOGLE SHEETS ---
elo_df["Elo Classifica Interna"] = np.round(updated_elo).astype(int)

# Crea un array per l'ID dell'ultimo game, da scrivere solo sulla prima riga
array_last_game_analyzed = [''] * len(nicks)
if games_processed_count > 0: # Aggiorna solo se sono state processate nuove partite
    # CONVERSIONE ESPLICITA a int nativo di Python
    array_last_game_analyzed[0] = int(last_id_game)
else:
    # CONVERSIONE ESPLICITA anche qui per sicurezza
    if last_game_analyzed != -1:
        array_last_game_analyzed[0] = int(last_game_analyzed)

elo_df["Last Game ID Checked"] = array_last_game_analyzed

if DEBUG:
    print("--- DataFrame finale prima della scrittura su Google Sheets ---")
    print(elo_df[["Nick", "Elo Tornei", "Elo Classifica Interna", "Last Game ID Checked"]].head())
    print("...")
    print("------------------------------------------------------------")

# Aggiorna il foglio Google
write_sheet(sheet_classifica_interna.worksheet("PersoneDitate"), elo_df)
