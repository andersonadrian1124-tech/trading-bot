"""
Starter template: Claude-powered crypto/meme coin research bot.

What this does:
  - Defines a few "tools" (functions) Claude can call: price/candles,
    basic technical indicators, and recent news.
  - Runs the standard Anthropic tool-use loop: send message -> Claude may
    request tool calls -> you run them -> send results back -> repeat until
    Claude returns a final text answer.

What you need to fill in / install:
  pip install anthropic ccxt pandas pandas-ta requests

  Set environment variables:
    ANTHROPIC_API_KEY
    CRYPTOPANIC_API_KEY   (optional, for news - free tier available)

This is a CLI starter, not production code: no caching, no rate-limit
handling, no persistent watchlist scheduler. Treat it as scaffolding.
"""

import os
import json
import requests
import ccxt
import pandas as pd
import pandas_ta as ta
from anthropic import Anthropic

client = Anthropic()  # reads ANTHROPIC_API_KEY from env
exchange = ccxt.binance()

with open(os.path.join(os.path.dirname(__file__), "system_prompt.md")) as f:
    SYSTEM_PROMPT = f.read()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def get_price_candles(symbol: str, timeframe: str = "1h", limit: int = 100):
    """Fetch OHLCV candles from Binance via ccxt. symbol like 'BTC/USDT'."""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(
            ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "latest_close": float(df["close"].iloc[-1]),
            "pct_change_period": float(
                (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
            ),
            "candles": df.tail(30).to_dict(orient="records"),  # trim payload
        }
    except Exception as e:
        return {"error": str(e)}


def get_indicators(symbol: str, timeframe: str = "1h", limit: int = 200):
    """Compute RSI, MACD, and volume trend for a symbol."""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(
            ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["rsi"] = ta.rsi(df["close"], length=14)
        macd = ta.macd(df["close"])
        df = pd.concat([df, macd], axis=1)

        recent_vol = df["volume"].tail(10).mean()
        prior_vol = df["volume"].tail(50).head(40).mean()

        return {
            "symbol": symbol,
            "rsi_latest": float(df["rsi"].iloc[-1]),
            "macd_latest": float(df["MACD_12_26_9"].iloc[-1]),
            "macd_signal_latest": float(df["MACDs_12_26_9"].iloc[-1]),
            "volume_trend_pct": float((recent_vol / prior_vol - 1) * 100)
            if prior_vol
            else None,
        }
    except Exception as e:
        return {"error": str(e)}


def get_news(query: str, limit: int = 10):
    """Fetch recent news headlines from CryptoPanic."""
    api_key = os.environ.get("CRYPTOPANIC_API_KEY")
    if not api_key:
        return {"error": "CRYPTOPANIC_API_KEY not set"}
    try:
        resp = requests.get(
            "https://cryptopanic.com/api/v1/posts/",
            params={"auth_token": api_key, "currencies": query, "public": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        posts = resp.json().get("results", [])[:limit]
        return {
            "query": query,
            "headlines": [
                {"title": p["title"], "published_at": p["published_at"], "url": p["url"]}
                for p in posts
            ],
        }
    except Exception as e:
        return {"error": str(e)}


TOOL_FUNCTIONS = {
    "get_price_candles": get_price_candles,
    "get_indicators": get_indicators,
    "get_news": get_news,
}

TOOLS = [
    {
        "name": "get_price_candles",
        "description": "Get recent OHLCV price candles for a trading pair.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "e.g. 'BTC/USDT'"},
                "timeframe": {"type": "string", "description": "e.g. '1h', '15m', '1d'"},
                "limit": {"type": "integer", "description": "number of candles"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_indicators",
        "description": "Compute RSI, MACD, and volume trend for a trading pair.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "timeframe": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_news",
        "description": "Get recent news headlines for a coin (ticker or slug).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "e.g. 'BTC' or 'DOGE'"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool-use loop
# ---------------------------------------------------------------------------

def run_research(user_prompt: str, model: str = "claude-sonnet-4-6", max_turns: int = 6):
    messages = [{"role": "user", "content": user_prompt}]

    for _ in range(max_turns):
        response = client.messages.create(
            model=model,
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            # Final answer — return the text content
            return "".join(
                block.text for block in response.content if block.type == "text"
            )

        # Claude wants to call one or more tools
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                func = TOOL_FUNCTIONS.get(block.name)
                result = func(**block.input) if func else {"error": "unknown tool"}
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    }
                )

        messages.append({"role": "user", "content": tool_results})

    return "Max turns reached without a final answer."


if __name__ == "__main__":
    query = input("What do you want researched? (e.g. 'analyze BTC/USDT right now'): ")
    print("\n--- Researching... ---\n")
    print(run_research(query))
