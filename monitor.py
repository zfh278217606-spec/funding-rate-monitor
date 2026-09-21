import concurrent.futures
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

THRESHOLD = -0.015  # -1.5%
STATE_FILE = "state.json"
TZ = ZoneInfo("Asia/Shanghai")

# 稳定币本位，排除 BTC/ETH 等币本位逆向合约
STABLE = ("USDT", "USDC")

S = requests.Session()
S.headers.update({"User-Agent": "funding-rate-monitor/1.0"})


def get(url, params=None):
    r = S.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def h(v):
    if v in (None, "", 0, "0"):
        return "未知"
    return f"{float(v):g}h"


def when(ts):
    if not ts:
        return "未知"

    ts = int(float(ts))

    # 有的交易所给秒，有的给毫秒
    if ts < 10_000_000_000:
        ts *= 1000

    return datetime.fromtimestamp(
        ts / 1000, TZ
    ).strftime("%m-%d %H:%M")


# =========================
# Binance
# =========================

def binance():
    base = "https://fapi.binance.com"

    rows = get(
        base + "/fapi/v1/premiumIndex"
    )

    info = get(
        base + "/fapi/v1/fundingInfo"
    )

    intervals = {
        x["symbol"]: x["fundingIntervalHours"]
        for x in info
    }

    out = []

    for x in rows:
        sym = x.get("symbol", "")
        rate = x.get("lastFundingRate", "")
        nxt = x.get("nextFundingTime", 0)

        if (
            sym.endswith(STABLE)
            and rate not in ("", None)
            and nxt
        ):
            out.append((
                "Binance",
                sym,
                float(rate),
                h(intervals.get(sym, 8)),
                nxt
            ))

    return out


# =========================
# Bybit
# =========================

def bybit():
    d = get(
        "https://api.bybit.com/v5/market/tickers",
        {"category": "linear"}
    )

    if d.get("retCode") != 0:
        raise RuntimeError(d)

    out = []

    for x in d["result"]["list"]:
        sym = x.get("symbol", "")
        rate = x.get("fundingRate", "")
        nxt = x.get("nextFundingTime", "")

        if (
            sym.endswith(STABLE)
            and rate not in ("", None)
            and nxt
        ):
            out.append((
                "Bybit",
                sym,
                float(rate),
                h(x.get("fundingIntervalHour")),
                nxt
            ))

    return out


# =========================
# Bitget
# =========================

def bitget():
    out = []

    for cat in (
        "USDT-FUTURES",
        "USDC-FUTURES"
    ):
        ins = get(
            "https://api.bitget.com/api/v3/market/instruments",
            {"category": cat}
        )

        ticks = get(
            "https://api.bitget.com/api/v3/market/tickers",
            {"category": cat}
        )

        if (
            ins.get("code") != "00000"
            or ticks.get("code") != "00000"
        ):
            raise RuntimeError(
                f"{ins.get('msg')} / {ticks.get('msg')}"
            )

        meta = {
            x["symbol"]: x
            for x in ins["data"]
            if x.get("type") == "perpetual"
            and x.get("status")
            in (
                "online",
                "limit_open",
                "limit_close"
            )
            and x.get(
                "symbolType",
                "crypto"
            ) == "crypto"
        }

        for x in ticks["data"]:
            sym = x.get("symbol", "")
            rate = x.get("fundingRate", "")

            if (
                sym in meta
                and rate not in ("", None)
            ):
                out.append((
                    "Bitget",
                    sym,
                    float(rate),
                    h(meta[sym].get("fundInterval")),
                    None
                ))

    return out


# =========================
# Gate
# =========================

def gate():
    rows = get(
        "https://api.gateio.ws/api/v4/futures/usdt/contracts"
    )

    out = []

    for x in rows:
        rate = x.get("funding_rate", "")

        if (
            rate in ("", None)
            or x.get("in_delisting")
        ):
            continue

        sec = x.get("funding_interval")

        interval = (
            float(sec) / 3600
            if sec
            else None
        )

        out.append((
            "Gate",
            x.get("name", ""),
            float(rate),
            h(interval),
            x.get("funding_next_apply")
        ))

    return out


# =========================
# OKX
# =========================

def okx():
    d = get(
        "https://www.okx.com/api/v5/public/instruments",
        {"instType": "SWAP"}
    )

    if d.get("code") != "0":
        raise RuntimeError(d)

    symbols = [
        x["instId"]
        for x in d["data"]
        if x.get("state") == "live"
        and x.get("settleCcy")
        in {"USDT", "USDC"}
    ]

    def one(sym):
        d = get(
            "https://www.okx.com/api/v5/public/funding-rate",
            {"instId": sym}
        )

        if (
            d.get("code") != "0"
            or not d.get("data")
        ):
            raise RuntimeError(d)

        x = d["data"][0]

        rate = x.get("fundingRate", "")

        if rate in ("", None):
            return None

        ft = x.get("fundingTime")
        nft = x.get("nextFundingTime")

        interval = None

        if ft and nft:
            diff = (
                int(nft) - int(ft)
            ) / 3_600_000

            if diff > 0:
                interval = diff

        return (
            "OKX",
            sym,
            float(rate),
            h(interval),
            ft
        )

    out = []
    failed = 0

    # OKX 当前资金费率需要按合约取，
    # 并发查询，避免几百个币慢慢排队
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=8
    ) as pool:

        fs = {
            pool.submit(one, s): s
            for s in symbols
        }

        for f in concurrent.futures.as_completed(fs):
            try:
                row = f.result()

                if row:
                    out.append(row)

            except Exception as e:
                failed += 1
                print(
                    "OKX symbol error:",
                    fs[f],
                    e
                )

    # 少量单币失败可以容忍。
    # 大面积失败则认为 OKX 本轮异常。
    if (
        symbols
        and failed
        > max(20, len(symbols) * 0.25)
    ):
        raise RuntimeError(
            f"OKX too many failures: "
            f"{failed}/{len(symbols)}"
        )

    return out


