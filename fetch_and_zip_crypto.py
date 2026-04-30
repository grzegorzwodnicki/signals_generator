import requests
import json
import time
import hmac
import hashlib
import zipfile
import os
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- KONFIGURACJA ---
# Wpisz swoje klucze API dla wyższych limitów (opcjonalnie)
API_KEY = "ZNTnlQsdBl5p2K6Dms"
API_SECRET = "aExm2IsrMGveuYaXOxrxmzVJaAPnyCrAOdAH"

OUTPUT_DIR = "output"
OUTPUT_JSON = os.path.join(OUTPUT_DIR, "market_data_crypto.json")
OUTPUT_ZIP = os.path.join(OUTPUT_DIR, "market_data_crypto.zip")

# Globalna zmienna dla czasu backtestu (ms)
BACKTEST_TIME_MS = None

# Ensure output directory exists
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR)

# Interwały wymagane przez użytkownika
TIMEFRAMES = ["1W", "1D", "4H", "1H", "30m", "15m", "5m", "1m"]
MAX_WORKERS = 30  # Liczba równoległych wątków
RATE_LIMIT_SEMAPHORE = threading.Semaphore(30)  # Maks. równoległych requestów HTTP
DELAY_BETWEEN_DAYS = 2  # Sekundy przerwy między kolejnymi dniami w zakresie

def generate_signature(params_str, timestamp):
    if not API_SECRET: return ""
    recv_window = "5000"
    raw_data = timestamp + API_KEY + recv_window + params_str
    return hmac.new(bytes(API_SECRET, "utf-8"), bytes(raw_data, "utf-8"), hashlib.sha256).hexdigest()

def get_all_bybit_tickers():
    """Pobiera wszystkie tickery linear z Bybit API v5."""
    url = "https://api.bybit.com/v5/market/tickers"
    params = {"category": "linear"}
    try:
        print("Pobieranie tickerów z Bybit...")
        response = requests.get(url, params=params, timeout=15)
        data = response.json()
        if data.get("retCode") != 0:
            print(f"Błąd Bybit: {data.get('retMsg')}")
            return []
        return data["result"]["list"]
    except Exception as e:
        print(f"Błąd podczas pobierania danych rynkowych: {e}")
        return []

def get_top_400_crypto():
    """Wybiera Top 400 wg turnover (price * volume)."""
    tickers = get_all_bybit_tickers()
    if not tickers: return []
    
    crypto_list = []
    for t in tickers:
        symbol = t.get("symbol")
        if symbol.endswith("USDT"):
            crypto_list.append({
                "symbol": symbol,
                "price": float(t.get("lastPrice", 0)),
                "turnover": float(t.get("turnover24h", 0))
            })
    
    # Sortowanie po turnover malejąco
    crypto_list.sort(key=lambda x: x["turnover"], reverse=True)
    top_400 = crypto_list[:400]
    print(f"Wybrano {len(top_400)} topowych par USDT Futures (dostępnych: {len(crypto_list)}).")
    return top_400

def get_file_crypto(filename):
    """Czyta plik crypto_ftmo.txt i wybiera tylko te symbole."""
    #filename = "crypto_ftmo.txt"
    if not os.path.exists(filename):
        print(f"BŁĄD: Plik {filename} nie istnieje!")
        return []
    
    with open(filename, "r") as f:
        ftmo_symbols = [line.strip().upper() for line in f if line.strip()]
    
    if not ftmo_symbols:
        print(f"BŁĄD: Plik {filename} jest pusty!")
        return []

    tickers = get_all_bybit_tickers()
    if not tickers: return []

    crypto_list = []
    for t in tickers:
        symbol = t.get("symbol")
        if symbol in ftmo_symbols:
            crypto_list.append({
                "symbol": symbol,
                "price": float(t.get("lastPrice", 0)),
                "turnover": float(t.get("turnover24h", 0))
            })
    
    found_symbols = [c["symbol"] for c in crypto_list]
    missing = [s for s in ftmo_symbols if s not in found_symbols]
    if missing:
        print(f"INFO: Następujących symboli z pliku nie znaleziono na Bybit Linear: {', '.join(missing)}")
    
    print(f"Wybrano {len(crypto_list)} symboli na podstawie pliku {filename}.")
    return crypto_list

