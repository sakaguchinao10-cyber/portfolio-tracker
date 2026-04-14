#!/usr/bin/env python3
"""
朝のアラート APIサーバー
ポートフォリオ管理フロントエンドと連携するためのFastAPI HTTPサーバー

起動方法:
  cd alerts
  pip install -r requirements.txt

  # 通常起動 (LAN内スマホからアクセス可)
  python api_server.py

  # ngrokでインターネット公開 (どこからでもアクセス可)
  pip install pyngrok
  python api_server.py --ngrok

  # Cloudflare Tunnel (無料・高速・インストール不要)
  cloudflared tunnel --url http://localhost:8000

エンドポイント:
  GET  /                         アプリ本体 (index.html)
  GET  /api/morning-alert        最新のアラートデータを返す
  POST /api/run-analysis         今すぐ分析を実行
  GET  /api/status               サーバー状態確認
"""

import os
import sys
import json
import asyncio
import logging
import socket
import threading
import time as _time
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager

import schedule
import uvicorn
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from dotenv import load_dotenv

load_dotenv()

from stock_morning_alert import StockMorningAlert, OUTPUT_JSON

# index.html のパス (alerts/ の一つ上のディレクトリ)
INDEX_HTML = Path(__file__).parent.parent / "index.html"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ─── ネットワークユーティリティ ──────────────────────────────────

def get_local_ip() -> str:
    """LAN上のIPアドレスを取得"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def start_ngrok(port: int) -> str | None:
    """pyngrok でトンネルを開いてパブリックURLを返す"""
    try:
        from pyngrok import ngrok, conf
        token = os.getenv("NGROK_AUTHTOKEN", "")
        if token:
            conf.get_default().auth_token = token
        tunnel = ngrok.connect(port, "http")
        return tunnel.public_url
    except ImportError:
        logger.error("pyngrokが未インストール: pip install pyngrok")
        return None
    except Exception as e:
        logger.error(f"ngrok起動エラー: {e}")
        return None


# ─── 分析状態管理 ────────────────────────────────────────────────

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
        message = engine.format_alert(results, us_data)
        engine.send_email(message)
        engine.send_line(message)
        _last_result = _load_cached()
        return _last_result or {}
    finally:
        _running = False


# ─── スケジューラー ───────────────────────────────────────────────

def _scheduler_thread():
    alert_time = os.getenv("ALERT_TIME", "07:00")
    logger.info(f"スケジューラー起動: 毎日 {alert_time} に分析実行")
    schedule.every().day.at(alert_time).do(_run_analysis_sync)
    while True:
        schedule.run_pending()
        _time.sleep(30)


# ─── FastAPI アプリ ───────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _last_result
    _last_result = _load_cached()
    threading.Thread(target=_scheduler_thread, daemon=True).start()
    yield


app = FastAPI(title="日本株 朝のアラート API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ─── エンドポイント ───────────────────────────────────────────────

@app.get("/")
async def serve_index():
    """アプリ本体を配信 (PC・スマホ・リモート共通)"""
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=404, detail="index.html が見つかりません")
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/api/status")
async def api_status():
    return {
        "status":      "running",
        "analyzing":   _running,
        "has_cache":   _last_result is not None,
        "cache_time":  _last_result.get("generated_at") if _last_result else None,
        "alert_time":  os.getenv("ALERT_TIME", "07:00"),
        "server_time": datetime.now().isoformat(),
    }


@app.get("/api/morning-alert")
async def get_morning_alert():
    """最新のアラートデータを返す。キャッシュなし時は即時分析。"""
    if _running:
        raise HTTPException(status_code=202, detail="分析実行中です。しばらくお待ちください。")

    data = _last_result or _load_cached()
    if data:
        return JSONResponse(content=data)

    logger.info("キャッシュなし — 初回分析を実行します")
    result = await asyncio.get_event_loop().run_in_executor(None, _run_analysis_sync)
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
    port      = int(os.getenv("API_PORT", "8000"))
    use_ngrok = "--ngrok" in sys.argv
    local_ip  = get_local_ip()

    public_url = None
    if use_ngrok:
        logger.info("ngrok トンネルを開始中...")
        public_url = start_ngrok(port)

    logger.info("=" * 60)
    logger.info("  日本株 朝のアラート — サーバー起動")
    logger.info("-" * 60)
    logger.info(f"  ローカル (PC)   : http://localhost:{port}")
    logger.info(f"  LAN (スマホ等)  : http://{local_ip}:{port}")
    if public_url:
        logger.info(f"  リモート (ngrok): {public_url}  ← インターネット公開中")
    else:
        logger.info("  リモート公開    : --ngrok オプションで有効化")
        logger.info("                    または: cloudflared tunnel --url http://localhost:8000")
    logger.info("=" * 60)

    uvicorn.run("api_server:app", host="0.0.0.0", port=port, reload=False)
