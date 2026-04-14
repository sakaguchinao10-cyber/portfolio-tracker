#!/usr/bin/env python3
"""
朝のアラート APIサーバー
ポートフォリオ管理フロントエンドと連携するためのFastAPI HTTPサーバー

起動方法:
  cd alerts
  pip install -r requirements.txt
  python api_server.py

エンドポイント:
  GET  /api/morning-alert        最新のアラートデータを返す (キャッシュ or 新規分析)
  POST /api/run-analysis         今すぐ分析を実行
  GET  /api/status               サーバー状態確認
"""

import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

load_dotenv()

from stock_morning_alert import StockMorningAlert, SECTOR_JP, OUTPUT_JSON

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# 分析実行中フラグ (同時に複数実行しないよう制御)
_running = False
_last_result: dict | None = None


def _load_cached() -> dict | None:
    """保存済みJSONがあれば読み込む"""
    if OUTPUT_JSON.exists():
        try:
            with open(OUTPUT_JSON, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _run_analysis_sync() -> dict:
    """分析を同期実行してキャッシュに保存"""
    global _running, _last_result
    _running = True
    try:
        engine = StockMorningAlert()
        results, us_data = engine.analyze()
        engine.save_json(results, us_data)
        engine.send_email(engine.format_alert(results, us_data))
        engine.send_line(engine.format_alert(results, us_data))
        _last_result = _load_cached()
        return _last_result or {}
    finally:
        _running = False


# ─── スケジューラー (起動時に07:00登録) ───────────────────────

import schedule
import threading
import time as _time

def _scheduler_thread():
    alert_time = os.getenv("ALERT_TIME", "07:00")
    logger.info(f"スケジューラー起動: 毎日 {alert_time} に分析実行")
    schedule.every().day.at(alert_time).do(_run_analysis_sync)
    while True:
        schedule.run_pending()
        _time.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 起動時にキャッシュ読み込み & スケジューラー開始
    global _last_result
    _last_result = _load_cached()
    thread = threading.Thread(target=_scheduler_thread, daemon=True)
    thread.start()
    yield
    # シャットダウン処理は不要


app = FastAPI(
    title="日本株 朝のアラート API",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS設定 (フロントエンドからのアクセスを許可)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ─── エンドポイント ───────────────────────────────────────────────

@app.get("/api/status")
async def status():
    return {
        "status":     "running",
        "analyzing":  _running,
        "has_cache":  _last_result is not None,
        "cache_time": _last_result.get("generated_at") if _last_result else None,
        "alert_time": os.getenv("ALERT_TIME", "07:00"),
        "server_time": datetime.now().isoformat(),
    }


@app.get("/api/morning-alert")
async def get_morning_alert():
    """
    最新のアラートデータを返す。
    キャッシュがない場合は分析を即時実行する。
    """
    if _running:
        raise HTTPException(status_code=202, detail="分析実行中です。しばらくお待ちください。")

    data = _last_result or _load_cached()
    if data:
        return JSONResponse(content=data)

    # キャッシュなし → 初回分析を実行
    logger.info("キャッシュなし — 初回分析を実行します")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _run_analysis_sync)
    return JSONResponse(content=result)


@app.post("/api/run-analysis")
async def run_analysis(background_tasks: BackgroundTasks):
    """今すぐ分析を実行 (バックグラウンド)"""
    if _running:
        raise HTTPException(status_code=409, detail="分析は既に実行中です")

    background_tasks.add_task(_run_analysis_sync)
    return {"message": "分析を開始しました。/api/morning-alert で結果を確認できます。"}


# ─── 起動 ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("API_PORT", "8000"))
    logger.info(f"APIサーバー起動 → http://localhost:{port}")
    uvicorn.run("api_server:app", host="0.0.0.0", port=port, reload=False)
