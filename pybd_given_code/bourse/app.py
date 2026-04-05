import os
import threading
import warnings
import logging
import dash
import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
from loguru import logger
from dash import Input, Output, dash_table, dcc, html

logger.remove()

import timescaledb_model as tsdb
from etl import store_files

DB_HOST = os.environ.get("DB_HOST", "database")
db = tsdb.TimescaleStockMarketModel("bourse", "bourse", DB_HOST, "password")

app = dash.Dash(__name__, external_stylesheets=[dbc.themes.FLATLY])

bourso_status = {"total": 0, "done": 0, "finished": False}


def get_options():
    df = db.df_query("""
        SELECT DISTINCT c.id, c.name FROM companies c
        JOIN daystocks d ON d.cid = c.id
        ORDER BY c.name
    """)
    names = df["name"]
    ids = df["id"]
    options = []
    for n, i in zip(names, ids):
        options.append({"label": n, "value": i})
    return options


def get_stocks(stock_ids, start, end):
    if not stock_ids or None in stock_ids:
        return pd.DataFrame()
    if not start or not end:
        return pd.DataFrame()
    id_list = []
    for i in stock_ids:
        id_list.append(str(i))
    ids = ",".join(id_list)
    query = f"""SELECT DISTINCT ON (d.date, d.cid)
        d.date, d.open, d.close, d.high, d.low,
        d.volume, d.mean, d.std, c.name
        FROM daystocks d
        JOIN companies c ON d.cid = c.id
        WHERE d.cid IN ({ids})
        AND d.date BETWEEN '{start}' AND '{end}'
        ORDER BY d.date, d.cid, d.volume DESC"""
    return db.df_query(query)


def build_layout():
    opts = get_options()
    return dbc.Container([

        dbc.Row([
            dbc.Col([
                html.H4("Bourse Dashboard", className="fw-bold mb-0 text-primary"),
                html.Small("Données Euronext + Boursorama", className="text-muted"),
            ], className="py-3"),
        ], className="border-bottom mb-3"),

        html.Div(id="loading-section", children=[
            html.Small(id="bourso-status", className="text-muted d-block mb-1"),
            dbc.Progress(id="bourso-progress", 
                         value=0, 
                         striped=True,
                         animated=True,
                         color="primary", 
                         style={"height": "5px"}),
        ], className="mb-4"),

        dbc.Row([
            dbc.Col([
                html.Label("Actions", className="small fw-semibold text-uppercase text-muted mb-1"),
                dcc.Dropdown(id="stock-select", 
                             options=opts, 
                             multi=True, 
                             placeholder="Rechercher des actions..."),
            ], md=6),
            dbc.Col([
                html.Label("Période", className="small fw-semibold text-uppercase text-muted mb-1"),
                dcc.DatePickerRange(id="date-range", 
                                    start_date="2020-05-01",
                                    end_date="2020-12-31", 
                                    display_format="YYYY-MM-DD"),
            ], md=4),
            dbc.Col([
                html.Label("Affichage", className="small fw-semibold text-uppercase text-muted mb-1"),
                dcc.RadioItems(id="chart-type",
                    options=[{"label": " Ligne", "value": "line"}, {"label": " Chandeliers", "value": "candle"}],
                    value="line", 
                    inline=True, 
                    inputStyle={"marginRight": "4px", "marginLeft": "10px"}),
            ], md=2, className="d-flex flex-column justify-content-end"),
        ], className="mb-4 g-3"),

        dbc.Tabs([
            dbc.Tab(label="Cours", tab_id="tab-cours", children=[
                dcc.Graph(id="price-chart", style={"height": "70vh"}),
            ]),
            dbc.Tab(label="Bollinger", tab_id="tab-bollinger", children=[
                dcc.Graph(id="bollinger-chart", style={"height": "70vh"}),
            ]),
            dbc.Tab(label="Données brutes", tab_id="tab-table", children=[
                dash_table.DataTable(id="data-table", page_size=25,
                    style_table={"overflowX": "auto", "marginTop": "15px"},
                    style_cell={"textAlign": "center", "padding": "10px", "fontSize": "13px", "fontFamily": "inherit", "border": "1px solid #dee2e6"},
                    style_header={"fontWeight": "600", "backgroundColor": "#f8f9fa", "borderBottom": "2px solid #dee2e6", "fontSize": "13px"},
                    style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": "#fbfcfd"}],
                ),
            ]),
            dbc.Tab(label="Corrélation", tab_id="tab-corr", children=[
                dcc.Graph(id="corr-chart", style={"height": "70vh"}),
            ]),
            dbc.Tab(label="Statistiques", tab_id="tab-stats", children=[
                dash_table.DataTable(id="stats-table", page_size=25,
                    style_table={"overflowX": "auto", "marginTop": "15px"},
                    style_cell={"textAlign": "center", "padding": "10px", "fontSize": "13px", "fontFamily": "inherit", "border": "1px solid #dee2e6"},
                    style_header={"fontWeight": "600", "backgroundColor": "#f8f9fa", "borderBottom": "2px solid #dee2e6", "fontSize": "13px"},
                    style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": "#fbfcfd"}],
                ),
            ]),
            dbc.Tab(label="Rendements", tab_id="tab-returns", children=[
                dcc.Graph(id="returns-chart", style={"height": "70vh"}),
            ]),
        ], id="tabs", active_tab="tab-cours", className="mb-3"),

        dcc.Interval(id="refresh-interval", interval=2000, n_intervals=0),

    ], fluid=True, className="px-4")