FETCH = {
    "Binance": binance,
    "OKX": okx,
    "Bybit": bybit,
    "Bitget": bitget,
    "Gate": gate,
}


# =========================
# 报警状态
# =========================

def load_state():
    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception:
        return {"active": {}}


def save_state(active):
    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            {"active": active},
            f,
            ensure_ascii=False,
            indent=2,
            sort_keys=True
        )


# =========================
# Server酱微信
# =========================

def push(title, body):
    # =====================
    # Server酱 → 微信
    # =====================

    sendkey = os.environ["SERVERCHAN_SENDKEY"]

    r = requests.post(
        f"https://sctapi.ftqq.com/{sendkey}.send",
        data={
            "title": title,
            "desp": body
        },
        timeout=20
    )

    r.raise_for_status()

    # =====================
    # ntfy → 安卓强提醒
    # =====================

    topic = os.environ.get("NTFY_TOPIC")

    if topic:
        r = requests.post(
            "https://ntfy.sh",
            json={
                "topic": topic,
                "title": title,
                "message": body,
                "priority": 5,
                "tags": ["warning"]
            },
            timeout=20
        )

        r.raise_for_status()


# =========================
# 主程序
# =========================

def main():
    old = load_state().get(
        "active",
        {}
    )

    # 某交易所临时挂了时，
    # 保留它上次的状态，避免重复报警
    active = dict(old)

    new_hits = []
    errors = []
    counts = {}

    for name, fn in FETCH.items():

        try:
            rows = fn()

            counts[name] = len(rows)

            hits = {}

            for row in rows:
                (
                    exchange,
                    symbol,
                    rate,
                    interval,
                    next_time
                ) = row

                if rate <= THRESHOLD:
                    hits[symbol] = row

            old_symbols = set(
                old.get(name, [])
            )

            for symbol, row in hits.items():

                if symbol not in old_symbols:
                    new_hits.append(row)

            active[name] = sorted(
                hits.keys()
            )

            print(
                f"{name}: "
                f"scanned={len(rows)}, "
                f"hit={len(hits)}, "
                f"new="
                f"{len(set(hits) - old_symbols)}"
            )

        except Exception as e:

            errors.append(name)

            print(
                f"{name} ERROR:",
                e
            )

    # 五家同时全挂时，
    # 不改状态，避免制造假恢复
    if len(errors) == len(FETCH):
        raise RuntimeError(
            "All five exchanges failed; "
            "state unchanged."
        )

    save_state(active)

    # =====================
    # 真正报警
    # =====================

    if new_hits:

        # 一条最多写 20 个，
        # 极端行情时避免微信正文过长
        for start in range(
            0,
            len(new_hits),
            20
        ):
            chunk = sorted(
                new_hits[start:start + 20],
                key=lambda x: x[2]
            )

            lines = [
                "## 新触发："
                f"资金费率 ≤ "
                f"{THRESHOLD * 100:.1f}%",
                ""
            ]

            for (
                exchange,
                symbol,
                rate,
                interval,
                next_time
            ) in chunk:

                lines.append(
                    f"- **{exchange} · "
                    f"{symbol}**："
                    f"**{rate * 100:.4f}%**"
                    f" ｜周期 {interval}"
                    f" ｜本期结算 "
                    f"{when(next_time)}"
                )

            lines += [
                "",
                "扫描时间："
                + datetime.now(TZ).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "",
                "首次跌破才提醒；"
                "恢复后再次跌破会重新提醒。"
            ]

            if errors:
                lines += [
                    "",
                    "本轮接口异常："
                    + "、".join(errors)
                ]

            push(
                "🚨 资金费率 ≤ -1.5%："
                f"{len(chunk)} 个新触发",
                "\n".join(lines)
            )

    # =====================
    # 手动运行时测试
    # =====================

    elif (
        os.getenv(
            "FORCE_TEST_NOTIFY"
        ) == "1"
    ):

        detail = "\n".join(
            f"- {name}: "
            f"{counts.get(name, 0)} 个合约"
            for name in FETCH
        )

        if errors:
            detail += (
                "\n\n接口异常："
                + "、".join(errors)
            )

        push(
            "✅ 五所资金费率扫描正常",
            "## 手动测试完成\n\n"
            + detail
            + "\n\n报警阈值："
            + f"{THRESHOLD * 100:.1f}%"
            + "\n\n定时运行无触发时"
              "不会发消息。"
        )

    else:
        print("No new alerts.")


if __name__ == "__main__":
    main()
