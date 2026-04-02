import os
import time
import numpy as np
import pandas as pd
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
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
        .str.replace(",", "", regex=False)
        .str.replace("(c)", "", regex=False),
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


_bourso_cache = None


def _get_bourso_all():
    global _bourso_cache
    if _bourso_cache is not None:
        return _bourso_cache
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
            files.append((dt, os.path.join(year_path, fname)))
    files.sort()
    _bourso_cache = files
    return files


def _list_bourso_files(start, end):
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    return [(dt, p) for dt, p in _get_bourso_all() if s <= dt <= e]


def _get_or_refresh_cid_map(db, new_names=None, cid_map=None):
    if new_names:
        new_rows = [{"name": n, "mid": 6, "symbol": "", "isin": "", "euronext": ""} for n in new_names]
        db.df_write(pd.DataFrame(new_rows), "companies")
        db.commit()
    existing = db.df_query("SELECT id, name FROM companies")
    return dict(zip(existing["name"], existing["id"])) if len(existing) else {}


def preload_bourso_companies(db: TSDB):
    cid_map = _get_or_refresh_cid_map(db)
    all_names = set()
    for year in os.listdir(BOURSO_DIR):
        year_path = os.path.join(BOURSO_DIR, year)
        if not os.path.isdir(year_path):
            continue
        files = [f for f in os.listdir(year_path) if f.endswith(".bz2")]
        for f in files[:2]:
            try:
                df = pd.read_pickle(os.path.join(year_path, f), compression="bz2")
                all_names.update(df["name"].unique())
            except Exception:
                continue

    new_names = all_names - set(cid_map.keys())
    if new_names:
        _get_or_refresh_cid_map(db, new_names)
        logger.info(f"Bourso: {len(new_names)} nouvelles actions enregistrees")
    else:
        logger.info("Bourso: toutes les actions deja connues")


def _read_bourso_for_stock(args):
    dt, path, stock_name = args
    try:
        import bz2, pickle
        with bz2.open(path, "rb") as f:
            df = pickle.load(f)
    except Exception:
        return None
    hit = df[df["name"] == stock_name]
    if hit.empty:
        return None
    raw = str(hit["last"].iloc[0]).replace(" ", "").replace(",", "").replace("(c)", "")
    try:
        price = float(raw)
    except (ValueError, TypeError):
        return None
    try:
        vol = float(hit["volume"].iloc[0])
    except (ValueError, TypeError):
        vol = 0.0
    return (dt.strftime("%Y-%m-%d"), price, vol)


def load_stock_priority(stock_name, start, end, db: TSDB):
    files = _list_bourso_files(start, end)
    if not files:
        return

    from collections import defaultdict
    by_day = defaultdict(list)
    for dt, path in files:
        by_day[dt.strftime("%Y-%m-%d")].append((dt, path))

    sampled = []
    for day, df_list in by_day.items():
        sampled.append(df_list[0])
        if len(df_list) > 1:
            sampled.append(df_list[-1])
        if len(df_list) > 4:
            sampled.append(df_list[len(df_list) // 2])

    args = [(dt, p, stock_name) for dt, p in sampled]
    workers = os.cpu_count() or 4
    results = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for r in pool.map(_read_bourso_for_stock, args):
            if r is not None:
                results.append(r)

    if not results:
        return

    df = pd.DataFrame(results, columns=["day", "price", "volume"])
    agg = df.groupby("day").agg(
        open=("price", "first"),
        close=("price", "last"),
        high=("price", "max"),
        low=("price", "min"),
        volume=("volume", "max"),
    ).reset_index()

    cid_map = _get_or_refresh_cid_map(db)
    cid = cid_map.get(stock_name)
    if cid is None:
        return

    agg.rename(columns={"day": "date"}, inplace=True)
    agg["cid"] = int(cid)
    ohlc = agg[["open", "high", "low", "close"]]
    agg["mean"] = ohlc.mean(axis=1).astype("float32")
    agg["std"] = ohlc.std(axis=1).astype("float32")

    result = agg[["date", "cid", "open", "close", "high", "low", "volume", "mean", "std"]].copy()
    result["cid"] = result["cid"].astype("Int16")
    for col in ("open", "close", "high", "low", "volume"):
        result[col] = result[col].astype("float32")

    db.df_write(result, "daystocks", chunksize=5000)
    db.commit()
    logger.info(f"Priority: {stock_name} - {len(result)} jours charges")


def store_files(start: str, end: str, website: str, db: TSDB, status=None):
    t0 = time.time()

    if website == "euronext":
        _store_euronext(start, end, db)
    elif website == "bourso":
        _store_bourso(start, end, db, status=status)
    else:
        raise ValueError(f"Source inconnue: {website}")

    logger.info(f"store_files({website}) en {time.time() - t0:.1f}s")


def _store_euronext(start, end, db):
    files = _list_euronext_files(start, end)
    logger.info(f"Euronext: {len(files)} fichiers")

    done = set(db.df_query("SELECT name FROM file_done")["name"])
    cid_map = _get_or_refresh_cid_map(db)

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
            cid_map = _get_or_refresh_cid_map(db, new_names)

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


def _store_bourso(start, end, db, status=None):
    done_df = db.df_query("SELECT name FROM file_done WHERE name LIKE 'bourso_stock_%'")
    done_stocks = {r["name"].replace("bourso_stock_", "") for _, r in done_df.iterrows()}

    all_companies = db.df_query("SELECT id, name FROM companies")
    all_map = dict(zip(all_companies["name"], all_companies["id"]))

    euronext_cids = set()
    ex = db.df_query("SELECT DISTINCT cid FROM daystocks")
    if len(ex):
        euronext_cids = set(ex["cid"])

    bourso_only = [n for n in all_map if all_map[n] not in euronext_cids and n not in done_stocks]
    shared = [n for n in all_map if all_map[n] in euronext_cids and n not in done_stocks]

    todo = bourso_only + shared

    if status is not None:
        status["total"] = len(todo)
        status["done"] = 0

    for i, name in enumerate(todo):
        load_stock_priority(name, start, end, db)
        db.raw_query(
            "INSERT INTO file_done (name) VALUES (%s) ON CONFLICT DO NOTHING",
            (f"bourso_stock_{name}",),
        )
        db.commit()
        if status is not None:
            status["done"] = i + 1

    if status is not None:
        status["finished"] = True
    logger.info("Import bourso termine")
