import google.auth
from google.auth.transport.requests import AuthorizedSession
import psycopg2
from psycopg2 import sql
import hashlib
import time
import uuid
import os
import logging
from datetime import datetime

# Carica variabili d'ambiente dal file .env
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env'))
except ImportError:
    pass

# Configurazione logging con livello DEBUG
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('sync.log'),
        logging.StreamHandler()
    ]
)

class SheetDBSynchronizer:
    def __init__(self):
        self.db_config = {
            "host": os.getenv("DB_HOST", "127.0.0.1"),
            "database": os.getenv("DB_NAME", "AgeOfEmpiresItalia_new"),
            "user": os.getenv("DB_USER", "bertocci"),
            "password": os.getenv("DB_PASS", ""),
            "port": os.getenv("DB_PORT", "5432")
        }
        self.sheet_config = {
            "spreadsheet_id": "1brJv_EYjHcf_6XlxP0tTdtepXwVn3dPcfeLjPF-rCf4",
            "sheets": ["Persone", "Accounts", "iDiti", "UggioXSessa"],
            "credentials_path": "/opt/bot_classifica/credentials/classificainterna-52aecb7873d2.json"
        }
        self.sync_interval = 300
        self.primary_keys = {
            "persone": "id",
            "accounts": "account_id",
            "iditi": ["dita_id", "id_game"],
            "uggioxsessa": "uggioxsessa_id"
        }
        self.last_hashes = {}
        self.conn = None # Inizializza la connessione a None

    def initialize(self):
        """Inizializza connessioni e sessioni"""
        try:
            self.credentials, _ = google.auth.load_credentials_from_file(
                self.sheet_config["credentials_path"],
                scopes=['https://www.googleapis.com/auth/spreadsheets']
            )
            self.gsheet_session = AuthorizedSession(self.credentials)
            self.conn = psycopg2.connect(**self.db_config)
            self.conn.autocommit = False
            logging.info("Connessioni inizializzate con successo")
        except Exception as e:
            logging.error(f"Errore inizializzazione: {str(e)}")
            raise

    def sanitize_column_name(self, name):
        """Normalizza i nomi delle colonne per PostgreSQL"""
        if not name: return f"col_{uuid.uuid4().hex[:4]}"
        name = name.strip().lower()
        replacements = {' ': '_', '?': '', '%': 'percent', '(': '', ')': '', '-': '_', '/': '_', '\n': '_'}
        for k, v in replacements.items():
            name = name.replace(k, v)
        name = ''.join(c for c in name if c.isalnum() or c == '_')
        if name and name[0].isdigit():
            name = f"col_{name}"
        return name or f"col_{uuid.uuid4().hex[:4]}"

    def convert_value(self, value, col_name):
        """Converte i valori in base al tipo di colonna"""
        if value is None or value == '':
            return None
        column_types = {
            'id': 'INTEGER', 'account_id': 'INTEGER', 'dita_id': 'INTEGER', 'personeditate_id': 'INTEGER',
            'uggioxsessa_id': 'INTEGER', 'aoe2id': 'INTEGER', 'aoe2_id': 'INTEGER', 'col_1v1_mmr': 'INTEGER',
            'max_1v1_mmr': 'INTEGER', 'tg_mmr': 'INTEGER', 'max_tg_mmr': 'INTEGER', 'col_1v1_games': 'INTEGER',
            'tg_games': 'INTEGER', 'elo': 'INTEGER', 'n_diti': 'INTEGER', 'n_diti_vinti': 'INTEGER',
            'elo_tornei': 'INTEGER', 'elo_classifica_interna': 'INTEGER', 'last_game_id_checked': 'BIGINT',
            'id_game': 'BIGINT', 'vinto': 'INTEGER', 'civ_id': 'INTEGER', 'id_civ': 'INTEGER',
            'durata_irl_minuti': 'INTEGER', 'durata_in_game_minuti': 'INTEGER', 'winrate': 'FLOAT',
            'data': 'TIMESTAMP', 'fine': 'TIMESTAMP', 'ultimo_game_1v1': 'TIMESTAMP',
            'ultimo_game_tg': 'TIMESTAMP', 'ultimogame1v1': 'TIMESTAMP', 'ultimogametg': 'TIMESTAMP'
        }
        col_type = column_types.get(col_name, 'TEXT')
        try:
            if col_type in ('INTEGER', 'BIGINT'):
                clean_value = str(value).split('.')[0].replace(',', '')
                return int(clean_value) if clean_value else None
            elif col_type == 'FLOAT':
                return float(str(value).replace(',', '.'))
            elif col_type == 'TIMESTAMP':
                for fmt in ('%d/%m/%Y %H.%M.%S', '%d/%m/%Y %H:%M:%S', '%d/%m/%Y'):
                    try:
                        dt = datetime.strptime(str(value), fmt)
                        return dt.strftime('%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        continue
                logging.warning(f"Formato data non riconosciuto per '{value}' in colonna {col_name}, inserito come NULL")
                return None
            else:
                return str(value)
        except (ValueError, TypeError) as e:
            logging.warning(f"Errore conversione valore '{value}' per colonna {col_name}: {str(e)}. Inserito come NULL.")
            return None

    def create_table(self, table_name, headers, pk_columns):
        """Crea o aggiorna lo schema della tabella"""
        try:
            with self.conn.cursor() as cur:
                sanitized_headers = [self.sanitize_column_name(h) for h in headers]
                sanitized_pk = [self.sanitize_column_name(pk) for pk in (pk_columns if isinstance(pk_columns, list) else [pk_columns])]
                
                column_types = {
                    'id': 'INTEGER', 'account_id': 'INTEGER', 'dita_id': 'INTEGER', 'personeditate_id': 'INTEGER',
                    'uggioxsessa_id': 'INTEGER', 'aoe2id': 'INTEGER', 'aoe2_id': 'INTEGER', 'col_1v1_mmr': 'INTEGER',
                    'max_1v1_mmr': 'INTEGER', 'tg_mmr': 'INTEGER', 'max_tg_mmr': 'INTEGER', 'col_1v1_games': 'INTEGER',
                    'tg_games': 'INTEGER', 'elo': 'INTEGER', 'n_diti': 'INTEGER', 'n_diti_vinti': 'INTEGER',
                    'elo_tornei': 'INTEGER', 'elo_classifica_interna': 'INTEGER', 'last_game_id_checked': 'BIGINT',
                    'id_game': 'BIGINT', 'vinto': 'INTEGER', 'civ_id': 'INTEGER', 'id_civ': 'INTEGER',
                    'durata_irl_minuti': 'INTEGER', 'durata_in_game_minuti': 'INTEGER', 'winrate': 'FLOAT',
                    'data': 'TIMESTAMP', 'fine': 'TIMESTAMP', 'ultimo_game_1v1': 'TIMESTAMP',
                    'ultimo_game_tg': 'TIMESTAMP', 'ultimogame1v1': 'TIMESTAMP', 'ultimogametg': 'TIMESTAMP'
                }

                cur.execute("SELECT EXISTS (SELECT FROM pg_tables WHERE tablename = %s)", [table_name])
                if not cur.fetchone()[0]:
                    column_defs = [sql.SQL("{} {}").format(sql.Identifier(h), sql.SQL(column_types.get(h, 'TEXT'))) for h in sanitized_headers]
                    pk_constraint = sql.SQL("PRIMARY KEY ({})").format(sql.SQL(', ').join(map(sql.Identifier, sanitized_pk)))
                    create_sql = sql.SQL("CREATE TABLE {} ({})").format(sql.Identifier(table_name), sql.SQL(', ').join(column_defs + [pk_constraint]))
                    cur.execute(create_sql)
                    logging.info(f"Tabella {table_name} creata con successo")
                else:
                    cur.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(sql.Identifier(table_name)))
                    existing_columns = [desc[0] for desc in cur.description]
                    for col in sanitized_headers:
                        if col not in existing_columns:
                            col_type = column_types.get(col, 'TEXT')
                            cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN {} {}").format(sql.Identifier(table_name), sql.Identifier(col), sql.SQL(col_type)))
                            logging.info(f"Colonna {col} aggiunta a {table_name}")
                self.conn.commit()
        except Exception as e:
            logging.error(f"Errore in create_table per {table_name}: {str(e)}")
            self.conn.rollback()
            raise

    def sync_table(self, sheet_name):
        """Sincronizza un singolo foglio"""
        table_name = sheet_name.lower()
        pk_columns = self.primary_keys.get(table_name)
        if not pk_columns:
            logging.error(f"Nessuna chiave primaria definita per {table_name}")
            return
            
        logging.info(f"Inizio sincronizzazione: {sheet_name} -> {table_name}")

        try:
            response = self.gsheet_session.get(f"https://sheets.googleapis.com/v4/spreadsheets/{self.sheet_config['spreadsheet_id']}/values/{sheet_name}")
            response.raise_for_status()
            data = response.json().get('values', [])
            if not data or len(data) < 2:
                logging.warning(f"{sheet_name}: Foglio vuoto o senza dati")
                return

            original_headers = data[0]
            # Assicuriamo che le intestazioni per le chiavi primarie siano corrette
            sanitized_pk = pk_columns if isinstance(pk_columns, list) else [pk_columns]
            for i, pk_name in enumerate(sanitized_pk):
                 if i < len(original_headers):
                     original_headers[i] = pk_name # Forza il nome corretto
            
            headers = [self.sanitize_column_name(h) for h in original_headers]
            self.create_table(table_name, headers, pk_columns)

            rows = data[1:]
            seen_pks = set()
            filtered_rows = []
            
            # --- MODIFICA CHIAVE ---
            # Invece di cercare il nome, assumiamo la posizione.
            # Singola PK -> colonna 0. Doppia PK -> colonne 0 e 1.
            pk_indices = [0, 1] if isinstance(pk_columns, list) else [0]

            for i, row in enumerate(rows):
                if not any(row): continue

                pk_values = tuple(row[idx].strip() for idx in pk_indices if idx < len(row) and row[idx])

                if len(pk_values) == len(pk_indices):
                    if pk_values not in seen_pks:
                        full_row = row[:len(headers)] + [''] * (len(headers) - len(row))
                        converted_row = [self.convert_value(full_row[j], headers[j]) for j in range(len(headers))]
                        filtered_rows.append(converted_row)
                        seen_pks.add(pk_values)
                    else:
                        logging.warning(f"Riga {i+2} ignorata in {sheet_name}: chiave primaria duplicata {pk_values}")
                else:
                    logging.warning(f"Riga {i+2} ignorata in {sheet_name}: chiave primaria mancante o incompleta")
            
            if not filtered_rows:
                logging.warning(f"{sheet_name}: Nessun dato valido dopo il filtraggio")
                return

            current_hash = hashlib.sha256(str(filtered_rows).encode()).hexdigest()
            if current_hash == self.last_hashes.get(table_name):
                logging.info(f"Nessuna modifica rilevata per {table_name}")
                return

            with self.conn.cursor() as cur:
                from psycopg2.extras import execute_batch
                
                temp_table = f"temp_{table_name}_{uuid.uuid4().hex[:6]}"
                cur.execute(sql.SQL("CREATE TEMP TABLE {} (LIKE {} INCLUDING ALL) ON COMMIT DROP").format(sql.Identifier(temp_table), sql.Identifier(table_name)))

                cols_sql = sql.SQL(', ').join(map(sql.Identifier, headers))
                placeholders_sql = sql.SQL(', ').join(sql.Placeholder() * len(headers))
                insert_sql = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(sql.Identifier(temp_table), cols_sql, placeholders_sql)
                
                execute_batch(cur, insert_sql, filtered_rows)
                
                pk_cols_sanitized = [self.sanitize_column_name(pk) for pk in (pk_columns if isinstance(pk_columns, list) else [pk_columns])]
                update_fields = [sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(h)) for h in headers if h not in pk_cols_sanitized]

                upsert_sql = sql.SQL("""
                    INSERT INTO {main} ({cols})
                    SELECT {cols} FROM {temp}
                    ON CONFLICT ({pk})
                    DO UPDATE SET {updates}
                """).format(
                    main=sql.Identifier(table_name),
                    cols=cols_sql,
                    temp=sql.Identifier(temp_table),
                    pk=sql.SQL(', ').join(map(sql.Identifier, pk_cols_sanitized)),
                    updates=sql.SQL(', ').join(update_fields)
                )
                cur.execute(upsert_sql)
                self.conn.commit()
                self.last_hashes[table_name] = current_hash
                logging.info(f"Sincronizzati {len(filtered_rows)} record per la tabella {table_name}")

        except Exception as e:
            logging.error(f"Errore sincronizzazione {table_name}: {str(e)}", exc_info=True)
            if self.conn:
                self.conn.rollback()

    def run(self):
        """Avvia il servizio di sincronizzazione per una singola esecuzione"""
        self.initialize()
        logging.info("Servizio di sincronizzazione avviato per esecuzione singola")
        try:
            start_time = datetime.now()
            logging.info(f"\n=== Inizio ciclo di sincronizzazione: {start_time.strftime('%Y-%m-%d %H:%M:%S')} ===")
            for sheet in self.sheet_config["sheets"]:
                self.sync_table(sheet)
            end_time = datetime.now()
            logging.info(f"=== Ciclo completato in {(end_time - start_time).total_seconds():.2f} secondi ===")
        except Exception as e:
            logging.error(f"Si è verificato un errore critico durante l'esecuzione: {e}", exc_info=True)
        finally:
            if self.conn:
                self.conn.close()
                logging.info("Connessioni al database chiuse correttamente.")

if __name__ == "__main__":
    sync = SheetDBSynchronizer()
    sync.run()
