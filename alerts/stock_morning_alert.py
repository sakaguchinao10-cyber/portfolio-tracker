#!/usr/bin/env python3
"""
日本株 朝のアラート
毎朝07:00に前日の米国株式市場の動きを基に
日本の寄り付きで上がる確率が高い銘柄TOP3を通知する

使い方:
  # スケジューラー起動 (07:00に毎日実行)
  python stock_morning_alert.py

  # 今すぐ実行
  python stock_morning_alert.py --now

  # APIサーバー経由でも起動可能
  python api_server.py
"""

import os
import sys
import json
import time
import logging
import smtplib
import schedule
import requests
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

# ─── ロギング設定 ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "stock_alert.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ─── 分析対象：日本株 ───────────────────────────────────────────
JAPANESE_STOCKS = {
    "8035.T": {"name": "東京エレクトロン",       "sector": "semiconductor"},
    "6758.T": {"name": "ソニーグループ",          "sector": "tech"},
    "6861.T": {"name": "キーエンス",              "sector": "tech"},
    "7203.T": {"name": "トヨタ自動車",            "sector": "auto"},
    "9984.T": {"name": "ソフトバンクG",           "sector": "tech"},
    "7974.T": {"name": "任天堂",                  "sector": "entertainment"},
    "6954.T": {"name": "ファナック",              "sector": "industrial"},
    "8306.T": {"name": "三菱UFJフィナンシャルG", "sector": "finance"},
    "4063.T": {"name": "信越化学工業",            "sector": "semiconductor"},
    "6367.T": {"name": "ダイキン工業",            "sector": "industrial"},
    "7267.T": {"name": "本田技研工業",            "sector": "auto"},
    "6501.T": {"name": "日立製作所",              "sector": "industrial"},
    "4519.T": {"name": "中外製薬",               "sector": "pharma"},
    "8411.T": {"name": "みずほフィナンシャルG",  "sector": "finance"},
    "6098.T": {"name": "リクルートHD",           "sector": "tech"},
    "7741.T": {"name": "HOYA",                   "sector": "optical"},
    "4543.T": {"name": "テルモ",                 "sector": "medical"},
    "9983.T": {"name": "ファーストリテイリング", "sector": "retail"},
    "6503.T": {"name": "三菱電機",               "sector": "industrial"},
    "4661.T": {"name": "オリエンタルランド",     "sector": "entertainment"},
}

# ─── 米国指数 ────────────────────────────────────────────────────
US_INDICES = {
    "^GSPC": "S&P 500",
    "^IXIC": "NASDAQ総合",
    "^DJI":  "ダウ平均",
    "^VIX":  "VIX恐怖指数",
    "^SOX":  "フィラデルフィア半導体",
}

# ─── セクター別：重視する米国指数 ──────────────────────────────
SECTOR_BENCHMARKS = {
    "semiconductor":  ["^IXIC", "^SOX"],
    "tech":           ["^IXIC", "^GSPC"],
    "auto":           ["^DJI",  "^GSPC"],
    "finance":        ["^DJI",  "^GSPC"],
    "industrial":     ["^DJI",  "^GSPC"],
    "entertainment":  ["^IXIC", "^GSPC"],
    "pharma":         ["^GSPC"],
    "retail":         ["^DJI",  "^GSPC"],
    "optical":        ["^IXIC"],
    "medical":        ["^GSPC"],
}

SECTOR_JP = {
    "semiconductor":  "半導体・電子部品",
    "tech":           "テクノロジー",
    "auto":           "自動車",
    "finance":        "金融",
    "industrial":     "産業機械",
    "entertainment":  "エンターテインメント",
    "pharma":         "製薬",
    "retail":         "小売",
    "optical":        "光学機器",
    "medical":        "医療機器",
}

OUTPUT_JSON = Path(__file__).parent / "morning_alert.json"
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "90"))


# ─── ユーティリティ ──────────────────────────────────────────────

def _pct_change(series: pd.Series) -> pd.Series:
    return series.pct_change().dropna()


def _align(a: pd.Series, b: pd.Series):
    common = a.index.intersection(b.index)
    return a[common], b[common]


def correlation(jp: pd.DataFrame, us: pd.DataFrame) -> float:
    jp_r, us_r = _align(_pct_change(jp["Close"]), _pct_change(us["Close"]))
    if len(jp_r) < 20:
        return 0.0
    c = jp_r.corr(us_r)
    return float(c) if not np.isnan(c) else 0.0


def beta(jp: pd.DataFrame, us: pd.DataFrame) -> float:
    jp_r, us_r = _align(_pct_change(jp["Close"]), _pct_change(us["Close"]))
    if len(jp_r) < 20:
        return 1.0
    var = us_r.var()
    if var == 0:
        return 1.0
    return float(jp_r.cov(us_r) / var)


