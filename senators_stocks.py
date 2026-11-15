#!/usr/bin/env python3
"""
Sleduje obchody amerických senátorů a kongresmanů s akciemi (zveřejňované
podle zákona STOCK Act) a posílá nové obchody česky do aplikace ntfy.

Zdroj dat: Financial Modeling Prep (FMP) – endpointy
  /stable/senate-latest  (Senát)
  /stable/house-latest   (Sněmovna reprezentantů)
Bezplatný API klíč: https://site.financialmodelingprep.com/register

Proměnné prostředí:
  FMP_API_KEY       – povinné, API klíč FMP
  NTFY_TOPIC        – téma ntfy (výchozí níže)
  NTFY_SERVER       – výchozí https://ntfy.sh
  ONLY_PURCHASES    – "1" = posílat jen nákupy
  NOTIFY_EMPTY      – "1" = poslat zprávu i když nejsou nové obchody
  FIRST_RUN_LIMIT   – kolik obchodů poslat při úplně prvním spuštění (výchozí 10)
  STATE_FILE        – soubor s již odeslanými obchody (výchozí seen.json)

Použití:
  python senators_stocks.py            # jedno spuštění (např. z cronu / GitHub Actions)
  python senators_stocks.py --loop     # běží stále a kontroluje každé 3 hodiny
  python senators_stocks.py --dry-run  # jen vypíše, nic neodešle ani neuloží
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DEFAULT_TOPIC = "senat-akcie-yckQ8xLDyxkG006JwR5wJI90"
FMP_BASE = "https://financialmodelingprep.com/stable"
FEEDS = {
    "Senát": "senate-latest",
    "Sněmovna": "house-latest",
}
INTERVAL_SECONDS = 3 * 60 * 60
NTFY_MAX_BYTES = 3800  # ntfy limit zprávy je 4096 B
MAX_SEEN = 5000

TYPE_CS = {
    "purchase": ("🟢", "NÁKUP"),
    "sale": ("🔴", "PRODEJ"),
    "sale (full)": ("🔴", "PRODEJ (celý)"),
    "sale (partial)": ("🟠", "PRODEJ (částečný)"),
    "exchange": ("🔄", "SMĚNA"),
}
OWNER_CS = {
    "self": "sám/sama",
    "spouse": "manžel/ka",
    "joint": "společně",
    "child": "dítě",
    "dependent": "dítě",
}


def env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "ano")


# ---------------------------------------------------------------- data ---

def fetch_feed(endpoint, api_key, limit=25):
    # Bezplatný tarif FMP: jen page=0 a max. 25 záznamů na dotaz.
    qs = urllib.parse.urlencode({"page": 0, "limit": limit, "apikey": api_key})
    url = f"{FMP_BASE}/{endpoint}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "senators-stocks/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if isinstance(data, dict):  # FMP vrací chybu jako objekt
        raise RuntimeError(data.get("Error Message") or data.get("message") or str(data))
    return data


def trade_key(chamber, t):
    parts = [
        chamber,
        t.get("firstName", ""), t.get("lastName", ""), t.get("office", ""),
        t.get("symbol", ""), t.get("assetDescription", ""),
        t.get("transactionDate", ""), t.get("disclosureDate", ""),
        t.get("type", ""), t.get("amount", ""), t.get("owner", ""),
    ]
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()


def politician_name(t):
    name = f"{t.get('firstName', '')} {t.get('lastName', '')}".strip()
    return name or t.get("office") or "Neznámý politik"


def fmt_date(s):
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d")
        return f"{d.day}. {d.month}. {d.year}"
    except (TypeError, ValueError):
        return s or "?"


def fmt_amount(a):
    if not a:
        return "?"
    a = a.replace("$", "").replace(",", " ").replace(" - ", " – ")
    if a.lower().startswith("over"):
        a = "více než " + a[4:].strip()
    return a + " USD"


def format_trade(chamber, t):
    ttype = (t.get("type") or "").strip()
    emoji, label = TYPE_CS.get(ttype.lower(), ("⚪", ttype.upper() or "OBCHOD"))
    symbol = (t.get("symbol") or "").strip()
    asset = (t.get("assetDescription") or "").strip()
    what = f"{symbol} – {asset}" if symbol and asset else (symbol or asset or "?")
    district = t.get("district") or ""
    who = politician_name(t) + (f" ({district})" if district else "")
    owner = OWNER_CS.get((t.get("owner") or "").strip().lower(), t.get("owner") or "")

    lines = [
        f"{emoji} {label}: {what}",
        f"👤 {who} · {chamber}",
        f"💵 {fmt_amount(t.get('amount'))}",
        f"📅 obchod {fmt_date(t.get('transactionDate'))}, "
        f"zveřejněno {fmt_date(t.get('disclosureDate'))}",
    ]
    if owner and owner != "sám/sama":
        lines.append(f"👪 vlastník: {owner}")
    if t.get("link"):
        lines.append(f"🔗 {t['link']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- ntfy ---

def ntfy_send(server, topic, title, message, tags=None, click=None):
    # JSON publikování podporuje UTF-8 v titulku i textu (čeština).
    payload = {"topic": topic, "title": title, "message": message}
    if tags:
        payload["tags"] = tags
    if click:
        payload["click"] = click
    req = urllib.request.Request(
        server.rstrip("/"),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def chunk_messages(blocks):
    chunks, cur = [], ""
    for b in blocks:
        candidate = f"{cur}\n\n{b}" if cur else b
        if cur and len(candidate.encode("utf-8")) > NTFY_MAX_BYTES:
            chunks.append(cur)
            cur = b
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


# --------------------------------------------------------------- state ---

def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        print(f"Varování: nelze načíst {path}: {e}", file=sys.stderr)
        return None


def save_state(path, seen):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"seen": seen[-MAX_SEEN:],
                   "updated": datetime.now(timezone.utc).isoformat()}, f, indent=0)


# ----------------------------------------------------------------- run ---

def run_once(dry_run=False):
    api_key = os.environ.get("FMP_API_KEY", "").strip()
    if not api_key:
        sys.exit("Chybí FMP_API_KEY (zdarma na https://site.financialmodelingprep.com/register).")
    topic = os.environ.get("NTFY_TOPIC", DEFAULT_TOPIC).strip()
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip()
    state_file = os.environ.get("STATE_FILE", "seen.json")
    only_purchases = env_flag("ONLY_PURCHASES")
    first_run_limit = int(os.environ.get("FIRST_RUN_LIMIT", "10"))

    state = load_state(state_file)
    first_run = state is None
    seen = list(state.get("seen", [])) if state else []
    seen_set = set(seen)

    new = []  # (disclosureDate, chamber, trade, key)
    errors = []
    for chamber, endpoint in FEEDS.items():
        try:
            trades = fetch_feed(endpoint, api_key)
        except (urllib.error.URLError, RuntimeError, ValueError) as e:
            errors.append(f"{chamber}: {e}")
            continue
        for t in trades:
            k = trade_key(chamber, t)
            if k in seen_set:
                continue
            seen_set.add(k)
            seen.append(k)
            if only_purchases and (t.get("type") or "").strip().lower() != "purchase":
                continue
            new.append((t.get("disclosureDate") or "", chamber, t, k))

    if errors and not new and len(errors) == len(FEEDS):
        msg = "Nepodařilo se stáhnout data:\n" + "\n".join(errors)
        print(msg, file=sys.stderr)
        if not dry_run:
            ntfy_send(server, topic, "⚠️ Chyba sledování obchodů", msg, tags=["warning"])
        return 1

    new.sort(key=lambda x: x[0], reverse=True)
    if first_run and len(new) > first_run_limit:
        new = new[:first_run_limit]

    if not new:
        print("Žádné nové obchody.")
        if env_flag("NOTIFY_EMPTY") and not dry_run:
            ntfy_send(server, topic, "Obchody Kongresu",
                      "Za poslední 3 hodiny nebyly zveřejněny žádné nové obchody.",
                      tags=["zzz"])
    else:
        blocks = [format_trade(ch, t) for _, ch, t, _ in new]
        chunks = chunk_messages(blocks)
        buys = sum(1 for _, _, t, _ in new if (t.get("type") or "").lower() == "purchase")
        for i, body in enumerate(chunks, 1):
            title = f"🏛️ {len(new)} nových obchodů Kongresu (nákupy: {buys})"
            if len(chunks) > 1:
                title += f" [{i}/{len(chunks)}]"
            print(f"--- {title}\n{body}\n")
            if not dry_run:
                ntfy_send(server, topic, title, body, tags=["classical_building"])
                time.sleep(1)

    if errors:
        print("Varování: " + "; ".join(errors), file=sys.stderr)
    if not dry_run:
        save_state(state_file, seen)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--loop", action="store_true", help="běžet stále, kontrola každé 3 hodiny")
    p.add_argument("--dry-run", action="store_true", help="nic neodesílat ani neukládat")
    args = p.parse_args()

    if not args.loop:
        sys.exit(run_once(args.dry_run))
    while True:
        try:
            run_once(args.dry_run)
        except Exception as e:  # smyčka nesmí spadnout
            print(f"Chyba: {e}", file=sys.stderr)
        print(f"Další kontrola za 3 hodiny ({datetime.now():%H:%M})…")
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