@app.callback(
    Output("stock-select", "options"),
    Output("bourso-status", "children"),
    Output("bourso-progress", "value"),
    Output("loading-section", "style"),
    Input("refresh-interval", "n_intervals"),
)
def refresh_ui(_):
    opts = get_options()

    if bourso_status["finished"]:
        total = bourso_status["total"]
        if total > 0:
            msg = f"Boursorama chargé — {total:,} fichiers (100%)"
        else:
            msg = "Boursorama chargé"
        return opts, msg, 100, {}

    elif bourso_status["total"] > 0:
        done = bourso_status["done"]
        total = bourso_status["total"]
        pct = int(done / total * 100)
        msg = f"Chargement Boursorama : {done:,} / {total:,} ({pct}%)"
        return opts, msg, pct, {}

    return opts, "Chargement Boursorama...", 0, {}


@app.callback(
    Output("price-chart", "figure"),
    Input("stock-select", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
    Input("chart-type", "value"),
)
def price_chart(ids, start, end, mode):
    df = get_stocks(ids, start, end)
    if df.empty:
        return go.Figure()

    fig = go.Figure()

    if mode == "candle":
        first_name = df["name"].iloc[0]
        g = df[df["name"] == first_name]
        g = g.sort_values("date")
        trace = go.Candlestick(
            x=g["date"],
            open=g["open"],
            high=g["high"],
            low=g["low"],
            close=g["close"],
            name=first_name,
        )
        fig.add_trace(trace)
    else:
        for name, g in df.groupby("name"):
            trace = go.Scatter(
                x=g["date"],
                y=g["close"],
                mode="lines",
                name=name,
            )
            fig.add_trace(trace)
        fig.update_yaxes(type="log")

    fig.update_layout(
        title="Cours des actions",
        xaxis_rangeslider_visible=False,
        xaxis_rangebreaks=[dict(bounds=["sat", "mon"])],
        dragmode="pan",
        margin=dict(l=50, r=30, t=50, b=40),
    )
    return fig


@app.callback(
    Output("bollinger-chart", "figure"),
    Input("stock-select", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
    Input("chart-type", "value"),
)
def bollinger_chart(ids, start, end, mode):
    if not ids:
        return go.Figure()
    stock_id = ids[0]
    df = get_stocks([stock_id], start, end)
    if len(df) < 20:
        return go.Figure()

    df = df.sort_values("date")
    df["sma"] = df["close"].rolling(20).mean()
    std20 = df["close"].rolling(20).std()
    df["upper"] = df["sma"] + 2 * std20
    df["lower"] = df["sma"] - 2 * std20

    name = df["name"].iloc[0]
    fig = go.Figure()

    band_sup = go.Scatter(
        x=df["date"], y=df["upper"],
        name="Bande sup",
        line=dict(color="rgba(150,150,150,0.3)"),
    )
    band_inf = go.Scatter(
        x=df["date"], y=df["lower"],
        name="Bande inf",
        line=dict(color="rgba(150,150,150,0.3)"),
        fill="tonexty",
        fillcolor="rgba(200,200,200,0.2)",
    )
    sma_line = go.Scatter(
        x=df["date"], y=df["sma"],
        name="SMA 20",
        line=dict(dash="dash", color="orange"),
    )

    fig.add_trace(band_sup)
    fig.add_trace(band_inf)
    fig.add_trace(sma_line)

    if mode == "candle":
        trace = go.Candlestick(
            x=df["date"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name=name,
        )
        fig.add_trace(trace)
    else:
        trace = go.Scatter(
            x=df["date"], y=df["close"],
            name=name,
            line=dict(color="#2c7be5"),
        )
        fig.add_trace(trace)

    fig.update_layout(
        title=f"Bandes de Bollinger — {name}",
        xaxis_rangeslider_visible=False,
        xaxis_rangebreaks=[dict(bounds=["sat", "mon"])],
        dragmode="pan",
        margin=dict(l=50, r=30, t=50, b=40),
    )
    return fig


@app.callback(
    Output("data-table", "data"),
    Output("data-table", "columns"),
    Input("stock-select", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
)
def data_table(ids, start, end):
    df = get_stocks(ids, start, end)
    if df.empty:
        return [], []

    dates = pd.to_datetime(df["date"])
    df["date"] = dates.dt.strftime("%Y-%m-%d")

    rename_map = {
        "date": "Date",
        "name": "Action",
        "low": "Min",
        "high": "Max",
        "open": "Début",
        "close": "Fin",
        "mean": "Moyenne",
        "std": "Écart-type",
    }
    table = df.rename(columns=rename_map)
    colonnes = ["Date", "Action", "Min", "Max", "Début", "Fin", "Moyenne", "Écart-type"]
    table = table[colonnes]

    for col in ("Min", "Max", "Début", "Fin", "Moyenne", "Écart-type"):
        table[col] = table[col].round(2)

    columns = []
    for c in table.columns:
        columns.append({"name": c, "id": c})
    return table.to_dict("records"), columns


@app.callback(
    Output("corr-chart", "figure"),
    Input("stock-select", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
)
def corr_chart(ids, start, end):
    df = get_stocks(ids, start, end)
    if df.empty:
        return go.Figure()

    noms = df["name"].unique()
    if len(noms) < 2:
        return go.Figure()

    pivot = df.pivot_table(index="date", columns="name", values="close")
    corr = pivot.corr()
    corr = corr.round(2)

    noms_colonnes = corr.columns.tolist()
    noms_lignes = corr.index.tolist()
    valeurs = corr.values

    trace = go.Heatmap(
        z=valeurs,
        x=noms_colonnes,
        y=noms_lignes,
        colorscale="RdBu",
        zmin=-1,
        zmax=1,
        text=valeurs,
        texttemplate="%{text:.2f}",
        colorbar=dict(title="Corrélation"),
    )

    fig = go.Figure(data=[trace])
    fig.update_layout(
        title="Matrice de corrélation",
        margin=dict(l=50, r=30, t=50, b=40),
    )
    return fig


@app.callback(
    Output("stats-table", "data"),
    Output("stats-table", "columns"),
    Input("stock-select", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
)
def stats_table(ids, start, end):
    df = get_stocks(ids, start, end)
    if df.empty:
        return [], []

    rows = []
    for name, g in df.groupby("name"):
        g = g.sort_values("date")

        ret = g["close"].pct_change()
        ret = ret.dropna()
        if ret.empty:
            continue

        prix_debut = g["close"].iloc[0]
        prix_fin = g["close"].iloc[-1]
        rendement = (prix_fin / prix_debut - 1) * 100

        ecart_type = ret.std()
        volatilite = ecart_type * (252 ** 0.5) * 100

        cummax = g["close"].cummax()
        drawdown = (g["close"] - cummax) / cummax
        drawdown_max = drawdown.min() * 100

        meilleur = ret.max() * 100
        pire = ret.min() * 100

        rows.append({
            "Action": name,
            "Rendement (%)": round(rendement, 2),
            "Volatilité annualisée (%)": round(volatilite, 2),
            "Drawdown max (%)": round(drawdown_max, 2),
            "Meilleur jour (%)": round(meilleur, 2),
            "Pire jour (%)": round(pire, 2),
        })

    if not rows:
        return [], []

    columns = []
    for c in rows[0].keys():
        columns.append({"name": c, "id": c})
    return rows, columns


@app.callback(
    Output("returns-chart", "figure"),
    Input("stock-select", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
)
def returns_chart(ids, start, end):
    df = get_stocks(ids, start, end)
    if df.empty:
        return go.Figure()

    fig = go.Figure()
    for name, g in df.groupby("name"):
        g = g.sort_values("date")
        ret = g["close"].pct_change()
        ret = ret.dropna() * 100

        trace = go.Histogram(x=ret, name=name, opacity=0.7, nbinsx=60)
        fig.add_trace(trace)

    fig.update_layout(
        barmode="overlay",
        title="Distribution des rendements journaliers (%)",
        xaxis_title="Rendement (%)",
        yaxis_title="Fréquence",
        dragmode="pan",
        margin=dict(l=50, r=30, t=50, b=40),
    )
    return fig


def load_bourso():
    store_files("2019-01-01", "2023-12-31", "bourso", db, status=bourso_status)


if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    logging.getLogger("sqlalchemy").setLevel(logging.ERROR)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    store_files("2020-05-01", "2024-12-31", "euronext", db)

    app.layout = build_layout()

    threading.Thread(target=load_bourso, daemon=True).start()

    print("Dashboard disponible sur http://localhost:8050/")
    app.run(host="0.0.0.0", debug=False, port=8050)
