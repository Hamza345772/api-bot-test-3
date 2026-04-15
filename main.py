from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import random
import math
from datetime import datetime, timezone, timedelta
from typing import List, Dict
import uvicorn
from tv_data_fetcher import TradingViewDataFetcher
from tradingview_ta import Interval

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

FOREX_PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD",
    "USDCHF", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY",
    "AUDJPY", "EURAUD", "EURCAD", "CADCHF", "GBPCHF",
    "AUDCAD", "NZDJPY", "EURCHF", "GBPAUD"
]

fetcher = TradingViewDataFetcher()
price_data: Dict[str, List[float]] = {}
stats_store = {"total": 0, "wins": 0, "losses": 0}
active_signals: Dict[str, dict] = {}
connected_clients: List[WebSocket] = []

def is_weekend():
    now = datetime.now()
    return now.weekday() >= 5

async def update_market_data():
    """Background task to fetch real data from TradingView API"""
    while True:
        if is_weekend():
            await asyncio.sleep(60)
            continue
            
        for symbol in FOREX_PAIRS:
            try:
                analysis = fetcher.get_analysis(symbol, "FX_IDC", "forex", Interval.INTERVAL_1_MINUTE)
                if analysis and 'close' in analysis.indicators:
                    price = analysis.indicators['close']
                    if symbol not in price_data:
                        price_data[symbol] = []
                    price_data[symbol].append(price)
                    if len(price_data[symbol]) > 100: # Reduced history requirement
                        price_data[symbol].pop(0)
            except Exception as e:
                print(f"Error updating data for {symbol}: {e}")
        
        await asyncio.sleep(15)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(update_market_data())

def compute_rsi(prices: List[float], period: int = 14) -> float:
    if len(prices) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = prices[-i] - prices[-i - 1]
        if diff >= 0: gains.append(diff); losses.append(0)
        else: gains.append(0); losses.append(abs(diff))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def compute_ema(prices: List[float], period: int) -> float:
    if len(prices) < period: return prices[-1]
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 6)

def analyze_pair(pair: str, duration: int) -> dict:
    if is_weekend():
        return {"status": "weekend", "message": "Market is closed."}
        
    # Try to get fresh analysis directly from API for immediate results
    try:
        analysis = fetcher.get_analysis(pair, "FX_IDC", "forex", Interval.INTERVAL_1_MINUTE)
        if not analysis: return None
        
        indicators = analysis.indicators
        summary = analysis.summary
        
        # Use API's built-in summary for faster signals
        # summary['RECOMMENDATION'] can be 'BUY', 'STRONG_BUY', 'SELL', 'STRONG_SELL', 'NEUTRAL'
        rec = summary.get('RECOMMENDATION', 'NEUTRAL')
        
        if 'NEUTRAL' in rec:
            return None

        direction = "UP" if "BUY" in rec else "DOWN"
        
        # Calculate confidence based on recommendation strength
        confidence = 65.0
        if "STRONG" in rec:
            confidence = 85.0
        
        # Add some randomness to confidence for variety
        confidence += random.uniform(2, 8)
        confidence = min(98, confidence)

        reasons = []
        if "STRONG" in rec: reasons.append("Strong Market Momentum")
        else: reasons.append("Market Trend Alignment")
        
        # Add specific indicator reasons if available
        rsi = indicators.get('RSI')
        if rsi:
            if rsi < 35: reasons.append("RSI Oversold")
            elif rsi > 65: reasons.append("RSI Overbought")
        
        macd = indicators.get('MACD.macd')
        signal = indicators.get('MACD.signal')
        if macd and signal:
            if macd > signal: reasons.append("MACD Bullish Cross")
            else: reasons.append("MACD Bearish Cross")

        signals_count = len(reasons) + (2 if "STRONG" in rec else 1)
        probability_score = round(confidence + (signals_count * 0.5), 2)

        now = datetime.now()
        entry_in = random.randint(3, 12) # Faster entry
        entry_time = datetime.fromtimestamp(now.timestamp() + entry_in)

        return {
            "pair": pair,
            "direction": direction,
            "confidence": round(confidence, 1),
            "probability_score": probability_score,
            "reasons": reasons[:5],
            "signals_count": signals_count,
            "entry_in": entry_in,
            "entry_time": entry_time.strftime("%H:%M:%S"),
            "duration": duration,
            "status": "ready",
            "id": f"{pair}-{int(now.timestamp())}",
            "timestamp": now.isoformat()
        }
    except Exception as e:
        print(f"Error in analyze_pair for {pair}: {e}")
        return None

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/api/pairs")
async def get_pairs():
    if is_weekend():
        return {"pairs": [], "status": "weekend", "message": "Market is closed."}
    return {"pairs": FOREX_PAIRS, "status": "open"}

@app.get("/api/stats")
async def get_stats():
    total = stats_store["total"]
    wins = stats_store["wins"]
    acc = round((wins / total * 100), 1) if total > 0 else 0
    return {**stats_store, "accuracy": acc}

@app.post("/api/result")
async def post_result(data: dict):
    res = data.get("result")
    if res == "win": stats_store["wins"] += 1
    elif res == "loss": stats_store["losses"] += 1
    stats_store["total"] += 1
    return {"status": "ok"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            
            if is_weekend():
                await websocket.send_text(json.dumps({"type": "error", "message": "Market is closed."}))
                continue

            if msg.get("type") == "analyze":
                pair = msg.get("pair")
                duration = msg.get("duration", 60)
                result = analyze_pair(pair, duration)
                if result:
                    await websocket.send_text(json.dumps({"type": "signal", "data": result}))
                else:
                    # Send a heartbeat to keep connection alive even if no signal
                    await websocket.send_text(json.dumps({"type": "heartbeat", "pair": pair}))
    except WebSocketDisconnect:
        connected_clients.remove(websocket)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
