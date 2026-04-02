import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

COLORS = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]


def create_line_figure(df: pd.DataFrame, stock_name: str) -> go.Figure:
    """
    Affiche le cours d'une action en échelle logarithmique (ligne).

    df : colonnes date, open, close, high, low
    """
    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=df["date"],
        y=df["close"],
        mode="lines",
        name=stock_name,
        line=dict(color=COLORS[0], width=2),
    ))

    fig.update_layout(
        title=f"Cours de {stock_name}",
        xaxis_title="Date",
        yaxis_title="Prix (€)",
        yaxis_type="log",
        hovermode="x unified",
        template="plotly_white",
    )
    return fig


def create_candlestick_figure(df: pd.DataFrame, stock_name: str) -> go.Figure:
    """
    Affiche le cours d'une action en chandeliers japonais (log scale).

    df : colonnes date, open, close, high, low
    """
    fig = go.Figure()

    fig.add_trace(go.Candlestick(
        x=df["date"],
        open=df["open"],
        high=df["high"],
        low=df["low"],
        close=df["close"],
        name=stock_name,
        increasing_line_color="#2ca02c",
        decreasing_line_color="#d62728",
    ))

    fig.update_layout(
        title=f"Cours de {stock_name} (chandeliers)",
        xaxis_title="Date",
        yaxis_title="Prix (€)",
        yaxis_type="log",
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        template="plotly_white",
    )
    return fig


def create_multi_stock_figure(dfs: list[tuple[str, pd.DataFrame]]) -> go.Figure:
    """
    Affiche plusieurs actions sur le même graphique, une couleur par action.

    dfs : liste de tuples (nom_action, dataframe avec colonnes date, close)
    """
    fig = go.Figure()

    for i, (name, df) in enumerate(dfs):
        color = COLORS[i % len(COLORS)]
        fig.add_trace(go.Scatter(
            x=df["date"],
            y=df["close"],
            mode="lines",
            name=name,
            line=dict(color=color, width=2),
        ))

    fig.update_layout(
        title="Comparaison de cours",
        xaxis_title="Date",
        yaxis_title="Prix (€)",
        yaxis_type="log",
        hovermode="x unified",
        template="plotly_white",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
        ),
    )
    return fig


def create_bollinger_figure(df: pd.DataFrame, stock_name: str, window: int = 20) -> go.Figure:
    """
    Affiche les bandes de Bollinger pour une action.

    df     : colonnes date, close
    window : fenêtre de la moyenne mobile (défaut 20 jours)
    """
    df = df.copy().sort_values("date")

    df["ma"] = df["close"].rolling(window=window).mean()
    df["std"] = df["close"].rolling(window=window).std()
    df["upper"] = df["ma"] + 2 * df["std"]
    df["lower"] = df["ma"] - 2 * df["std"]

    fig = go.Figure()

    # Cours
    fig.add_trace(go.Scatter(
        x=df["date"],
        y=df["close"],
        mode="lines",
        name="Cours",
        line=dict(color=COLORS[0], width=2),
    ))

    # Bande supérieure
    fig.add_trace(go.Scatter(
        x=df["date"],
        y=df["upper"],
        mode="lines",
        name="Bande haute",
        line=dict(color="#ff7f0e", width=1, dash="dash"),
    ))

    # Bande inférieure + remplissage entre les deux bandes
    fig.add_trace(go.Scatter(
        x=df["date"],
        y=df["lower"],
        mode="lines",
        name="Bande basse",
        line=dict(color="#ff7f0e", width=1, dash="dash"),
        fill="tonexty",
        fillcolor="rgba(255, 127, 14, 0.1)",
    ))

    # Moyenne mobile
    fig.add_trace(go.Scatter(
        x=df["date"],
        y=df["ma"],
        mode="lines",
        name=f"Moyenne mobile ({window}j)",
        line=dict(color="#9467bd", width=1, dash="dot"),
    ))

    fig.update_layout(
        title=f"Bandes de Bollinger — {stock_name} (fenêtre {window} jours)",
        xaxis_title="Date",
        yaxis_title="Prix (€)",
        hovermode="x unified",
        template="plotly_white",
    )
    return fig


def create_raw_table(df: pd.DataFrame, stock_name: str) -> go.Figure:
    """
    Affiche un tableau de données brutes agrégées par jour.

    df : colonnes date, open, close, high, low, mean, std
    """
    df = df.copy().sort_values("date", ascending=False)

    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

    for col in ["open", "close", "high", "low", "mean", "std"]:
        if col in df.columns:
            df[col] = df[col].round(2)

    fig = go.Figure(data=[go.Table(
        header=dict(
            values=["Date", "Ouverture", "Clôture", "Max", "Min", "Moyenne", "Écart-type"],
            fill_color="#1f77b4",
            font=dict(color="white", size=13),
            align="center",
            height=30,
        ),
        cells=dict(
            values=[
                df["date"],
                df["open"],
                df["close"],
                df["high"],
                df["low"],
                df["mean"],
                df["std"],
            ],
            fill_color=[["#f0f4ff" if i % 2 == 0 else "white" for i in range(len(df))]],
            align="center",
            height=25,
        ),
    )])

    fig.update_layout(
        title=f"Données brutes — {stock_name}",
        template="plotly_white",
    )
    return fig


def create_volume_figure(df: pd.DataFrame, stock_name: str) -> go.Figure:
    """
    Fonctionnalité bonus : affiche le cours + le volume en sous-graphe.

    df : colonnes date, close, volume
    """
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.7, 0.3],
        vertical_spacing=0.05,
        subplot_titles=(f"Cours de {stock_name}", "Volume"),
    )

    # Cours
    fig.add_trace(
        go.Scatter(
            x=df["date"],
            y=df["close"],
            mode="lines",
            name="Cours",
            line=dict(color=COLORS[0], width=2),
        ),
        row=1, col=1,
    )

    # Volume (barres colorées selon hausse/baisse)
    colors_vol = [
        "#2ca02c" if c >= o else "#d62728"
        for c, o in zip(df["close"], df["open"])
    ]
    fig.add_trace(
        go.Bar(
            x=df["date"],
            y=df["volume"],
            name="Volume",
            marker_color=colors_vol,
            opacity=0.7,
        ),
        row=2, col=1,
    )

    fig.update_yaxes(type="log", row=1, col=1, title_text="Prix (€)")
    fig.update_yaxes(row=2, col=1, title_text="Volume")
    fig.update_xaxes(row=2, col=1, title_text="Date")

    fig.update_layout(
        hovermode="x unified",
        template="plotly_white",
        showlegend=False,
    )
    return fig
