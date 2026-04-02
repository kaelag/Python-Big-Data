import time
import timescaledb_model as tsdb

import os
import numpy as np
import pandas as pd
from datetime import datetime
from loguru import logger


TSDB = tsdb.TimescaleStockMarketModel
DATADIR = os.environ.get("DATADIR", "/mnt/data/")
EURONEXT_DIR = os.path.join(DATADIR, "euronext")
BOURSO_DIR = os.path.join(DATADIR, "boursorama")


# =================================================
# Extract, Transform and Load data in the database
# =================================================


def timer_decorator(func):
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        print(f"{func.__name__} run in {(end_time - start_time):.2f} seconds.")
        return result
    return wrapper


# --------------- helpers ---------------


def _clean_numeric(series):
    """Nettoie une Series de strings sales en float (espaces, virgules)."""
    return pd.to_numeric(
        series.astype(str)
        .str.replace(" ", "", regex=False)
        .str.replace(",", "", regex=False),
        errors="coerce",
    )


def _read_euronext_file(filepath):
    """Lit un fichier euronext (CSV ou XLSX), retourne un DataFrame propre."""
    if filepath.endswith(".csv"):
        df = pd.read_csv(filepath, sep="\t", skiprows=[1, 2, 3])
    else:
        df = pd.read_excel(filepath, skiprows=[1, 2, 3])

    for col in ("Open", "High", "Low", "Last", "Volume", "Turnover"):
        if col in df.columns:
            df[col] = _clean_numeric(df[col])

    df["Volume"] = df["Volume"].fillna(0)
    return df.dropna(subset=["Open", "High", "Low", "Last"])


def _list_euronext_files(start, end):
    """Liste les fichiers euronext entre start et end (YYYY-MM-DD)."""
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    result = []
    for f in os.listdir(EURONEXT_DIR):
        if not (f.endswith(".csv") or f.endswith(".xlsx")):
            continue
        try:
            dt = datetime.strptime(f.split("_")[-1].rsplit(".", 1)[0], "%Y-%m-%d")
        except ValueError:
            continue
        if s <= dt <= e:
            result.append((dt, os.path.join(EURONEXT_DIR, f)))
    result.sort()
    return result


def _get_or_create_companies(df, db):
    """Cree les companies manquantes, retourne le dict name -> cid."""
    existing = db.df_query("SELECT id, name FROM companies")
    known = dict(zip(existing["name"], existing["id"])) if len(existing) else {}

    new_rows = []
    for _, r in df.drop_duplicates("Name").iterrows():
        if r["Name"] not in known:
            new_rows.append({
                "name": r["Name"],
                "mid": 6,
                "symbol": r.get("Symbol", ""),
                "isin": r.get("ISIN", ""),
                "euronext": r.get("Symbol", ""),
            })
            known[r["Name"]] = None  # evite les doublons dans la boucle

    if new_rows:
        db.df_write(pd.DataFrame(new_rows), "companies")
        db.commit()
        existing = db.df_query("SELECT id, name FROM companies")
        known = dict(zip(existing["name"], existing["id"]))

    return known


def _is_done(fname, db):
    """Verifie si un fichier a deja ete traite."""
    r = db.df_query(f"SELECT name FROM file_done WHERE name = '{fname}'")
    return len(r) > 0


def _mark_done(fname, db):
    """Marque un fichier comme traite."""
    db.raw_query(
        "INSERT INTO file_done (name) VALUES (%s) ON CONFLICT DO NOTHING",
        (fname,),
    )
    db.commit()


# --------------- fonctions publiques ---------------


def get_price_euronext(stock_name, date):
    """Retourne (Open, Last, High, Low, Volume, Turnover) pour une action a une date."""
    for ext in (".csv", ".xlsx"):
        filepath = os.path.join(EURONEXT_DIR, f"Euronext_Equities_{date}{ext}")
        if os.path.exists(filepath):
            break
    else:
        return None

    df = _read_euronext_file(filepath)
    row = df[df["Name"] == stock_name]
    if row.empty:
        return None

    return (
        row["Open"].values[0],
        row["Last"].values[0],
        row["High"].values[0],
        row["Low"].values[0],
        row["Volume"].values[0],
        row["Turnover"].values[0] if "Turnover" in row.columns else 0,
    )


