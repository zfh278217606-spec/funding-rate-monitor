
import concurrent.futures
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


# =========================================================
# 基础设置
# 严格小于 -1.8% 才报警
# =========================================================

THRESHOLD = -0.018
STATE_FILE = "state.json"
TZ = ZoneInfo("Asia/Shanghai")

USER_AGENT = "funding-rate-monitor/2.1"


def get(url, params=None):
    r = requests.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
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

    if ts < 10_000_000_000:
        ts *= 1000

    return datetime.fromtimestamp(
        ts / 1000,
        TZ,
    ).strftime("%m-%d %H:%M")


# =========================================================
# OKX 永续资金费率
# =========================================================

def okx():
    d = get(
        "https://www.okx.com/api/v5/public/instruments",
        {"instType": "SWAP"},
    )

    if d.get("code") != "0":
        raise RuntimeError(d)

    symbols = [
        x["instId"]
        for x in d["data"]
        if x.get("state") == "live"
        and x.get("settleCcy") in {"USDT", "USDC"}
    ]

    def one(sym):
        d = get(
            "https://www.okx.com/api/v5/public/funding-rate",
            {"instId": sym},
        )

        if d.get("code") != "0" or not d.get("data"):
            raise RuntimeError(d)

        x = d["data"][0]

        rate = x.get("fundingRate", "")

        if rate in ("", None):
            return None

        funding_time = x.get("fundingTime")
        next_funding_time = x.get("nextFundingTime")

        interval = None

        if funding_time and next_funding_time:
            diff = (
                int(next_funding_time) - int(funding_time)
            ) / 3_600_000

            if diff > 0:
                interval = diff

        return (
            "OKX",
            sym,
            float(rate),
            h(interval),
            funding_time,
        )

    out = []
    failed = 0

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
                print("OKX symbol error:", fs[f], e)

    if symbols and failed > max(
        20,
        len(symbols) * 0.25,
    ):
        raise RuntimeError(
            f"OKX too many failures: {failed}/{len(symbols)}"
        )

    return out


# =========================================================
# Bitget 永续资金费率
# =========================================================

def bitget():
    out = []

    for cat in ("USDT-FUTURES", "USDC-FUTURES"):

        ins = get(
            "https://api.bitget.com/api/v3/market/instruments",
            {"category": cat},
        )

        ticks = get(
            "https://api.bitget.com/api/v3/market/tickers",
            {"category": cat},
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
            and x.get("status") in (
                "online",
                "limit_open",
                "limit_close",
            )
            and x.get("symbolType", "crypto") == "crypto"
        }

        for x in ticks["data"]:

            sym = x.get("symbol", "")
            rate = x.get("fundingRate", "")

            if sym in meta and rate not in ("", None):

                out.append((
                    "Bitget",
                    sym,
                    float(rate),
                    h(meta[sym].get("fundInterval")),
                    None,
                ))

    return out


# =========================================================
# Gate 永续资金费率
# =========================================================

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
            x.get("funding_next_apply"),
        ))

    return out


# =========================================================
# 实际扫描资金费率的三家
# =========================================================

FETCH = {
    "OKX": okx,
    "Bitget": bitget,
    "Gate": gate,
}


# =========================================================
# 提取基础币种
# =========================================================

def contract_base(exchange, symbol):

    symbol = symbol.upper()

    if exchange == "Gate":
        return symbol.split("_")[0]

    if exchange == "OKX":
        return symbol.split("-")[0]

    for quote in ("USDT", "USDC"):
        if symbol.endswith(quote):
            return symbol[:-len(quote)]

    return symbol


# =========================================================
# OKX 现货
# =========================================================

def spot_okx():

    d = get(
        "https://www.okx.com/api/v5/public/instruments",
        {"instType": "SPOT"},
    )

    if d.get("code") != "0":
        raise RuntimeError(d)

    return {
        x["baseCcy"].upper()
        for x in d["data"]
        if x.get("state") == "live"
        and x.get("baseCcy")
    }


# =========================================================
# Gate 现货
# =========================================================

def spot_gate():

    rows = get(
        "https://api.gateio.ws/api/v4/spot/currency_pairs"
    )

    return {
        x["base"].upper()
        for x in rows
        if x.get("base")
        and x.get("trade_status") in {
            "tradable",
            "buyable",
            "sellable",
        }
    }


# =========================================================
# Bitget 现货
# =========================================================

def spot_bitget():

    d = get(
        "https://api.bitget.com/api/v3/market/instruments",
        {"category": "SPOT"},
    )

    if d.get("code") != "00000":
        raise RuntimeError(d)

    return {
        x["baseCoin"].upper()
        for x in d["data"]
        if x.get("baseCoin")
    }