def momentum_5d(hist: pd.DataFrame) -> float:
    """直近5日間の平均日次リターン (%)"""
    if len(hist) < 6:
        return 0.0
    return float(_pct_change(hist["Close"]).iloc[-5:].mean() * 100)


def recent_moves(hist: pd.DataFrame, n: int = 5) -> list:
    """直近n日の日次変化率リスト (%)"""
    returns = _pct_change(hist["Close"]).iloc[-n:] * 100
    return [round(float(v), 2) for v in returns.tolist()]


# ─── メイン分析クラス ─────────────────────────────────────────────

class StockMorningAlert:

    def __init__(self):
        self.lookback = LOOKBACK_DAYS

    # ── データ取得 ─────────────────────────────────────────────────

    def _fetch(self, ticker: str, period: str = None) -> pd.DataFrame | None:
        try:
            period = period or f"{self.lookback + 10}d"
            hist = yf.Ticker(ticker).history(period=period)
            return hist if not hist.empty else None
        except Exception as e:
            logger.warning(f"  取得失敗 {ticker}: {e}")
            return None

    def fetch_us_market(self) -> dict:
        """米国市場の直近データを取得"""
        logger.info("米国市場データを取得中...")
        result = {}
        for ticker, name in US_INDICES.items():
            hist = self._fetch(ticker, period=f"{self.lookback + 10}d")
            if hist is None or len(hist) < 2:
                logger.warning(f"  {name}: データ不足")
                continue
            prev_close = float(hist["Close"].iloc[-2])
            last_close = float(hist["Close"].iloc[-1])
            chg = (last_close - prev_close) / prev_close * 100
            result[ticker] = {
                "name":        name,
                "history":     hist,
                "latest_close": last_close,
                "prev_close":   prev_close,
                "change_pct":   round(chg, 2),
                "date":        hist.index[-1].strftime("%Y-%m-%d"),
            }
            sign = "+" if chg >= 0 else ""
            logger.info(f"  {name}: {sign}{chg:.2f}%")
        return result

    # ── 確率計算 ────────────────────────────────────────────────────

    def calc_probability(
        self,
        ticker: str,
        info: dict,
        jp_hist: pd.DataFrame,
        us_data: dict,
    ) -> dict:
        sector = info["sector"]
        sector_bench = SECTOR_BENCHMARKS.get(sector, ["^GSPC"])

        prob = 0.50  # ベース確率
        impact_list = []

        for us_ticker, us_info in us_data.items():
            if us_ticker == "^VIX":
                continue
            us_hist = us_info["history"]
            corr = correlation(jp_hist, us_hist)
            b    = beta(jp_hist, us_hist)
            us_chg = us_info["change_pct"]

            # 相関 × ベータ × 米国変動 → 日本株への影響推計
            impact = corr * b * us_chg * 0.30
            # セクター関連指数は影響を1.5倍
            if us_ticker in sector_bench:
                impact *= 1.5
            impact_list.append(impact)

        # 相関ベース確率調整 (tanh で ±25% 以内に収束)
        if impact_list:
            avg_impact = float(np.mean(impact_list))
            prob += float(np.tanh(avg_impact / 2)) * 0.25

        # VIX 調整
        if "^VIX" in us_data:
            vix_lvl = us_data["^VIX"]["latest_close"]
            vix_chg = us_data["^VIX"]["change_pct"]
            if vix_lvl > 30:
                prob -= 0.10
            elif vix_lvl > 25:
                prob -= 0.05
            elif vix_lvl < 15:
                prob += 0.03
            if vix_chg > 15:
                prob -= 0.10
            elif vix_chg > 10:
                prob -= 0.06
            elif vix_chg < -10:
                prob += 0.05

        # 短期モメンタム調整 (±8%)
        mom = momentum_5d(jp_hist)
        prob += float(np.tanh(mom / 2)) * 0.08

        prob = max(0.10, min(0.90, prob))

        # 表示用：主要相関
        corr_map = {}
        for us_ticker, us_info in us_data.items():
            if us_ticker == "^VIX":
                continue
            corr_map[us_info["name"]] = round(correlation(jp_hist, us_info["history"]), 3)

        return {
            "probability":       round(prob, 4),
            "momentum":          round(mom, 2),
            "correlation_with_us": corr_map,
            "recent_moves":      recent_moves(jp_hist, 5),
            "current_price":     round(float(jp_hist["Close"].iloc[-1]), 0),
            "prev_day_change":   round(float(_pct_change(jp_hist["Close"]).iloc[-1] * 100), 2),
            "volume_ratio":      round(
                float(jp_hist["Volume"].iloc[-1]) / float(jp_hist["Volume"].iloc[-20:].mean()), 2
            ) if jp_hist["Volume"].iloc[-20:].mean() > 0 else 1.0,
        }

    # ── 全銘柄分析 ──────────────────────────────────────────────────

    def analyze(self) -> tuple[list, dict]:
        logger.info("=" * 56)
        logger.info("  日本株 朝のアラート 分析開始")
        logger.info("=" * 56)

        us_data = self.fetch_us_market()
        if not us_data:
            logger.error("米国市場データの取得に失敗しました")
            return [], {}

        results = []
        logger.info("日本株を分析中...")

        for ticker, info in JAPANESE_STOCKS.items():
            jp_hist = self._fetch(ticker)
            if jp_hist is None or len(jp_hist) < 30:
                logger.warning(f"  {info['name']}: データ不足のためスキップ")
                continue

            try:
                analysis = self.calc_probability(ticker, info, jp_hist, us_data)
                prob = analysis["probability"]
                results.append({
                    "ticker":  ticker,
                    "name":    info["name"],
                    "sector":  info["sector"],
                    **analysis,
                })
                sign = "+" if analysis["prev_day_change"] >= 0 else ""
                logger.info(
                    f"  {info['name']:18s} "
                    f"確率:{prob:.0%}  "
                    f"前日:{sign}{analysis['prev_day_change']:.2f}%  "
                    f"mom:{analysis['momentum']:+.2f}%"
                )
            except Exception as e:
                logger.error(f"  {info['name']} 分析エラー: {e}", exc_info=True)

        results.sort(key=lambda x: x["probability"], reverse=True)
        logger.info(f"\n分析完了: {len(results)} 銘柄")
        return results, us_data

    # ── フォーマット ────────────────────────────────────────────────

    @staticmethod
    def _bar(prob: float, width: int = 10) -> str:
        filled = round(prob * width)
        return "█" * filled + "░" * (width - filled)

    @staticmethod
    def _corr_label(c: float) -> str:
        if c >= 0.70:  return "★★★ 強い正相関"
        if c >= 0.40:  return "★★☆ 中程度の正相関"
        if c >= 0.10:  return "★☆☆ 弱い正相関"
        if c >= -0.10: return "☆☆☆ ほぼ無相関"
        if c >= -0.40: return "▽☆☆ 弱い逆相関"
        return             "▽▽☆ 強い逆相関"

    @staticmethod
    def _moves_str(moves: list) -> str:
        parts = []
        for m in moves:
            if m >= 1.5:   parts.append(f"↑↑{m:+.1f}%")
            elif m >= 0.3: parts.append(f"↑{m:+.1f}%")
            elif m <= -1.5: parts.append(f"↓↓{m:+.1f}%")
            elif m <= -0.3: parts.append(f"↓{m:+.1f}%")
            else:           parts.append(f"→{m:+.1f}%")
        return " ".join(parts) if parts else "データなし"

    def format_alert(self, results: list, us_data: dict) -> str:
        now = datetime.now()
        lines = []
        sep = "=" * 60

        lines.append(sep)
        lines.append(f"  日本株 朝のアラート  {now.strftime('%Y年%m月%d日 %H:%M')}")
        lines.append(sep)
        lines.append("")
        lines.append("【前日の米国市場】")

        for ticker, d in us_data.items():
            sign = "+" if d["change_pct"] >= 0 else ""
            arrow = "↑" if d["change_pct"] > 0 else ("↓" if d["change_pct"] < 0 else "→")
            lines.append(f"  {arrow} {d['name']:22s} {sign}{d['change_pct']:.2f}%  ({d['date']})")

        lines.append("")

        # 判定条件：上昇確率50%超の銘柄が対象
        valid = [r for r in results if r["probability"] > 0.50]

        if not valid:
            lines.append("⚠ 本日は上昇確率が50%を超える銘柄が見当たりません")
            sp_chg = us_data.get("^GSPC", {}).get("change_pct", 0)
            if sp_chg < -1.5:
                lines.append("  (前日の米国市場が大幅下落のため全体的にリスク高)")
            lines.append("")
            lines.append(sep)
            return "\n".join(lines)

        medals = ["1位", "2位", "3位"]
        lines.append("【寄り付き上昇期待 TOP3】")
        lines.append("-" * 60)

        for i, stock in enumerate(results[:3]):
            if stock["probability"] <= 0.50:
                break
            bar = self._bar(stock["probability"])
            mv  = self._moves_str(stock["recent_moves"])
            pdc = stock["prev_day_change"]
            pdc_str = f"+{pdc:.2f}%" if pdc >= 0 else f"{pdc:.2f}%"

            lines.append(f"")
            lines.append(f"  {medals[i]}  {stock['name']}  ({stock['ticker']})")
            lines.append(f"  セクター : {SECTOR_JP.get(stock['sector'], stock['sector'])}")
            lines.append(f"  上昇確率 : {stock['probability']:.1%}  [{bar}]")
            lines.append(f"  現在株価 : ¥{stock['current_price']:,.0f}  前日比: {pdc_str}")
            lines.append(f"  出来高比 : {stock['volume_ratio']:.2f}x (過去20日平均比)")
            lines.append(f"  直近5日  : {mv}")
            lines.append(f"  米国相関 :")
            for us_name, c in stock["correlation_with_us"].items():
                lines.append(f"    {us_name:22s} {c:+.3f}  {self._corr_label(c)}")
            lines.append("-" * 60)

        lines.append("")
        lines.append("※ この情報は投資の参考情報です。投資判断はご自身でお願いします。")
        lines.append(sep)
        return "\n".join(lines)

    # ── 保存 ────────────────────────────────────────────────────────

    @staticmethod
    def save_json(results: list, us_data: dict):
        output = {
            "generated_at": datetime.now().isoformat(),
            "us_market": {
                ticker: {
                    "name":        d["name"],
                    "change_pct":  d["change_pct"],
                    "latest_close": d["latest_close"],
                    "date":        d["date"],
                }
                for ticker, d in us_data.items()
            },
            "top_picks": [
                {
                    "ticker":             r["ticker"],
                    "name":              r["name"],
                    "sector":            r["sector"],
                    "sector_jp":         SECTOR_JP.get(r["sector"], r["sector"]),
                    "probability":       r["probability"],
                    "current_price":     r["current_price"],
                    "prev_day_change":   r["prev_day_change"],
                    "recent_moves":      r["recent_moves"],
                    "momentum":          r["momentum"],
                    "volume_ratio":      r["volume_ratio"],
                    "correlation_with_us": r["correlation_with_us"],
                }
                for r in results[:3]
                if r["probability"] > 0.50
            ],
            "all_results_count": len(results),
            "above_50pct_count": len([r for r in results if r["probability"] > 0.50]),
        }
        with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        logger.info(f"JSONを保存: {OUTPUT_JSON}")

    # ── 通知 ────────────────────────────────────────────────────────

    @staticmethod
    def send_email(message: str):
        host   = os.getenv("SMTP_HOST", "smtp.gmail.com")
        port   = int(os.getenv("SMTP_PORT", "587"))
        user   = os.getenv("SMTP_USER", "")
        passwd = os.getenv("SMTP_PASS", "")
        to     = os.getenv("ALERT_EMAIL", "")
        if not all([user, passwd, to]):
            return
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"日本株 朝のアラート {datetime.now().strftime('%Y/%m/%d')}"
            msg["From"] = user
            msg["To"]   = to
            msg.attach(MIMEText(message, "plain", "utf-8"))
            with smtplib.SMTP(host, port) as s:
                s.starttls()
                s.login(user, passwd)
                s.sendmail(user, to, msg.as_bytes())
            logger.info(f"メール送信: {to}")
        except Exception as e:
            logger.error(f"メール送信エラー: {e}")

    @staticmethod
    def send_line(message: str):
        token = os.getenv("LINE_NOTIFY_TOKEN", "")
        if not token:
            return
        try:
            requests.post(
                "https://notify-api.line.me/api/notify",
                headers={"Authorization": f"Bearer {token}"},
                data={"message": "\n" + message},
                timeout=10,
            )
            logger.info("LINE Notify 送信完了")
        except Exception as e:
            logger.error(f"LINE Notify エラー: {e}")

    # ── 実行エントリ ────────────────────────────────────────────────

    def run(self):
        try:
            results, us_data = self.analyze()
            message = self.format_alert(results, us_data)
            print("\n" + message)
            self.save_json(results, us_data)
            self.send_email(message)
            self.send_line(message)
        except Exception as e:
            logger.error(f"実行エラー: {e}", exc_info=True)


# ─── スケジューラー起動 ─────────────────────────────────────────

def main():
    alert = StockMorningAlert()

    if "--now" in sys.argv:
        logger.info("即時実行モード")
        alert.run()
        return

    alert_time = os.getenv("ALERT_TIME", "07:00")
    logger.info(f"スケジューラー起動 — 毎日 {alert_time} に実行します")
    schedule.every().day.at(alert_time).do(alert.run)

    logger.info(f"次回実行: {schedule.next_run()}")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