def get_files_in_range(start, end):
    """Liste les fichiers bourso .bz2 dans un intervalle (YYYY-MM-DD-HH:MM)."""
    s = datetime.strptime(start, "%Y-%m-%d-%H:%M")
    e = datetime.strptime(end, "%Y-%m-%d-%H:%M")
    files = []
    for year in os.listdir(BOURSO_DIR):
        year_path = os.path.join(BOURSO_DIR, year)
        if not os.path.isdir(year_path):
            continue
        for fname in os.listdir(year_path):
            if not fname.endswith(".bz2"):
                continue
            try:
                dt = datetime.strptime(fname[6:-4], "%Y-%m-%d %H:%M:%S.%f")
            except ValueError:
                continue
            if s <= dt <= e:
                files.append((dt, os.path.join(year_path, fname)))
    files.sort()
    return files


def get_price_bourso(stock_name, start, end):
    """Retourne un DataFrame (date, value, volume) intraday pour une action."""
    files = get_files_in_range(start, end)
    rows = []
    for dt, path in files:
        df = pd.read_pickle(path, compression="bz2")
        hit = df.loc[df["name"] == stock_name]
        if hit.empty:
            continue
        last = hit["last"].values[0]
        if isinstance(last, str):
            last = float(last.replace(" ", ""))
        rows.append({
            "date": dt,
            "value": last,
            "volume": int(hit["volume"].values[0]),
        })
    return pd.DataFrame(rows)


# --------------- store_files ---------------


@timer_decorator
def store_files(start: str, end: str, website: str, db: TSDB):
    """Point d'entree ETL. start/end au format YYYY-MM-DD."""
    if website == "euronext":
        _store_euronext(start, end, db)
    elif website == "bourso":
        _store_bourso(start, end, db)
    else:
        raise ValueError(f"Source inconnue: {website}")


def _store_euronext(start, end, db):
    """Charge les fichiers euronext dans daystocks."""
    files = _list_euronext_files(start, end)
    logger.info(f"Euronext: {len(files)} fichiers a traiter")

    for dt, path in files:
        fname = os.path.basename(path)
        if _is_done(fname, db):
            continue

        df = _read_euronext_file(path)
        if df.empty:
            logger.warning(f"Fichier vide ou illisible: {fname}")
            continue

        cid_map = _get_or_create_companies(df, db)

        # Conversion numpy pour la perf
        opens = df["Open"].values.astype(np.float32)
        closes = df["Last"].values.astype(np.float32)
        highs = df["High"].values.astype(np.float32)
        lows = df["Low"].values.astype(np.float32)
        vols = df["Volume"].values.astype(np.float32)

        ohlc = np.column_stack([opens, highs, lows, closes])
        means = np.mean(ohlc, axis=1).astype(np.float32)
        stds = np.std(ohlc, axis=1).astype(np.float32)

        daystocks = pd.DataFrame({
            "date": dt,
            "cid": df["Name"].map(cid_map).values.astype(np.int16),
            "open": opens,
            "close": closes,
            "high": highs,
            "low": lows,
            "volume": vols,
            "mean": means,
            "std": stds,
        })

        db.df_write(daystocks, "daystocks")
        _mark_done(fname, db)
        logger.info(f"  {fname}: {len(daystocks)} lignes")

    logger.info("Import euronext termine")


def _store_bourso(start, end, db):
    """Charge les fichiers bourso dans stocks."""
    files = get_files_in_range(start + "-09:00", end + "-18:00")
    logger.info(f"Bourso: {len(files)} fichiers a traiter")

    # On a besoin des companies deja en base (chargees via euronext)
    existing = db.df_query("SELECT id, name FROM companies")
    cid_map = dict(zip(existing["name"], existing["id"])) if len(existing) else {}

    for dt, path in files:
        fname = os.path.basename(path)
        if _is_done(fname, db):
            continue

        df = pd.read_pickle(path, compression="bz2")
        df["last"] = _clean_numeric(df["last"])
        df = df.dropna(subset=["last"])

        # Ne garder que les actions connues
        df["cid"] = df["name"].map(cid_map)
        df = df.dropna(subset=["cid"])
        if df.empty:
            continue

        stocks = pd.DataFrame({
            "date": dt,
            "cid": df["cid"].values.astype(np.int16),
            "value": df["last"].values.astype(np.float32),
            "volume": df["volume"].values.astype(np.float32),
        })

        db.df_write(stocks, "stocks")
        _mark_done(fname, db)

    db.commit()
    logger.info("Import bourso termine")