# =========================================================
# Bybit 现货
# =========================================================

def spot_bybit():

    d = get(
        "https://api.bybit.com/v5/market/instruments-info",
        {"category": "spot"},
    )

    if d.get("retCode") != 0:
        raise RuntimeError(d)

    return {
        x["baseCoin"].upper()
        for x in d["result"]["list"]
        if x.get("baseCoin")
        and x.get("status") == "Trading"
    }


# =========================================================
# 四家现货核验
# =========================================================

SPOT_FETCH = {
    "Gate": spot_gate,
    "OKX": spot_okx,
    "Bybit": spot_bybit,
    "Bitget": spot_bitget,
}


def load_spot_markets():

    spot_sources = {}
    errors = []
    counts = {}

    for exchange, fn in SPOT_FETCH.items():

        try:
            coins = fn()

            counts[exchange] = len(coins)

            print(
                f"{exchange} spot: {len(coins)} coins"
            )

            for coin in coins:
                spot_sources.setdefault(
                    coin,
                    set(),
                ).add(exchange)

        except Exception as e:

            errors.append(exchange)
            counts[exchange] = 0

            print(
                f"{exchange} SPOT ERROR:",
                e,
            )

    if len(errors) == len(SPOT_FETCH):
        raise RuntimeError(
            "All spot verification exchanges failed; "
            "state unchanged."
        )

    return spot_sources, errors, counts


# =========================================================
# 报警状态
#
# 修改阈值后自动重置旧状态
# 避免以前 -1.5% 的状态影响 -1.8% 新规则
# =========================================================

def load_state():

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            state = json.load(f)

        if state.get("threshold") != THRESHOLD:

            print(
                "Threshold changed. "
                "Resetting previous alert state."
            )

            return {"active": {}}

        return state

    except FileNotFoundError:
        return {"active": {}}

    except Exception as e:

        print("State read warning:", e)

        return {"active": {}}


def save_state(active):

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            {
                "threshold": THRESHOLD,
                "active": active,
            },
            f,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


# =========================================================
# 微信 + ntfy 推送
# =========================================================

def push(title, body):

    success = 0
    errors = []

    # Server酱 → 微信

    sendkey = os.environ.get("SERVERCHAN_SENDKEY")

    if sendkey:

        try:
            r = requests.post(
                f"https://sctapi.ftqq.com/{sendkey}.send",
                data={
                    "title": title,
                    "desp": body,
                },
                timeout=20,
            )

            r.raise_for_status()

            success += 1

        except Exception as e:

            errors.append(f"Server酱: {e}")

    # ntfy → 安卓

    topic = os.environ.get("NTFY_TOPIC")

    if topic:

        try:
            r = requests.post(
                "https://ntfy.sh",
                json={
                    "topic": topic,
                    "title": title,
                    "message": body,
                    "priority": 5,
                    "tags": ["warning"],
                },
                timeout=20,
            )

            r.raise_for_status()

            success += 1

        except Exception as e:

            errors.append(f"ntfy: {e}")

    if errors:

        print(
            "PUSH WARNING:",
            " | ".join(errors),
        )

    if success == 0:

        raise RuntimeError(
            "All notification channels failed."
        )


# =========================================================
# 主程序
# =========================================================

