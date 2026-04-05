import os
import bz2
import pickle
import pandas as pd
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

DATADIR = os.environ.get("DATADIR", "/mnt/data/")
EURONEXT = os.path.join(DATADIR, "euronext")
BOURSO = os.path.join(DATADIR, "boursorama")


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
        df[col] = clean_num(df[col])

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
        
        parts = f.split("_")
        last_part = parts[-1]
        date_str = last_part.rsplit(".", 1)[0]
        dt = datetime.strptime(date_str, "%Y-%m-%d")

        if s <= dt <= e:
            full_path = os.path.join(EURONEXT, f)
            files.append((dt, full_path))
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

            date_part = fname[6:-4]
            date_part = date_part.replace("_", ":")
            dt = datetime.strptime(date_part, "%Y-%m-%d %H:%M:%S.%f")

            if s <= dt <= e:
                full_path = os.path.join(year_path, fname)
                files.append((dt, full_path))
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
    names = df["name"]
    ids = df["id"]
    return dict(zip(names, ids))


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

    day = dt.strftime("%Y-%m-%d")
    name_vals = df.loc[mask, "name"].values
    price_vals = prices[mask].values

    raw_vol = df.loc[mask, "volume"]
    vol_vals = pd.to_numeric(raw_vol, errors="coerce")
    vol_vals = vol_vals.fillna(0)
    vol_vals = vol_vals.values

    fname = os.path.basename(path)
    return (day, name_vals, price_vals, vol_vals), fname


def store_euronext(start, end, db):
    files = list_euronext(start, end)
    done = set(db.df_query("SELECT name FROM file_done")["name"])
    cid_map = get_cid_map(db)

    todo = []
    for dt, p in files:
        fname = os.path.basename(p)
        if fname not in done:
            todo.append((dt, p))

    if not todo:
        return

    frames = []
    fnames = []

    for dt, path in todo:
        df = read_euronext(path)
        if df.empty:
            continue

        actions_connues = set(cid_map.keys())
        actions_fichier = set(df["Name"])
        new_names = actions_fichier - actions_connues
        if new_names:
            cid_map = get_cid_map(db, new_names)

        day = pd.DataFrame()
        day["date"] = [dt] * len(df)
        day["cid"] = df["Name"].map(cid_map).values
        day["open"] = df["Open"].values
        day["close"] = df["Last"].values
        day["high"] = df["High"].values
        day["low"] = df["Low"].values
        day["volume"] = df["Volume"].values

        ohlc = day[["open", "high", "low", "close"]]
        day["mean"] = ohlc.mean(axis=1)
        day["std"] = ohlc.std(axis=1)

        frames.append(day)
        fnames.append(os.path.basename(path))

    if frames:
        all_data = pd.concat(frames, ignore_index=True)
        db.df_write(all_data, "daystocks", chunksize=10000)
        for fname in fnames:
            db.raw_query("INSERT INTO file_done (name) VALUES (%s)", (fname,))
        db.commit()


def store_bourso(start, end, db, status=None):
    files = list_bourso(start, end)
    done = set(db.df_query("SELECT name FROM file_done")["name"])
    cid_map = get_cid_map(db)

    todo = []
    for dt, p in files:
        fname = os.path.basename(p)
        if fname not in done:
            todo.append((dt, p))

    total_files = len(files)

    if not todo:
        if status:
            status["total"] = total_files
            status["done"] = total_files
            status["finished"] = True
        return

    total = len(todo)
    if status:
        status["total"] = total
        status["done"] = 0

    workers = os.cpu_count() or 4
    BATCH = 30000
    processed = 0

    for i in range(0, total, BATCH):
        batch = todo[i:i + BATCH]
        days = []
        names = []
        prices = []
        volumes = []
        fnames = []

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

        df = pd.DataFrame()
        df["day"] = days
        df["name"] = names
        df["last"] = prices
        df["volume"] = volumes

        agg = df.groupby(["day", "name"]).agg(
            open=("last", "first"),
            close=("last", "last"),
            high=("last", "max"),
            low=("last", "min"),
            volume=("volume", "max"),
        )
        agg = agg.reset_index()
        agg = agg.rename(columns={"day": "date"})

        actions_connues = set(cid_map.keys())
        actions_batch = set(agg["name"])
        new_names = actions_batch - actions_connues
        if new_names:
            cid_map = get_cid_map(db, new_names)

        agg["cid"] = agg["name"].map(cid_map)

        ohlc = agg[["open", "high", "low", "close"]]
        agg["mean"] = ohlc.mean(axis=1)
        agg["std"] = ohlc.std(axis=1)

        cols = ["date", "cid", "open", "close", "high", "low", "volume", "mean", "std"]
        db.df_write(agg[cols], "daystocks", chunksize=10000)
        for fname in fnames:
            db.raw_query("INSERT INTO file_done (name) VALUES (%s)", (fname,))
        db.commit()

        processed += len(batch)

    if status:
        status["finished"] = True


def store_files(start, end, website, db, status=None):
    if website == "euronext":
        store_euronext(start, end, db)
    elif website == "bourso":
        store_bourso(start, end, db, status)
