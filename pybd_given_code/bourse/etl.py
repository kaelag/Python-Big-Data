import os
import time
import numpy as np
import pandas as pd
from datetime import datetime
from loguru import logger

import timescaledb_model as tsdb

TSDB = tsdb.TimescaleStockMarketModel
DATADIR = os.environ.get("DATADIR", "/mnt/data/")
EURONEXT_DIR = os.path.join(DATADIR, "euronext")
BOURSO_DIR = os.path.join(DATADIR, "boursorama")


def _clean_numeric(series):
    return pd.to_numeric(
        series.astype(str)
        .str.replace(" ", "", regex=False)
        .str.replace(",", "", regex=False),
        errors="coerce",
    )


def _read_euronext_file(filepath):
    if filepath.endswith(".csv"):
        df = pd.read_csv(filepath, sep="\t", skiprows=[1, 2, 3])
    else:
        df = pd.read_excel(filepath, skiprows=[1, 2, 3])

    rename = {
        "Open Price": "Open", "High Price": "High",
        "low Price": "Low", "last Price": "Last",
        "Currency": "Trading Currency",
        "last Trade MIC Time": "Last Date/Time",
    }
    df.rename(columns={k: v for k, v in rename.items() if k in df.columns}, inplace=True)

    for col in ("Open", "High", "Low", "Last", "Volume", "Turnover"):
        if col in df.columns:
            df[col] = _clean_numeric(df[col])

    df["Volume"] = df["Volume"].fillna(0)
    return df.dropna(subset=["Open", "High", "Low", "Last"])


def _list_euronext_files(start, end):
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


def _list_bourso_files(start, end):
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


def store_files(start: str, end: str, website: str, db: TSDB):
    t0 = time.time()

    if website == "euronext":
        _store_euronext(start, end, db)
    elif website == "bourso":
        _store_bourso(start, end, db)
    else:
        raise ValueError(f"Source inconnue: {website}")

    logger.info(f"store_files({website}) en {time.time() - t0:.1f}s")


def _store_euronext(start, end, db):
    files = _list_euronext_files(start, end)
    logger.info(f"Euronext: {len(files)} fichiers")

    done = set(db.df_query("SELECT name FROM file_done")["name"])
    existing = db.df_query("SELECT id, name FROM companies")
    cid_map = dict(zip(existing["name"], existing["id"])) if len(existing) else {}

    todo = [(dt, p) for dt, p in files if os.path.basename(p) not in done]
    if not todo:
        logger.info("Rien a charger")
        return

    logger.info(f"{len(todo)} fichiers a charger")
    all_frames = []
    new_fnames = []

    for dt, path in todo:
        df = _read_euronext_file(path)
        if df.empty:
            continue

        new_names = set(df["Name"]) - set(cid_map.keys())
        if new_names:
            new_rows = []
            for _, r in df[df["Name"].isin(new_names)].drop_duplicates("Name").iterrows():
                new_rows.append({
                    "name": r["Name"],
                    "mid": 6,
                    "symbol": r.get("Symbol", ""),
                    "isin": r.get("ISIN", ""),
                    "euronext": r.get("Symbol", ""),
                })
            db.df_write(pd.DataFrame(new_rows), "companies")
            db.commit()
            existing = db.df_query("SELECT id, name FROM companies")
            cid_map = dict(zip(existing["name"], existing["id"]))

        daystocks = pd.DataFrame({
            "date": dt,
            "cid": df["Name"].map(cid_map).astype("Int16"),
            "open": df["Open"].astype("float32"),
            "close": df["Last"].astype("float32"),
            "high": df["High"].astype("float32"),
            "low": df["Low"].astype("float32"),
            "volume": df["Volume"].astype("float32"),
        })

        ohlc = daystocks[["open", "high", "low", "close"]]
        daystocks["mean"] = ohlc.mean(axis=1).astype("float32")
        daystocks["std"] = ohlc.std(axis=1).astype("float32")

        all_frames.append(daystocks)
        new_fnames.append(os.path.basename(path))

    if all_frames:
        big = pd.concat(all_frames, ignore_index=True)
        db.df_write(big, "daystocks", chunksize=5000)
        for fname in new_fnames:
            db.raw_query(
                "INSERT INTO file_done (name) VALUES (%s) ON CONFLICT DO NOTHING",
                (fname,),
            )
        db.commit()
        logger.info(f"{len(big)} lignes inserees")

    logger.info("Import euronext termine")


def _store_bourso(start, end, db):
    files = _list_bourso_files(start + "-09:00", end + "-18:00")
    logger.info(f"Bourso: {len(files)} fichiers")

    done = set(db.df_query("SELECT name FROM file_done")["name"])
    existing = db.df_query("SELECT id, name FROM companies")
    cid_map = dict(zip(existing["name"], existing["id"])) if len(existing) else {}

    todo = [(dt, p) for dt, p in files if os.path.basename(p) not in done]
    if not todo:
        return

    all_frames = []
    new_fnames = []

    for dt, path in todo:
        df = pd.read_pickle(path, compression="bz2")
        df["last"] = _clean_numeric(df["last"])
        df = df.dropna(subset=["last"])

        df["cid"] = df["name"].map(cid_map)
        df = df.dropna(subset=["cid"])
        if df.empty:
            continue

        stocks = pd.DataFrame({
            "date": dt,
            "cid": df["cid"].astype("Int16"),
            "value": df["last"].astype("float32"),
            "volume": df["volume"].astype("float32"),
        })

        all_frames.append(stocks)
        new_fnames.append(os.path.basename(path))

    if all_frames:
        big = pd.concat(all_frames, ignore_index=True)
        db.df_write(big, "stocks", chunksize=5000)
        for fname in new_fnames:
            db.raw_query(
                "INSERT INTO file_done (name) VALUES (%s) ON CONFLICT DO NOTHING",
                (fname,),
            )
        db.commit()

    logger.info("Import bourso termine")
