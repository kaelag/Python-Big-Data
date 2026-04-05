import os
import json
import threading
import warnings
import logging

import dash
import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go
from dash import ALL, Input, Output, State, dash_table, dcc, html

import timescaledb_model as tsdb
from etl import store_files

DB_HOST = os.environ.get("DB_HOST", "database")
db = tsdb.TimescaleStockMarketModel("bourse", "bourse", DB_HOST, "password")

app = dash.Dash(
    __name__,
    title="Bourse Dashboard",
    suppress_callback_exceptions=True,
    external_stylesheets=[dbc.themes.FLATLY],
)

bourso_status = {"total": 0, "done": 0, "finished": False}


def get_options():
    df = db.df_query("""
        SELECT DISTINCT c.id, c.name FROM companies c
        JOIN daystocks d ON d.cid = c.id
        ORDER BY c.name
    """)
    return [{"label": r["name"], "value": r["id"]} for _, r in df.iterrows()]


def get_stocks(stock_ids, start, end):
    if not stock_ids or not start or not end:
        return pd.DataFrame()
    ids = ",".join(str(i) for i in stock_ids)
    return db.df_query(f"""
        SELECT DISTINCT ON (d.date, d.cid)
               d.date, d.open, d.close, d.high, d.low,
               d.volume, d.mean, d.std, c.name
        FROM daystocks d
        JOIN companies c ON d.cid = c.id
        WHERE d.cid IN ({ids})
          AND d.date BETWEEN '{start}' AND '{end}'
        ORDER BY d.date, d.cid, d.volume DESC
    """)


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
            dbc.Progress(id="bourso-progress", value=0, striped=True, animated=True,
                         color="primary", style={"height": "5px"}),
        ], className="mb-4"),

        dbc.Row([
            dbc.Col([
                html.Label("Actions", className="small fw-semibold text-uppercase text-muted mb-1"),
                dcc.Dropdown(id="stock-select", options=opts, multi=True,
                             placeholder="Rechercher des actions...", className="stock-search"),
                html.Div(id="selected-tags", className="mt-2 d-flex flex-wrap gap-1"),
            ], md=6),
            dbc.Col([
                html.Label("Période", className="small fw-semibold text-uppercase text-muted mb-1"),
                dcc.DatePickerRange(id="date-range", start_date="2020-05-01",
                                    end_date="2020-12-31", display_format="YYYY-MM-DD"),
            ], md=4),
            dbc.Col([
                html.Label("Affichage", className="small fw-semibold text-uppercase text-muted mb-1"),
                dcc.RadioItems(id="chart-type",
                               options=[{"label": " Ligne", "value": "line"},
                                        {"label": " Chandeliers", "value": "candle"}],
                               value="line", inline=True,
                               inputStyle={"marginRight": "4px", "marginLeft": "10px"}),
            ], md=2, className="d-flex flex-column justify-content-end"),
        ], className="mb-4 g-3"),

        dbc.Tabs([
            dbc.Tab(label="Cours", tab_id="tab-cours", children=[
                dcc.Graph(id="price-chart", style={"height": "70vh"}),
            ]),
            dbc.Tab(label="Bollinger", tab_id="tab-bollinger", children=[
                dbc.Row([
                    dbc.Col([
                        html.Label("Action", className="small fw-semibold text-uppercase text-muted mt-3 mb-1"),
                        dcc.Dropdown(id="bollinger-stock", placeholder="Choisir une action..."),
                    ], md=4),
                ], className="mb-2"),
                dcc.Graph(id="bollinger-chart", style={"height": "65vh"}),
            ]),
            dbc.Tab(label="Données brutes", tab_id="tab-table", children=[
                dash_table.DataTable(
                    id="data-table", page_size=25,
                    style_table={"overflowX": "auto", "marginTop": "15px"},
                    style_cell={"textAlign": "center", "padding": "10px",
                                "fontSize": "13px", "fontFamily": "inherit",
                                "border": "1px solid #dee2e6"},
                    style_header={"fontWeight": "600", "backgroundColor": "#f8f9fa",
                                  "borderBottom": "2px solid #dee2e6", "fontSize": "13px"},
                    style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": "#fbfcfd"}],
                ),
            ]),
            dbc.Tab(label="Corrélation", tab_id="tab-corr", children=[
                dcc.Graph(id="corr-chart", style={"height": "70vh"}),
            ]),
            dbc.Tab(label="Statistiques", tab_id="tab-stats", children=[
                dash_table.DataTable(
                    id="stats-table", page_size=25,
                    style_table={"overflowX": "auto", "marginTop": "15px"},
                    style_cell={"textAlign": "center", "padding": "10px",
                                "fontSize": "13px", "fontFamily": "inherit",
                                "border": "1px solid #dee2e6"},
                    style_header={"fontWeight": "600", "backgroundColor": "#f8f9fa",
                                  "borderBottom": "2px solid #dee2e6", "fontSize": "13px"},
                    style_data_conditional=[
                        {"if": {"row_index": "odd"}, "backgroundColor": "#fbfcfd"},
                        {"if": {"filter_query": "{Rendement (%)} < 0", "column_id": "Rendement (%)"},
                         "color": "#e63757"},
                        {"if": {"filter_query": "{Rendement (%)} >= 0", "column_id": "Rendement (%)"},
                         "color": "#00d97e"},
                        {"if": {"filter_query": "{Drawdown max (%)} < 0", "column_id": "Drawdown max (%)"},
                         "color": "#e63757"},
                    ],
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
        msg = f"Boursorama chargé — {total:,} fichiers (100%)" if total > 0 else "Boursorama chargé"
        return opts, msg, 100, {"display": "none"}
    elif bourso_status["total"] > 0:
        pct = int(bourso_status["done"] / bourso_status["total"] * 100)
        msg = f"Chargement Boursorama : {bourso_status['done']:,} / {bourso_status['total']:,} ({pct}%)"
        return opts, msg, pct, {}
    return opts, "Chargement Boursorama...", 0, {}


@app.callback(
    Output("selected-tags", "children"),
    Input("stock-select", "value"),
    State("stock-select", "options"),
)
def show_tags(ids, opts):
    if not ids:
        return []
    labels = {o["value"]: o["label"] for o in (opts or [])}
    return [
        dbc.Badge(
            [labels.get(i, str(i)), html.Span(" ×", className="ms-1")],
            id={"type": "remove-stock", "index": i},
            color="primary", n_clicks=0,
            style={"cursor": "pointer", "fontSize": "12px", "padding": "5px 10px"},
        )
        for i in ids
    ]


@app.callback(
    Output("stock-select", "value"),
    Input({"type": "remove-stock", "index": ALL}, "n_clicks"),
    State("stock-select", "value"),
    prevent_initial_call=True,
)
def remove_stock(_, ids):
    ctx = dash.callback_context
    if not ctx.triggered or not ids or not ctx.triggered[0]["value"]:
        raise dash.exceptions.PreventUpdate
    removed = json.loads(ctx.triggered[0]["prop_id"].rsplit(".", 1)[0])["index"]
    return [i for i in ids if i != removed]


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
        g = df[df["name"] == df["name"].iloc[0]].sort_values("date")
        fig.add_trace(go.Candlestick(x=g["date"], open=g["open"], high=g["high"],
                                      low=g["low"], close=g["close"], name=g["name"].iloc[0]))
    else:
        for name, g in df.groupby("name"):
            fig.add_trace(go.Scatter(x=g["date"], y=g["close"], mode="lines", name=name))
        fig.update_yaxes(type="log")

    fig.update_layout(
        title="Cours des actions" + (" (échelle log)" if mode == "line" else ""),
        xaxis_rangeslider_visible=False,
        xaxis_rangebreaks=[dict(bounds=["sat", "mon"])],
        dragmode="pan", margin=dict(l=50, r=30, t=50, b=40),
    )
    return fig


@app.callback(
    Output("bollinger-stock", "options"),
    Output("bollinger-stock", "value"),
    Input("stock-select", "value"),
)
def bollinger_options(ids):
    if not ids:
        return [], None
    df = db.df_query(f"SELECT id, name FROM companies WHERE id IN ({','.join(str(i) for i in ids)})")
    opts = list(zip(df["name"], df["id"]))
    return [{"label": n, "value": i} for n, i in opts], ids[0]


@app.callback(
    Output("bollinger-chart", "figure"),
    Input("bollinger-stock", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
    Input("chart-type", "value"),
)
def bollinger_chart(stock_id, start, end, mode):
    df = get_stocks([stock_id], start, end)
    if len(df) < 20:
        return go.Figure()

    df = df.sort_values("date")
    df["sma"] = df["close"].rolling(20).mean()
    df["upper"] = df["sma"] + 2 * df["close"].rolling(20).std()
    df["lower"] = df["sma"] - 2 * df["close"].rolling(20).std()

    name = df["name"].iloc[0]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["date"], y=df["upper"], name="Bande sup",
                             line=dict(color="rgba(150,150,150,0.3)")))
    fig.add_trace(go.Scatter(x=df["date"], y=df["lower"], name="Bande inf",
                             line=dict(color="rgba(150,150,150,0.3)"),
                             fill="tonexty", fillcolor="rgba(200,200,200,0.2)"))
    fig.add_trace(go.Scatter(x=df["date"], y=df["sma"], name="SMA 20",
                             line=dict(dash="dash", color="orange")))

    if mode == "candle":
        fig.add_trace(go.Candlestick(x=df["date"], open=df["open"], high=df["high"],
                                      low=df["low"], close=df["close"], name=name))
    else:
        fig.add_trace(go.Scatter(x=df["date"], y=df["close"], name=name,
                                  line=dict(color="#2c7be5")))

    fig.update_layout(
        title=f"Bandes de Bollinger — {name}",
        xaxis_rangeslider_visible=False,
        xaxis_rangebreaks=[dict(bounds=["sat", "mon"])],
        dragmode="pan", margin=dict(l=50, r=30, t=50, b=40),
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

    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    table = df.rename(columns={
        "date": "Date", "name": "Action", "low": "Min", "high": "Max",
        "open": "Début", "close": "Fin", "mean": "Moyenne", "std": "Écart-type",
    })[["Date", "Action", "Min", "Max", "Début", "Fin", "Moyenne", "Écart-type"]]

    for col in ("Min", "Max", "Début", "Fin", "Moyenne", "Écart-type"):
        table[col] = table[col].round(2)

    return table.to_dict("records"), [{"name": c, "id": c} for c in table.columns]


@app.callback(
    Output("corr-chart", "figure"),
    Input("stock-select", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
)
def corr_chart(ids, start, end):
    df = get_stocks(ids, start, end)
    if df.empty or len(df["name"].unique()) < 2:
        return go.Figure()

    pivot = df.pivot_table(index="date", columns="name", values="close")
    corr = pivot.corr().round(2)

    fig = go.Figure(go.Heatmap(
        z=corr.values, x=corr.columns.tolist(), y=corr.index.tolist(),
        colorscale="RdBu", zmin=-1, zmax=1,
        text=corr.values, texttemplate="%{text:.2f}",
        colorbar=dict(title="Corrélation"),
    ))
    fig.update_layout(title="Matrice de corrélation", margin=dict(l=50, r=30, t=50, b=40))
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
        ret = g["close"].pct_change().dropna()
        if ret.empty:
            continue
        total = (g["close"].iloc[-1] / g["close"].iloc[0] - 1) * 100
        vol = ret.std() * (252 ** 0.5) * 100
        dd = ((g["close"] - g["close"].cummax()) / g["close"].cummax()).min() * 100
        rows.append({
            "Action": name,
            "Rendement (%)": round(total, 2),
            "Volatilité annualisée (%)": round(vol, 2),
            "Drawdown max (%)": round(dd, 2),
            "Meilleur jour (%)": round(ret.max() * 100, 2),
            "Pire jour (%)": round(ret.min() * 100, 2),
        })

    if not rows:
        return [], []
    return rows, [{"name": c, "id": c} for c in rows[0].keys()]


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
        ret = g.sort_values("date")["close"].pct_change().dropna() * 100
        fig.add_trace(go.Histogram(x=ret, name=name, opacity=0.7, nbinsx=60))

    fig.update_layout(
        barmode="overlay",
        title="Distribution des rendements journaliers (%)",
        xaxis_title="Rendement (%)", yaxis_title="Fréquence",
        dragmode="pan", margin=dict(l=50, r=30, t=50, b=40),
    )
    return fig


if __name__ == "__main__":
    warnings.filterwarnings("ignore")
    logging.getLogger("sqlalchemy").setLevel(logging.ERROR)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    store_files("2020-05-01", "2024-12-31", "euronext", db)

    app.layout = build_layout()

    threading.Thread(
        target=lambda: store_files("2019-01-01", "2023-12-31", "bourso", db, status=bourso_status),
        daemon=True
    ).start()

    print("Dashboard disponible sur http://localhost:8050/")
    app.run(host="0.0.0.0", debug=False, port=8050)
