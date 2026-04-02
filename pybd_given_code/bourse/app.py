import os
import dash
import dash_bootstrap_components as dbc
from dash import html, dcc, dash_table, Input, Output
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
    external_stylesheets=[dbc.themes.BOOTSTRAP],
)


def get_company_options():
    df = db.df_query("""
        SELECT DISTINCT c.id, c.name FROM companies c
        JOIN daystocks d ON d.cid = c.id
        ORDER BY c.name
    """)
    return [{"label": r["name"], "value": r["id"]} for _, r in df.iterrows()]


def build_layout():
    options = get_company_options()
    return dbc.Container([
        html.H2("Bourse Dashboard", className="mt-3 mb-4"),

        dbc.Row([
            dbc.Col([
                html.Label("Actions"),
                dcc.Dropdown(
                    id="stock-select", options=options,
                    multi=True, placeholder="Choisir des actions...",
                ),
            ], md=5),
            dbc.Col([
                html.Label("Periode"),
                dcc.DatePickerRange(
                    id="date-range",
                    start_date="2020-05-01",
                    end_date="2020-12-31",
                    display_format="YYYY-MM-DD",
                ),
            ], md=4),
            dbc.Col([
                html.Label("Type"),
                dcc.RadioItems(
                    id="chart-type",
                    options=[
                        {"label": " Ligne", "value": "line"},
                        {"label": " Chandeliers", "value": "candle"},
                    ],
                    value="line", inline=True,
                ),
            ], md=3),
        ], className="mb-4"),

        dbc.Tabs([
            dbc.Tab(label="Cours", tab_id="tab-cours", children=[
                dcc.Graph(id="price-chart", style={"height": "75vh"}),
            ]),
            dbc.Tab(label="Bollinger", tab_id="tab-bollinger", children=[
                dbc.Row([
                    dbc.Col([
                        html.Label("Action pour Bollinger", className="mt-3"),
                        dcc.Dropdown(id="bollinger-stock", placeholder="Choisir une action..."),
                    ], md=4),
                ], className="mb-2"),
                dcc.Graph(id="bollinger-chart", style={"height": "70vh"}),
            ]),
            dbc.Tab(label="Donnees brutes", tab_id="tab-table", children=[
                html.Div([
                    dash_table.DataTable(
                        id="data-table",
                        page_size=25,
                        style_table={"overflowX": "auto", "marginTop": "15px"},
                        style_cell={"textAlign": "center", "padding": "8px", "fontSize": "14px"},
                        style_header={"fontWeight": "bold", "backgroundColor": "#f8f9fa"},
                    ),
                ]),
            ]),
            dbc.Tab(label="Performance (base 100)", tab_id="tab-perf", children=[
                dcc.Graph(id="perf-chart", style={"height": "75vh"}),
            ]),
        ], id="tabs", active_tab="tab-cours", className="mb-3"),

    ], fluid=True)


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
        title="Cours des actions" + (" (echelle log)" if chart_type == "line" else ""),
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
            line=dict(color="blue"),
        ))

    fig.update_layout(
        title=f"Bandes de Bollinger - {name}",
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
        "open": "Debut", "close": "Fin",
        "mean": "Moyenne", "std": "Ecart-type",
    })[["Date", "Action", "Min", "Max", "Debut", "Fin", "Moyenne", "Ecart-type"]]

    for col in ("Min", "Max", "Debut", "Fin", "Moyenne", "Ecart-type"):
        table[col] = table[col].round(2)

    columns = [{"name": c, "id": c} for c in table.columns]
    return table.to_dict("records"), columns


@app.callback(
    Output("perf-chart", "figure"),
    Input("stock-select", "value"),
    Input("date-range", "start_date"),
    Input("date-range", "end_date"),
)
def update_perf(stock_ids, start, end):
    df = query_daystocks(stock_ids, start, end)
    if df.empty:
        return go.Figure()

    fig = go.Figure()
    for name, grp in df.groupby("name"):
        grp = grp.sort_values("date")
        base = grp["close"].iloc[0]
        if base and base > 0:
            fig.add_trace(go.Scatter(
                x=grp["date"],
                y=(grp["close"] / base) * 100,
                mode="lines", name=name,
            ))

    fig.add_hline(y=100, line_dash="dash", line_color="gray")
    fig.update_layout(
        title="Performance comparee (base 100)",
        xaxis_title="Date", yaxis_title="Performance",
        dragmode="pan",
        margin=dict(l=50, r=30, t=50, b=40),
    )
    return fig


if __name__ == "__main__":
    import logging, warnings

    logger.info("Chargement des donnees euronext...")
    store_files("2020-05-01", "2024-12-31", "euronext", db)

    app.layout = build_layout()

    logger.info("Dashboard pret sur http://0.0.0.0:8050/")

    warnings.filterwarnings("ignore")
    logger.remove()
    logging.getLogger("sqlalchemy").setLevel(logging.ERROR)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    app.run(host="0.0.0.0", debug=False, port=8050)