def get_bybit_candles_batch(symbol, interval, limit=200, end_time=None):
    url = "https://api.bybit.com/v5/market/kline"
    mapping = {"1W": "W", "1D": "D", "4H": "240", "1H": "60", "30m": "30", "15m": "15", "5m": "5", "1m": "1"}
    bybit_interval = mapping.get(interval, interval)

    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": bybit_interval,
        "limit": limit
    }
    if end_time: params["end"] = end_time

    for attempt in range(5):
        try:
            with RATE_LIMIT_SEMAPHORE:
                resp = requests.get(url, params=params, timeout=15)
                d = resp.json()

            ret_code = d.get("retCode")
            if ret_code == 0:
                # Pusta lista = ticker nie istniał w tym okresie — nie retryuj
                return d["result"]["list"]

            # Rate limit (10002 / 10006 / 10018) lub server error — czekaj i retry
            ret_msg = d.get("retMsg", "")
            if ret_code in (10002, 10006, 10018) or "rate" in ret_msg.lower():
                wait = 2 ** (attempt + 2)  # 4, 8, 16, 32, 64s
                print(f"  [RATE LIMIT] {symbol} {interval}, retry {attempt+1}/5 za {wait}s")
                time.sleep(wait)
                continue

            return []
        except Exception:
            wait = 2 ** attempt
            time.sleep(wait)

    return []

def get_400_candles(symbol, interval, end_time_ms=None):
    """Pobiera 400 świec (2 paczki po 200)."""
    batch1 = get_bybit_candles_batch(symbol, interval, limit=200, end_time=end_time_ms)
    if not batch1: return []
    
    last_ts = int(batch1[-1][0]) - 1
    batch2 = get_bybit_candles_batch(symbol, interval, limit=200, end_time=last_ts)
    
    full = batch1 + batch2
    formatted = []
    for c in full:
        formatted.append({
            "time": int(c[0]) // 1000,
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5])
        })
    formatted.reverse()
    return formatted

def fetch_all_data_for_symbol(info):
    """Pobiera wszystkie dane dla jednej monety."""
    symbol = info["symbol"]
    try:
        current_time = int(time.time())
        if BACKTEST_TIME_MS:
            current_time = BACKTEST_TIME_MS // 1000

        data = {
            "current": {"price": info["price"], "time": current_time},
            "timeframes": {}
        }
        
        for tf in TIMEFRAMES:
            candles = get_400_candles(symbol, tf, end_time_ms=BACKTEST_TIME_MS)
            data["timeframes"][tf] = candles
            
            # Jeśli jesteśmy w trybie backtest i to jest interwał 1m, 
            # zaktualizuj current price na close ostatniej świecy
            if BACKTEST_TIME_MS and tf == "1m" and candles:
                data["current"]["price"] = candles[-1]["close"]
                
        return symbol, data
    except:
        return symbol, None

