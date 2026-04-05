import os
import bz2
import pickle
import numpy as np
import pandas as pd
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import timescaledb_model as tsdb

DATADIR = os.environ.get("DATADIR", "/mnt/data/")
EURONEXT = os.path.join(DATADIR, "euronext")
BOURSO = os.path.join(DATADIR, "boursorama")


def clean_num(s):
    return pd.to_numeric(
        s.astype(str).str.replace(r"[ ,(c)]", "", regex=True),
        errors="coerce"
    )


def read_euronext(path):
    if path.endswith(".csv"):
        df = pd.read_csv(path, sep="\t", skiprows=[1, 2, 3])
    else:
        df = pd.read_excel(path, skiprows=[1, 2, 3])

    df.rename(columns={
        "Open Price": "Open", "High Price": "High",
        "low Price": "Low", "last Price": "Last",
        "Currency": "Trading Currency",
        "last Trade MIC Time": "Last Date/Time",
    }, inplace=True)

    for col in ("Open", "High", "Low", "Last", "Volume"):
        if col in df.columns:
            df[col] = clean_num(df[col])

    df["Volume"] = df["Volume"].fillna(0)
    return df.dropna(subset=["Open", "High", "Low", "Last"])


def list_euronext(start, end):
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    files = []
    for f in os.listdir(EURONEXT):
        if not (f.endswith(".csv") or f.endswith(".xlsx")):
            continue
        try:
            dt = datetime.strptime(f.split("_")[-1].rsplit(".", 1)[0], "%Y-%m-%d")
        except ValueError:
            continue
        if s <= dt <= e:
            files.append((dt, os.path.join(EURONEXT, f)))
    return sorted(files)


def list_bourso(start, end):
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    files = []
    for year in os.listdir(BOURSO):
        year_path = os.path.join(BOURSO, year)
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
    return sorted(files)


def get_cid_map(db, new_names=None):
    if new_names:
        rows = pd.DataFrame([{"name": n, "mid": 6, "symbol": "", "isin": "", "euronext": ""} for n in new_names])
        db.df_write(rows, "companies")
        db.commit()
    df = db.df_query("SELECT id, name FROM companies")
    return dict(zip(df["name"], df["id"])) if len(df) else {}


def read_bourso_file(args):
    dt, path = args
    try:
        with bz2.open(path, "rb") as f:
            df = pickle.load(f)
    except Exception:
        return None, None

    prices = clean_num(df["last"])
    mask = prices.notna()
    if not mask.any():
        return None, None

    return (
        dt.strftime("%Y-%m-%d"),
        df.loc[mask, "name"].values,
        prices[mask].values.astype(np.float32),
        pd.to_numeric(df.loc[mask, "volume"], errors="coerce").fillna(0).values.astype(np.float32),
    ), os.path.basename(path)


def store_euronext(start, end, db):
    files = list_euronext(start, end)
    done = set(db.df_query("SELECT name FROM file_done")["name"])
    cid_map = get_cid_map(db)

    todo = [(dt, p) for dt, p in files if os.path.basename(p) not in done]
    if not todo:
        return

    print(f"Euronext: {len(todo)} fichiers a charger")
    frames = []
    fnames = []

    for dt, path in todo:
        df = read_euronext(path)
        if df.empty:
            continue

        new_names = set(df["Name"]) - set(cid_map.keys())
        if new_names:
            cid_map = get_cid_map(db, new_names)

        day = pd.DataFrame({
            "date": dt,
            "cid": df["Name"].map(cid_map).astype("Int16"),
            "open": df["Open"].astype("float32"),
            "close": df["Last"].astype("float32"),
            "high": df["High"].astype("float32"),
            "low": df["Low"].astype("float32"),
            "volume": df["Volume"].astype("float32"),
        })

        ohlc = day[["open", "high", "low", "close"]]
        day["mean"] = ohlc.mean(axis=1).astype("float32")
        day["std"] = ohlc.std(axis=1).astype("float32")

        frames.append(day)
        fnames.append(os.path.basename(path))

    if frames:
        db.df_write(pd.concat(frames, ignore_index=True), "daystocks", chunksize=5000)
        for fname in fnames:
            db.raw_query("INSERT INTO file_done (name) VALUES (%s) ON CONFLICT DO NOTHING", (fname,))
        db.commit()


def store_bourso(start, end, db, status=None):
    files = list_bourso(start, end)
    done = set(db.df_query("SELECT name FROM file_done")["name"])
    cid_map = get_cid_map(db)

    todo = [(dt, p) for dt, p in files if os.path.basename(p) not in done]
    if not todo:
        if status:
            status["finished"] = True
        return

    total = len(todo)
    print(f"Bourso: {total} fichiers a charger")

    if status:
        status["total"] = total
        status["done"] = 0

    workers = os.cpu_count() or 4
    BATCH = 30000
    processed = 0

    for i in range(0, total, BATCH):
        batch = todo[i:i + BATCH]
        days, names, prices, volumes, fnames = [], [], [], [], []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for idx, (result, fname) in enumerate(pool.map(read_bourso_file, batch)):
                if result is not None:
                    day, n, p, v = result
                    days.extend([day] * len(n))
                    names.extend(n)
                    prices.extend(p)
                    volumes.extend(v)
                    fnames.append(fname)
                if status:
                    status["done"] = processed + idx + 1

        if not fnames:
            processed += len(batch)
            continue

        df = pd.DataFrame({"day": days, "name": names, "last": prices, "volume": volumes})

        agg = df.groupby(["day", "name"]).agg(
            open=("last", "first"),
            close=("last", "last"),
            high=("last", "max"),
            low=("last", "min"),
            volume=("volume", "max"),
        ).reset_index().rename(columns={"day": "date"})

        new_names = set(agg["name"]) - set(cid_map.keys())
        if new_names:
            cid_map = get_cid_map(db, new_names)

        agg["cid"] = agg["name"].map(cid_map)
        agg = agg.dropna(subset=["cid"])

        ohlc = agg[["open", "high", "low", "close"]]
        agg["mean"] = ohlc.mean(axis=1).astype("float32")
        agg["std"] = ohlc.std(axis=1).astype("float32")
        agg["cid"] = agg["cid"].astype("Int16")
        for col in ("open", "close", "high", "low", "volume"):
            agg[col] = agg[col].astype("float32")

        db.df_write(agg[["date", "cid", "open", "close", "high", "low", "volume", "mean", "std"]], "daystocks", chunksize=10000)
        for fname in fnames:
            db.raw_query("INSERT INTO file_done (name) VALUES (%s) ON CONFLICT DO NOTHING", (fname,))
        db.commit()

        processed += len(batch)

    if status:
        status["finished"] = True


def store_files(start, end, website, db, status=None):
    if website == "euronext":
        store_euronext(start, end, db)
    elif website == "bourso":
        store_bourso(start, end, db, status)
