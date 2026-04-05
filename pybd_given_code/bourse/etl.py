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


# nettoie les colonnes numeriques qui ont des espaces ou des virgules
def clean_num(s):
    s = s.astype(str)
    s = s.str.replace(" ", "", regex=False)
    s = s.str.replace(",", "", regex=False)
    s = s.str.replace("(c)", "", regex=False)
    result = pd.to_numeric(s, errors="coerce")
    return result


def read_euronext(path):
    if path.endswith(".csv"):
        df = pd.read_csv(path, sep="\t", skiprows=[1, 2, 3])
    else:
        df = pd.read_excel(path, skiprows=[1, 2, 3])

    # les colonnes changent selon la source, faut renommer
    rename_map = {
        "Open Price": "Open",
        "High Price": "High",
        "low Price": "Low",
        "last Price": "Last",
        "Currency": "Trading Currency",
        "last Trade MIC Time": "Last Date/Time",
    }
    df.rename(columns=rename_map, inplace=True)

    for col in ("Open", "High", "Low", "Last", "Volume"):
        if col in df.columns:
            df[col] = clean_num(df[col])

    # volume manquant = 0, pas grave
    df["Volume"] = df["Volume"].fillna(0)
    df = df.dropna(subset=["Open", "High", "Low", "Last"])
    return df


def list_euronext(start, end):
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    files = []
    for f in os.listdir(EURONEXT):
        if not (f.endswith(".csv") or f.endswith(".xlsx")):
            continue
        # le nom du fichier contient la date à la fin ex: euronext_2024-01-15.csv
        try:
            date_part = f.split("_")[-1].rsplit(".", 1)[0]
            dt = datetime.strptime(date_part, "%Y-%m-%d")
        except ValueError:
            continue
        if s <= dt <= e:
            files.append((dt, os.path.join(EURONEXT, f)))
    files.sort()
    return files


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
                # fichier mal nommé, on skip
                continue
            if s <= dt <= e:
                files.append((dt, os.path.join(year_path, fname)))
    files.sort()
    return files


def get_cid_map(db, new_names=None):
    if new_names:
        rows = []
        for n in new_names:
            rows.append({"name": n, "mid": 6, "symbol": "", "isin": "", "euronext": ""})
        db.df_write(pd.DataFrame(rows), "companies")
        db.commit()
    df = db.df_query("SELECT id, name FROM companies")
    if len(df) == 0:
        return {}
    return dict(zip(df["name"], df["id"]))


def read_bourso_file(args):
    dt, path = args
    try:
        with bz2.open(path, "rb") as f:
            df = pickle.load(f)
    except Exception as e:
        print("erreur lecture fichier:", path, e)
        return None, None

    prices = clean_num(df["last"])
    mask = prices.notna()
    if not mask.any():
        return None, None

    date_str = dt.strftime("%Y-%m-%d")
    noms = df.loc[mask, "name"].values
    px = prices[mask].values.astype(np.float32)
    # TODO: verifier si volume doit etre float ou int
    vol = pd.to_numeric(df.loc[mask, "volume"], errors="coerce").fillna(0).values

    return (date_str, noms, px, vol), os.path.basename(path)


def store_euronext(start, end, db):
    files = list_euronext(start, end)
    done = set(db.df_query("SELECT name FROM file_done")["name"])
    cid_map = get_cid_map(db)

    todo = [(dt, p) for dt, p in files if os.path.basename(p) not in done]
    if not todo:
        return

    print("Euronext: {} fichiers à charger".format(len(todo)))
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
            "open": df["Open"].astype(float),
            "close": df["Last"].astype(float),
            "high": df["High"].astype(float),
            "low": df["Low"].astype(float),
            "volume": df["Volume"],
        })

        # calcul moyenne et ecart type sur ohlc
        ohlc = day[["open", "high", "low", "close"]]
        day["mean"] = ohlc.mean(axis=1)
        day["std"] = ohlc.std(axis=1)

        frames.append(day)
        fnames.append(os.path.basename(path))

    if frames:
        all_data = pd.concat(frames, ignore_index=True)
        db.df_write(all_data, "daystocks", chunksize=5000)
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
    print(f"Bourso: {total} fichiers à charger")

    if status:
        status["total"] = total
        status["done"] = 0

    # 4 threads, testé avec plus mais pas vraiment plus rapide sur cette machine
    NB_WORKERS = 4
    BATCH = 30000  # si trop grand ca mange trop de RAM
    processed = 0

    for i in range(0, total, BATCH):
        batch = todo[i:i + BATCH]
        days = []
        names = []
        prices = []
        volumes = []
        fnames = []

        with ThreadPoolExecutor(max_workers=NB_WORKERS) as pool:
            results = list(pool.map(read_bourso_file, batch))

        for idx, (result, fname) in enumerate(results):
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
        ).reset_index()
        agg = agg.rename(columns={"day": "date"})

        new_names = set(agg["name"]) - set(cid_map.keys())
        if new_names:
            cid_map = get_cid_map(db, new_names)

        agg["cid"] = agg["name"].map(cid_map)
        agg = agg.dropna(subset=["cid"])

        ohlc = agg[["open", "high", "low", "close"]]
        agg["mean"] = ohlc.mean(axis=1)
        agg["std"] = ohlc.std(axis=1)
        agg["cid"] = agg["cid"].astype("Int16")
        # les prix restent en float standard
        for col in ("open", "close", "high", "low"):
            agg[col] = agg[col].astype(float)

        cols = ["date", "cid", "open", "close", "high", "low", "volume", "mean", "std"]
        db.df_write(agg[cols], "daystocks", chunksize=10000)
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
