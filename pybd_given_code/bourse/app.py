import os
import threading
import dash
import dash_bootstrap_components as dbc
import json
from dash import html, dcc, dash_table, Input, Output, State, ALL
import plotly.graph_objects as go
import pandas as pd
from loguru import logger

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


def build_layout():
    options = get_options()
    return dbc.Container([

        dbc.Row([
            dbc.Col([
                html.H4("Bourse Dashboard", className="fw-bold mb-0 text-primary"),
                html.Small("Données Euronext + Boursorama", className="text-muted"),
            ], className="py-3"),
        ], className="border-bottom mb-3"),

        html.Div(id="loading-section", children=[
            html.Small(id="bourso-status", className="text-muted d-block mb-1"),
            dbc.Progress(
                id="bourso-progress", value=0, striped=True, animated=True,
                color="primary", style={"height": "5px"},
            ),
        ], className="mb-4"),

        dbc.Row([
            dbc.Col([
                html.Label("Actions", className="small fw-semibold text-uppercase text-muted mb-1"),
                dcc.Dropdown(
                    id="stock-select", options=options, multi=True,
                    placeholder="Rechercher des actions...",
                    className="stock-search",
                ),
                html.Div(id="selected-tags", className="mt-2 d-flex flex-wrap gap-1"),
            ], md=6),
            dbc.Col([
                html.Label("Période", className="small fw-semibold text-uppercase text-muted mb-1"),
                dcc.DatePickerRange(
                    id="date-range",
                    start_date="2020-05-01",
                    end_date="2020-12-31",
                    display_format="YYYY-MM-DD",
                ),
            ], md=4),
            dbc.Col([
                html.Label("Affichage", className="small fw-semibold text-uppercase text-muted mb-1"),
                dcc.RadioItems(
                    id="chart-type",
                    options=[
                        {"label": " Ligne", "value": "line"},
                        {"label": " Chandeliers", "value": "candle"},
                    ],
                    value="line", inline=True,
                    inputStyle={"marginRight": "4px", "marginLeft": "10px"},
                ),
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
                    id="data-table",
                    page_size=25,
                    style_table={"overflowX": "auto", "marginTop": "15px"},
                    style_cell={
                        "textAlign": "center", "padding": "10px",
                        "fontSize": "13px", "fontFamily": "inherit",
                        "border": "1px solid #dee2e6",
                    },
                    style_header={
                        "fontWeight": "600", "backgroundColor": "#f8f9fa",
                        "borderBottom": "2px solid #dee2e6", "fontSize": "13px",
                    },
                    style_data_conditional=[
                        {"if": {"row_index": "odd"}, "backgroundColor": "#fbfcfd"},
                    ],
                ),
            ]),

            dbc.Tab(label="Corrélation", tab_id="tab-corr", children=[
                dcc.Graph(id="corr-chart", style={"height": "70vh"}),
            ]),
            dbc.Tab(label="Statistiques", tab_id="tab-stats", children=[
                dash_table.DataTable(
                    id="stats-table",
                    page_size=25,
                    style_table={"overflowX": "auto", "marginTop": "15px"},
                    style_cell={
                        "textAlign": "center", "padding": "10px",
                        "fontSize": "13px", "fontFamily": "inherit",
                        "border": "1px solid #dee2e6",
                    },
                    style_header={
                        "fontWeight": "600", "backgroundColor": "#f8f9fa",
                        "borderBottom": "2px solid #dee2e6", "fontSize": "13px",
                    },
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
    options = get_options()

    if bourso_status["finished"]:
        total = bourso_status["total"]
        status = f"Boursorama chargé — {total:,} fichiers (100%)" if total > 0 else "Boursorama chargé"
        return options, status, 100, {"display": "none"}
    elif bourso_status["total"] > 0:
        pct = int(bourso_status["done"] / bourso_status["total"] * 100)
        status = f"Chargement Boursorama : {bourso_status['done']:,} / {bourso_status['total']:,} ({pct}%)"
        return options, status, pct, {}
    else:
        return options, "Chargement Boursorama...", 0, {}


@app.callback(
    Output("selected-tags", "children"),
    Input("stock-select", "value"),
    State("stock-select", "options"),
)
def render_tags(stock_ids, options):
    if not stock_ids:
        return []
    opt_map = {o["value"]: o["label"] for o in (options or [])}
    return [
        dbc.Badge(
            [opt_map.get(sid, str(sid)), html.Span(" ×", className="ms-1")],
            id={"type": "remove-stock", "index": sid},
            color="primary", n_clicks=0,
            style={"cursor": "pointer", "fontSize": "12px", "padding": "5px 10px"},
        )
        for sid in stock_ids
    ]


@app.callback(
    Output("stock-select", "value"),
    Input({"type": "remove-stock", "index": ALL}, "n_clicks"),
    State("stock-select", "value"),
    prevent_initial_call=True,
)
def remove_stock(n_clicks_list, current_ids):
    ctx = dash.callback_context
    if not ctx.triggered or not current_ids:
        raise dash.exceptions.PreventUpdate
    if not ctx.triggered[0]["value"]:
        raise dash.exceptions.PreventUpdate
    stock_id = json.loads(ctx.triggered[0]["prop_id"].rsplit(".", 1)[0])["index"]
    return [sid for sid in current_ids if sid != stock_id]


def query_daystocks(stock_ids, start, end):
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


@app.callback(
    Output("price-chart", "figure"),
    Input("stock-select", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
    Input("chart-type", "value"),
)
def update_price_chart(stock_ids, start, end, chart_type):
    df = query_daystocks(stock_ids, start, end)
    if df.empty:
        return go.Figure()

    fig = go.Figure()

    if chart_type == "candle":
        first = query_daystocks([stock_ids[0]], start, end)
        name = first["name"].iloc[0]
        fig.add_trace(go.Candlestick(
            x=first["date"], open=first["open"], high=first["high"],
            low=first["low"], close=first["close"], name=name,
        ))
    else:
        for name, grp in df.groupby("name"):
            fig.add_trace(go.Scatter(
                x=grp["date"], y=grp["close"],
                mode="lines", name=name,
            ))

    if chart_type == "line":
        fig.update_yaxes(type="log")

    fig.update_layout(
        title="Cours des actions" + (" (échelle log)" if chart_type == "line" else ""),
        xaxis_title="Date", yaxis_title="Prix",
        xaxis_rangeslider_visible=False,
        xaxis_rangebreaks=[dict(bounds=["sat", "mon"])],
        dragmode="pan",
        margin=dict(l=50, r=30, t=50, b=40),
    )
    return fig


@app.callback(
    Output("bollinger-stock", "options"),
    Output("bollinger-stock", "value"),
    Input("stock-select", "value"),
)
def update_bollinger_options(stock_ids):
    if not stock_ids:
        return [], None
    ids = ",".join(str(i) for i in stock_ids)
    df = db.df_query(f"SELECT id, name FROM companies WHERE id IN ({ids})")
    opts = [{"label": r["name"], "value": r["id"]} for _, r in df.iterrows()]
    return opts, stock_ids[0]


@app.callback(
    Output("bollinger-chart", "figure"),
    Input("bollinger-stock", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
    Input("chart-type", "value"),
)
def update_bollinger(stock_id, start, end, chart_type):
    if not stock_id or not start or not end:
        return go.Figure()

    df = query_daystocks([stock_id], start, end)
    if len(df) < 20:
        return go.Figure()

    df = df.sort_values("date")
    df["sma20"] = df["close"].rolling(20).mean()
    df["std20"] = df["close"].rolling(20).std()
    df["upper"] = df["sma20"] + 2 * df["std20"]
    df["lower"] = df["sma20"] - 2 * df["std20"]

    name = df["name"].iloc[0]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["date"], y=df["upper"], name="Bande sup",
        line=dict(color="rgba(150,150,150,0.3)"),
    ))
    fig.add_trace(go.Scatter(
        x=df["date"], y=df["lower"], name="Bande inf",
        line=dict(color="rgba(150,150,150,0.3)"),
        fill="tonexty", fillcolor="rgba(200,200,200,0.2)",
    ))
    fig.add_trace(go.Scatter(
        x=df["date"], y=df["sma20"], name="SMA 20",
        line=dict(dash="dash", color="orange"),
    ))

    if chart_type == "candle":
        fig.add_trace(go.Candlestick(
            x=df["date"], open=df["open"], high=df["high"],
            low=df["low"], close=df["close"], name=name,
        ))
    else:
        fig.add_trace(go.Scatter(
            x=df["date"], y=df["close"], name=name,
            line=dict(color="#2c7be5"),
        ))

    fig.update_layout(
        title=f"Bandes de Bollinger — {name}",
        dragmode="pan",
        xaxis_rangeslider_visible=False,
        xaxis_rangebreaks=[dict(bounds=["sat", "mon"])],
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
def update_table(stock_ids, start, end):
    df = query_daystocks(stock_ids, start, end)
    if df.empty:
        return [], []

    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    table = df.rename(columns={
        "date": "Date", "name": "Action",
        "low": "Min", "high": "Max",
        "open": "Début", "close": "Fin",
        "mean": "Moyenne", "std": "Écart-type",
    })[["Date", "Action", "Min", "Max", "Début", "Fin", "Moyenne", "Écart-type"]]

    for col in ("Min", "Max", "Début", "Fin", "Moyenne", "Écart-type"):
        table[col] = table[col].round(2)

    columns = [{"name": c, "id": c} for c in table.columns]
    return table.to_dict("records"), columns



@app.callback(
    Output("corr-chart", "figure"),
    Input("stock-select", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
)
def update_corr(stock_ids, start, end):
    df = query_daystocks(stock_ids, start, end)
    if df.empty or len(df["name"].unique()) < 2:
        return go.Figure()

    pivot = df.pivot_table(index="date", columns="name", values="close")
    corr = pivot.corr().round(2)
    fig = go.Figure(go.Heatmap(
        z=corr.values,
        x=corr.columns.tolist(),
        y=corr.index.tolist(),
        colorscale="RdBu",
        zmin=-1, zmax=1,
        text=corr.values,
        texttemplate="%{text:.2f}",
        colorbar=dict(title="Corrélation"),
    ))
    fig.update_layout(
        title="Matrice de corrélation des rendements",
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
def update_stats(stock_ids, start, end):
    df = query_daystocks(stock_ids, start, end)
    if df.empty:
        return [], []

    rows = []
    for name, grp in df.groupby("name"):
        grp = grp.sort_values("date")
        returns = grp["close"].pct_change().dropna()
        if returns.empty:
            continue
        total_return = (grp["close"].iloc[-1] / grp["close"].iloc[0] - 1) * 100
        vol = returns.std() * (252 ** 0.5) * 100
        cummax = grp["close"].cummax()
        drawdown = ((grp["close"] - cummax) / cummax).min() * 100
        rows.append({
            "Action": name,
            "Rendement (%)": round(total_return, 2),
            "Volatilité annualisée (%)": round(vol, 2),
            "Drawdown max (%)": round(drawdown, 2),
            "Meilleur jour (%)": round(returns.max() * 100, 2),
            "Pire jour (%)": round(returns.min() * 100, 2),
        })

    if not rows:
        return [], []
    columns = [{"name": c, "id": c} for c in rows[0].keys()]
    return rows, columns


@app.callback(
    Output("returns-chart", "figure"),
    Input("stock-select", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
)
def update_returns(stock_ids, start, end):
    df = query_daystocks(stock_ids, start, end)
    if df.empty:
        return go.Figure()

    fig = go.Figure()
    for name, grp in df.groupby("name"):
        returns = grp.sort_values("date")["close"].pct_change().dropna() * 100
        fig.add_trace(go.Histogram(
            x=returns, name=name, opacity=0.7, nbinsx=60,
        ))

    fig.update_layout(
        barmode="overlay",
        title="Distribution des rendements journaliers (%)",
        xaxis_title="Rendement journalier (%)",
        yaxis_title="Fréquence",
        dragmode="pan",
        margin=dict(l=50, r=30, t=50, b=40),
    )
    return fig


def _load_bourso_background():
    store_files("2019-01-01", "2023-12-31", "bourso", db, status=bourso_status)


if __name__ == "__main__":
    import logging, warnings

    logger.info("Chargement des données Euronext...")
    store_files("2020-05-01", "2024-12-31", "euronext", db)

    app.layout = build_layout()

    logger.info("Dashboard prêt sur http://0.0.0.0:8050/")

    warnings.filterwarnings("ignore")
    logger.remove()
    logging.getLogger("sqlalchemy").setLevel(logging.ERROR)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    threading.Thread(target=_load_bourso_background, daemon=True).start()

    app.run(host="0.0.0.0", debug=False, port=8050)
