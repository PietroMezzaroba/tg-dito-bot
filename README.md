# 🎲 TG Dito Bot

Bot Discord per la community italiana di **Age of Empires 2: Definitive Edition**. Bilanciamento automatico delle squadre, sistema scommesse con auto-risoluzione, casinò e statistiche dettagliate.

## ✨ Funzionalità

### 🛠️ Bilanciamento
| Comando | Descrizione |
|---------|-------------|
| `!bilancia <nome lobby>` | Bilancia i team (4/6/8 giocatori) per ELO |
| `!bilancia dito <nome>` | Modalità 1v1 con quote e scommesse |
| `!notifica <lobby>` | Monitora una lobby e bilancia automaticamente quando piena |
| `!stopnotifica` | Ferma il monitoraggio |

### 🎲 Scommesse Dito
Le scommesse si aprono automaticamente col bilanciamento. I bottoni Discord permettono di puntare, inclusa la modalità ALL-IN.

- Quote live ogni 15 secondi
- Timer di 7 minuti
- Tassa del 5% sul pool
- **Auto-risoluzione**: a partita finita il bot rileva il risultato dall'API Relic e paga automaticamente

| Comando | Descrizione |
|---------|-------------|
| `!scommesse <id>` | Vedi le puntate di un dito |
| `!diti_attivi` | Elenco diti con scommesse aperte |
| `!mie_scommesse` | Le tue scommesse attive |

### 🎰 Casinò
| Comando | Regole |
|---------|--------|
| `!dadi <n>` | Due dadi: 7 o 11 → x2, 2 o 12 → x3, perdita → al banco |
| `!roulette <n>` | Europea (0-36): RED/BLACK/EVEN/ODD/LOW/HIGH → x2, ZERO → x36 |
| `!slot <n>` | 3 rulli: 💎💎💎 → x20, 7️⃣7️⃣7️⃣ → x10, tris → x3, coppia → x1.5 |
| `!plinko <n>` | Pallina a zigzag tra i pioli: premi da x0 a x5 |
| `!blackjack <n>` | Contro il banco: HIT/STAND/DOUBLE, blackjack paga x2.5 |

### 🎟️ Lotteria & Tornei
| Comando | Descrizione |
|---------|-------------|
| `!lotto <n>` | Compra un biglietto per la lotteria settimanale |
| `!estrai_lotto` | Admin: estrazione settimanale (70% al vincitore) |
| `!torneo crea "nome" "P1" "P2"` | Admin: crea match torneo |
| `!tbet <id> <1\|2> <n>` | Scommetti su un match torneo |

### 💰 Economia
| Comando | Descrizione |
|---------|-------------|
| `!wallet` | Il tuo saldo in U |
| `!daily` | Bonus 50 U ogni 24 ore |
| `!banca` | Classifica TOP 10 più ricchi |
| `!pay @utente <n>` | Invia U a un utente |
| `!profilo [@utente]` | Statistiche scommesse e titoli |

### 📊 Statistiche Giocatori
| Comando | Descrizione |
|---------|-------------|
| `!stats <nome>` | ELO, winrate, partite totali |
| `!elo <nome>` | Solo ELO |
| `!history <nome>` | Ultime partite con civiltà ed esito |
| `!streak <nome>` | Serie vittorie/sconfitte consecutive |
| `!variazioni <nome>` | Variazioni ELO partita per partita |
| `!civ <nome>` | Civiltà più giocate |
| `!classifica` | Top 20 ELO attivi |
| `!tgstats` | Report mensile attività |

### ⚔️ Confronti
| Comando | Descrizione |
|---------|-------------|
| `!scontri A vs B` | Testa a testa tra due giocatori |
| `!confronta A vs B` | Confronto statistiche |
| `!nemesi <nome>` | Il giocatore contro cui perdi di più |

### 🔗 Utility
| Comando | Descrizione |
|---------|-------------|
| `!link <steam_id>` | Collega il tuo account Steam |
| `!storico` | Cronologia scommesse e transazioni |

### ⚙️ Admin
| Comando | Descrizione |
|---------|-------------|
| `!risolvi_dito <id> <1\|2>` | Risolvi manualmente un dito (con conferma) |
| `!annulla_dito <id>` | Annulla dito e rimborsa tutti |
| `!banco` | Statistiche incassi casa |
| `!sync_banco` | Versa le tasse nel wallet del banco |
| `!give_u @utente <n>` | Dai U a un utente |
| `!premia @utente <n>` | Paga un utente dal banco |
| `!wallets` | Tabella completa wallet utenti |
| `!statbot` | Statistiche globali del bot |
| `!UpdateElo` | Aggiorna ELO da Google Sheets |
| `!UpdateDB` | Sincronizza DB da Google Sheets |
| `!reset_wallets` | Reset globale (chiede conferma) |

## 🔧 Setup

### Requisiti
```bash
pip install -r requirements.txt
```

### File `.env`
Crea un file `.env` nella root con:
```env
DISCORD_BOT_TOKEN=il_tuo_token
GOOGLE_API_KEY=google_gemini_api_key   # opzionale
DB_HOST=127.0.0.1
DB_NAME=nome_database
DB_USER=utente
DB_PASS=password
```

### Database PostgreSQL
Il bot usa PostgreSQL per:
- Dati giocatori (ELO, statistiche)
- Wallet e scommesse (U economy)
- Tracking diti attivi

### Avvio
```bash
python3 bot.py
```

## 🏗️ Architettura

```
bot.py              → Bot principale (RelicAPI, bilanciamento, statistiche)
betting_system.py   → Sistema scommesse (BetView, wallet, economy)
features.py         → Giochi casinò, lotteria, tornei, profili
UpdateExcel.py      → Calcolo ELO da Google Sheets
Database/Database.py → Sincronizzazione Google Sheets → PostgreSQL
```

### API Esterne
- **Relic API** (`aoe-api.worldsedgelink.com`) — lobby, match history, giocatori
- **Google Sheets API** — dati raw (sincronizzati nel DB)
- **Google Gemini** — messaggi deliranti periodici

### Flusso Auto-Risoluzione
```
!bilancia dito → trova lobby API → salva steam_id giocatori
    ↓
scommesse aperte (7 min)
    ↓
auto_resolve_loop (ogni 120s):
    → API Relic: cerca match con i giocatori
    → Verifica: match successivo al dito?
    → Verifica: tutti i giocatori presenti?
    → Risolve e paga automaticamente 🤖
```

## 🔒 Sicurezza

- Credenziali solo in `.env` (mai committato)
- Chiavi Google in `credentials/` (in `.gitignore`)
- Token API mai hardcodati
- Comandi admin protetti da `is_bot_admin()`

---

🍕 Fatto con amore per la community TG Dito