def main():

    old = load_state().get("active", {})

    active = {
        name: list(old.get(name, []))
        for name in FETCH
    }

    # 每一轮重新查询四家现货
    #
    # 今天币没有现货：
    # 不报警、不记录
    #
    # 后天上现货：
    # 如果资金费率仍小于 -1.8%
    # 立即报警

    (
        spot_sources,
        spot_errors,
        spot_counts,
    ) = load_spot_markets()

    new_hits = []
    funding_errors = []
    funding_counts = {}

    # =====================================================
    # 扫描资金费率
    # =====================================================

    for name, fn in FETCH.items():

        try:

            rows = fn()

            funding_counts[name] = len(rows)

            qualified = {}
            uncertain = set()

            old_symbols = set(
                old.get(name, [])
            )

            for row in rows:

                (
                    exchange,
                    symbol,
                    rate,
                    interval,
                    next_time,
                ) = row

                # =========================================
                # 新阈值：
                # 严格小于 -1.8%
                #
                # -1.80% 不报警
                # -1.81% 报警
                # =========================================

                if rate >= THRESHOLD:
                    continue

                base = contract_base(
                    exchange,
                    symbol,
                )

                spot_on = spot_sources.get(
                    base,
                    set(),
                )

                # 至少一家有现货

                if spot_on:

                    qualified[symbol] = row

                    print(
                        f"QUALIFIED: "
                        f"{exchange} "
                        f"{symbol} "
                        f"{rate * 100:.4f}% "
                        f"- spot on "
                        f"{','.join(sorted(spot_on))}"
                    )

                    if symbol not in old_symbols:
                        new_hits.append(row)

                else:

                    # 部分现货接口异常
                    # 不把未知当成无现货

                    if spot_errors:

                        uncertain.add(symbol)

                        print(
                            f"SUPPRESSED/UNKNOWN: "
                            f"{exchange} "
                            f"{symbol} "
                            f"{rate * 100:.4f}% "
                            f"- no verified spot; "
                            f"spot API errors="
                            f"{','.join(spot_errors)}"
                        )

                    else:

                        print(
                            f"SUPPRESSED: "
                            f"{exchange} "
                            f"{symbol} "
                            f"{rate * 100:.4f}% "
                            f"- no spot on "
                            f"Gate/OKX/Bybit/Bitget"
                        )

            # 更新状态
            #
            # 无现货且没有有效触发：
            # 不进入已报警名单
            #
            # 接口异常：
            # 暂时保留旧状态，避免重复报警

            active[name] = sorted(
                set(qualified.keys())
                |
                (old_symbols & uncertain)
            )

            print(
                f"{name}: "
                f"scanned={len(rows)}, "
                f"qualified={len(qualified)}, "
                f"new={len(set(qualified) - old_symbols)}"
            )

        except Exception as e:

            funding_errors.append(name)

            print(
                f"{name} ERROR:",
                e,
            )

            active[name] = list(
                old.get(name, [])
            )

    # 三家全部失败

    if len(funding_errors) == len(FETCH):

        raise RuntimeError(
            "All funding exchanges failed; "
            "state unchanged."
        )

    # 保存本轮报警状态

    save_state(active)

    # =====================================================
    # 新触发报警
    # =====================================================

    if new_hits:

        for start in range(
            0,
            len(new_hits),
            20,
        ):

            chunk = sorted(
                new_hits[start:start + 20],
                key=lambda x: x[2],
            )

            lines = [
                "## 新触发：资金费率 < -1.8%",
                "",
                "现货过滤：Gate / OKX / "
                "Bybit / Bitget "
                "至少一家存在现货。",
                "",
            ]

            for (
                exchange,
                symbol,
                rate,
                interval,
                next_time,
            ) in chunk:

                base = contract_base(
                    exchange,
                    symbol,
                )

                spot_on = "、".join(
                    sorted(
                        spot_sources.get(
                            base,
                            set(),
                        )
                    )
                )

                lines.append(
                    f"- **{exchange} · {symbol}**："
                    f"**{rate * 100:.4f}%**"
                    f" ｜周期 {interval}"
                    f" ｜现货：{spot_on}"
                    f" ｜本期结算 {when(next_time)}"
                )

            lines += [
                "",
                "扫描时间："
                + datetime.now(TZ).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "",
                "没有现货的币不会进入报警状态；"
                "以后每轮仍会重新检查。"
                "一旦上现货且资金费率仍低于 -1.8%，"
                "会立即报警。",
            ]

            if funding_errors:

                lines += [
                    "",
                    "本轮资金费率接口异常："
                    + "、".join(funding_errors),
                ]

            if spot_errors:

                lines += [
                    "",
                    "本轮现货核验接口异常："
                    + "、".join(spot_errors),
                ]

            push(
                "🚨 资金费率 < -1.8%："
                f"{len(chunk)} 个新触发",
                "\n".join(lines),
            )

    # =====================================================
    # 手动 Run workflow 测试
    # =====================================================

    elif os.getenv("FORCE_TEST_NOTIFY") == "1":

        funding_detail = "\n".join(
            f"- {name}: "
            f"{funding_counts.get(name, 0)} 个合约"
            for name in FETCH
        )

        spot_detail = "\n".join(
            f"- {name}: "
            f"{spot_counts.get(name, 0)} 种现货基础币"
            for name in SPOT_FETCH
        )

        body = (
            "## 手动测试完成\n\n"
            "### 资金费率扫描\n"
            + funding_detail
            + "\n\n"
            "### 现货核验\n"
            + spot_detail
            + "\n\n"
            "报警阈值：严格小于 -1.8%"
            + "\n\n"
            "只有 Gate / OKX / Bybit / Bitget "
            "至少一家明确存在现货，才会报警。"
        )

        if funding_errors:

            body += (
                "\n\n资金费率接口异常："
                + "、".join(funding_errors)
            )

        if spot_errors:

            body += (
                "\n\n现货核验接口异常："
                + "、".join(spot_errors)
            )

        push(
            "✅ 资金费率 + 现货过滤扫描正常",
            body,
        )

    else:

        print("No new alerts.")


# =========================================================
# 启动
# =========================================================

if __name__ == "__main__":
    main()
