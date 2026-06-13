import os
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- CONFIGURAZIONE ---
# 1. Incolla qui l'ID del tuo progetto Apps Script che hai trovato nelle impostazioni dell'editor.
ID_PROGETTO_SCRIPT = "INCOLLA_QUI_IL_TUO_ID_SCRIPT"

# Carica le variabili d'ambiente (per trovare il file credentials.json)
load_dotenv()

def leggi_google_apps_script():
    """
    Si connette a Google usando le credenziali del service account e legge
    tutti i file di codice sorgente di un progetto Google Apps Script.
    """
    
    print(f"--- Tentativo di leggere lo script con ID: {ID_PROGETTO_SCRIPT} ---")

    if ID_PROGETTO_SCRIPT == "INCOLLA_QUI_IL_TUO_ID_SCRIPT":
        print("\n❌ ERRORE: Devi inserire l'ID del tuo progetto script nella variabile 'ID_PROGETTO_SCRIPT'.")
        print("▶️ SOLUZIONE: Segui le istruzioni per trovare l'ID e modificalo nello script.")
        return

    # Definiamo gli "scopes", ovvero i permessi che chiediamo a Google.
    # Oltre a spreadsheets e drive, aggiungiamo quello per leggere gli script.
    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/script.projects.readonly" # Permesso per leggere gli script
    ]

    try:
        # L'autenticazione funziona come nel tuo script UpdateExcel.py
        credentials_path = os.getenv("CREDENTIALS_PATH")
        if not credentials_path:
            print("❌ ERRORE: La variabile d'ambiente CREDENTIALS_PATH non è stata trovata.")
            print("▶️ SOLUZIONE: Assicurati di avere un file .env con CREDENTIALS_PATH=percorso/al/tuo/file.json")
            return

        credentials = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
        print("✅ Autenticazione con le API di Google avvenuta con successo.")

        # Costruiamo il servizio per interagire con l'API di Apps Script
        service = build('script', 'v1', credentials=credentials)

        # Chiamiamo l'API per ottenere il contenuto del progetto script
        print("\nRecupero il contenuto dello script...")
        response = service.projects().getContent(scriptId=ID_PROGETTO_SCRIPT).execute()
        
        files = response.get('files', [])
        
        if not files:
            print("ℹ️ Il progetto script esiste ma non contiene file di codice.")
            return

        print(f"✅ Trovati {len(files)} file nel progetto. Stampo il loro contenuto:\n")

        # Iteriamo su ogni file trovato e ne stampiamo il nome e il codice
        for file in files:
            nome_file = file.get('name')
            tipo_file = file.get('type')
            codice = file.get('source')
            
            print("=" * 70)
            print(f"📄 NOME FILE: {nome_file}  (Tipo: {tipo_file})")
            print("=" * 70)
            print(codice)
            print("\n")

    except HttpError as error:
        print(f"\n❌ ERRORE DURANTE LA CHIAMATA API: {error.status_code} - {error.reason}")
        if error.status_code == 404:
            print("▶️ SOLUZIONE: L'ID dello script è sbagliato. Controllalo di nuovo.")
        elif error.status_code == 403:
            print("▶️ SOLUZIONE 1: Assicurati di aver ABILITATO l'API di Google Apps Script nel tuo progetto Google Cloud.")
            print("▶️ SOLUZIONE 2: Assicurati di aver condiviso il tuo Foglio Google (e quindi lo script) con l'email del service account (con permessi da 'Editor').")
        else:
            print(f"Dettagli errore: {error.content}")
    except Exception as e:
        print(f"❌ Si è verificato un errore imprevisto: {e}")

# --- ESECUZIONE DELLO SCRIPT ---
if __name__ == "__main__":
    # Installa le librerie necessarie se non le hai già
    try:
        from googleapiclient.discovery import build
    except ImportError:
        print("Libreria mancante. Per favore, esegui questo comando nel terminale:")
        print("pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib")
    else:
        leggi_google_apps_script()