def main():
    global BACKTEST_TIME_MS
    print("=== Bybit Crypto Downloader ===")
    
    # --- Zapytanie o tryb (Teraz/Dzień/Zakres) ---
    bt_choice = input("Czy pobrać dane terazniejsze? (t/n): ").strip().lower()
    
    tasks = [] # Lista (BACKTEST_TIME_MS, date_str, json_date, zip_it)
    
    if bt_choice == 'n':
        mode = input("Pobrać wybrany dzień (d) czy zakres (z)? ").strip().lower()
        if mode == 'z':
            range_input = input("Podaj zakres dat (dd/mm/yyyy - dd/mm/yyyy): ").strip()
            time_input = input("Podaj godzinę (hh:mm): ").strip()
            try:
                start_str, end_str = range_input.split("-")
                start_dt = datetime.strptime(start_str.strip(), "%d/%m/%Y")
                end_dt = datetime.strptime(end_str.strip(), "%d/%m/%Y")
                hour, minute = map(int, time_input.split(":"))
                
                current_dt = start_dt
                while current_dt <= end_dt:
                    dt_obj = current_dt.replace(hour=hour, minute=minute)
                    ts_ms = int(dt_obj.timestamp() * 1000)
                    d_str = dt_obj.strftime("%Y_%m_%d_%H%M")
                    j_date = dt_obj.strftime("%Y-%m-%d %H:%M")
                    tasks.append((ts_ms, d_str, j_date, False))
                    current_dt += timedelta(days=1)
            except Exception as e:
                print(f"Błąd formatu: {e}. Przerywam.")
                return
        else:
            date_input = input("Podaj datę i czas (dd/mm/yyyy hh:mm): ").strip()
            try:
                dt_obj = datetime.strptime(date_input, "%d/%m/%Y %H:%M")
                ts_ms = int(dt_obj.timestamp() * 1000)
                d_str = dt_obj.strftime("%Y_%m_%d_%H%M")
                j_date = dt_obj.strftime("%Y-%m-%d %H:%M")
                tasks.append((ts_ms, d_str, j_date, False))
            except ValueError:
                print("Nieprawidłowy format daty! Przerywam.")
                return
    else:
        now = datetime.now()
        d_str = now.strftime("%Y_%m_%d_%H%M")
        j_date = now.strftime("%Y-%m-%d %H:%M")
        tasks.append((None, d_str, j_date, True))

    # Wybór symboli (tylko raz dla całego procesu)
    print("\nWybierz źródło symboli:")
    print("1. Top 400 wg obrotu (standard)")
    print("2. Symbole z pliku crypto_ftmo.txt")
    print("3. Symbole z pliku crypto_breakout.txt")
    
    choice = input("Wybór (1/2, domyślnie 1): ").strip()
    if choice == "3":
        top_crypto = get_file_crypto("crypto_breakout.txt")
    elif choice == "2":
        top_crypto = get_file_crypto("crypto_ftmo.txt")
    else:
        top_crypto = get_top_400_crypto()
    
    if not top_crypto:
        print("Nie znaleziono żadnych symboli do pobrania. Koniec.")
        return

    # Pętla po zadaniach (dniach)
    for task_idx, (ts_ms, d_str, j_date, zip_it) in enumerate(tasks):
        if task_idx > 0 and DELAY_BETWEEN_DAYS > 0:
            print(f"Przerwa {DELAY_BETWEEN_DAYS}s przed kolejnym dniem...")
            time.sleep(DELAY_BETWEEN_DAYS)

        BACKTEST_TIME_MS = ts_ms
        print(f"\n--- Przetwarzanie: {j_date} ({task_idx+1}/{len(tasks)}) ---")
        
        output_json = os.path.join(OUTPUT_DIR, f"market_data_crypto_{d_str}.json")
        output_zip = os.path.join(OUTPUT_DIR, f"market_data_crypto_{d_str}.zip")

        all_data = {
            "metadata": {
                "date": j_date,
                "is_backtest": BACKTEST_TIME_MS is not None
            },
            "symbols": {}
        }
        total = len(top_crypto)
        
        print(f"Pobieranie danych dla {total} symboli przy użyciu {MAX_WORKERS} wątków...")
        start_time = time.time()

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(fetch_all_data_for_symbol, c): c for c in top_crypto}
            
            completed = 0
            for future in as_completed(futures):
                symbol, data = future.result()
                if data:
                    all_data["symbols"][symbol] = data
                
                completed += 1
                if completed % 50 == 0 or completed == total:
                    print(f"Postęp: {completed}/{total} ({(completed/total)*100:.1f}%)")

        print(f"Zapisywanie do {output_json}...")
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(all_data, f, indent=2)

        if zip_it:
            print(f"Tworzenie archiwum {output_zip}...")
            with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
                zipf.write(output_json, arcname=os.path.basename(output_json))
            if os.path.exists(output_json):
                os.remove(output_json)
                print("Tymczasowy JSON usunięty.")
        else:
            print(f"Plik JSON zachowany: {output_json}")

        end_time = time.time()
        print(f"Gotowe! Dzień zajęła {end_time - start_time:.2f} sekund.")


if __name__ == "__main__":
    main()
