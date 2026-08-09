#!/usr/bin/env python3
"""Auslieferung «Check my relay list» (1'000 sats).

Für einen fremden npub/hex-Pubkey:
1. NIP-65-Liste (kind 10002) von den Seed-Relays holen (neueste gewinnt).
2. Jedes beworbene Relay live prüfen: Verbindung, und ob die Notizen des
   Kunden dort tatsächlich abrufbar sind (REQ kinds=[1] authors=[pk]).
3. Status aus dem letzten Tages-Audit danebenstellen (Klasse + Peer-Kontext).

Ausgabe: JSON nach data/checks/<pk16>.json und Markdown-Report auf stdout
bzw. data/checks/<pk16>.md — der Markdown-Text ist die DM-/Mail-Antwort.

Aufruf: python3 scripts/relay_list_check.py <npub1...|hex> [--out data/checks]
Läuft idealerweise auf dem GitHub-Runner (neutraler Vantage Point); aus der
Sandbox sind "timeout/refused" nicht von Proxy-Blockaden unterscheidbar —
der Vantage Point steht deshalb im Report.
"""
import asyncio, json, sys, os, datetime, socket

try:
    import websockets
except ImportError:
    sys.exit("pip install websockets")

SEED_RELAYS = ["wss://relay.primal.net", "wss://nos.lol", "wss://relay.damus.io",
               "wss://offchain.pub", "wss://nostr.mom", "wss://relay.nostr.band"]
AUDIT_JSON = os.path.join(os.path.dirname(__file__), "..", "data", "nostr", "relay-audit.json")
CONNECT_TIMEOUT = 8
RECV_TIMEOUT = 10


def npub_to_hex(npub: str) -> str:
    """Minimal bech32-Decoder (BIP-173) für npub — keine Fremdlib nötig."""
    CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    if not npub.startswith("npub1"):
        raise ValueError("kein npub")
    data = [CHARSET.index(c) for c in npub[5:]]
    # 5-bit → 8-bit, Checksumme (letzte 6 Zeichen) abschneiden
    bits, acc, out = 0, 0, []
    for v in data[:-6]:
        acc = (acc << 5) | v
        bits += 5
        if bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return bytes(out).hex()


async def fetch_nip65(pk: str):
    """Neueste kind-10002-Liste über die Seed-Relays."""
    async def one(relay):
        try:
            async with websockets.connect(relay, open_timeout=CONNECT_TIMEOUT, close_timeout=3) as ws:
                await ws.send(json.dumps(["REQ", "n65", {"kinds": [10002], "authors": [pk], "limit": 3}]))
                evs = []
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
                    except asyncio.TimeoutError:
                        break
                    d = json.loads(msg)
                    if d[0] == "EVENT":
                        evs.append(d[2])
                    elif d[0] == "EOSE":
                        break
                return evs
        except Exception:
            return []
    all_evs = [e for evs in await asyncio.gather(*[one(r) for r in SEED_RELAYS]) for e in evs]
    if not all_evs:
        return None
    return max(all_evs, key=lambda e: e["created_at"])


async def probe_relay(relay: str, pk: str):
    """Verbinden + prüfen, ob Notizen des Kunden abrufbar sind."""
    res = {"relay": relay, "connect": False, "events_found": 0, "eose": False,
           "auth_required": False, "error": None, "latency_ms": None}
    t0 = asyncio.get_event_loop().time()
    try:
        async with websockets.connect(relay, open_timeout=CONNECT_TIMEOUT, close_timeout=3) as ws:
            res["connect"] = True
            res["latency_ms"] = round((asyncio.get_event_loop().time() - t0) * 1000)
            await ws.send(json.dumps(["REQ", "chk", {"kinds": [1], "authors": [pk], "limit": 5}]))
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
                except asyncio.TimeoutError:
                    res["error"] = "recv_timeout"
                    break
                d = json.loads(msg)
                if d[0] == "EVENT":
                    res["events_found"] += 1
                elif d[0] == "EOSE":
                    res["eose"] = True
                    break
                elif d[0] == "AUTH":
                    res["auth_required"] = True
                elif d[0] == "CLOSED":
                    res["error"] = f"closed: {d[2][:60] if len(d) > 2 else ''}"
                    break
                elif d[0] == "NOTICE":
                    res.setdefault("notice", str(d[1])[:60])
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {str(e)[:60]}"
    return res


def verdict(p, marker):
    if not p["connect"]:
        return "DEAD — connection failed"
    if p["events_found"] > 0:
        return "OK — your notes are retrievable here"
    if p["auth_required"] and not p["eose"]:
        return "AUTH — relay requires login; reading without auth not possible"
    if p["eose"]:
        if marker == "read":
            return "EMPTY — reachable, none of your notes (fine for a read-only entry)"
        return "EMPTY — reachable, but NONE of your notes are stored here"
    return f"UNCLEAR — {p.get('error') or 'no clean response'}"


async def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    arg = sys.argv[1].strip()
    pk = npub_to_hex(arg) if arg.startswith("npub1") else arg.lower()
    outdir = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else os.path.join(
        os.path.dirname(__file__), "..", "data", "checks")
    os.makedirs(outdir, exist_ok=True)

    nip65 = await fetch_nip65(pk)
    if not nip65:
        print(f"FEHLER: keine NIP-65-Liste (kind 10002) für {pk[:16]}… auf den Seed-Relays gefunden.")
        sys.exit(2)

    relays = []  # (url, marker)
    for t in nip65["tags"]:
        if t and t[0] == "r" and len(t) > 1:
            url = t[1].rstrip("/")
            marker = t[2] if len(t) > 2 else "read+write"
            relays.append((url, marker))
    if not relays:
        print("FEHLER: NIP-65-Liste enthält keine r-Tags.")
        sys.exit(2)

    # Audit-Kontext laden
    audit = {}
    try:
        for r in json.load(open(AUDIT_JSON))["results"]:
            audit[r["relay"].rstrip("/")] = r
    except Exception:
        pass

    probes = await asyncio.gather(*[probe_relay(u, pk) for u, _ in relays])
    listed_dt = datetime.datetime.utcfromtimestamp(nip65["created_at"]).strftime("%Y-%m-%d")
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    vantage = os.environ.get("VANTAGE", socket.gethostname())

    rows, n_ok, n_dead = [], 0, 0
    for (url, marker), p in zip(relays, probes):
        v = verdict(p, marker)
        if v.startswith("OK"):
            n_ok += 1
        if v.startswith("DEAD"):
            n_dead += 1
        a = audit.get(url)
        rows.append({"relay": url, "marker": marker, "verdict": v, "probe": p,
                     "audit_class": a["class"] if a else None,
                     "advertised_by_pubkeys": a["advertised_by_pubkeys"] if a else None})

    result = {"pubkey": pk, "checked_at": now, "vantage": vantage,
              "nip65_created": listed_dt, "relays_listed": len(relays),
              "ok": n_ok, "dead": n_dead, "rows": rows}
    pk16 = pk[:16]
    json.dump(result, open(os.path.join(outdir, f"{pk16}.json"), "w"), indent=1, ensure_ascii=False)

    # Markdown-Report (= Text für die DM/Mail an den Kunden)
    md = [f"# Relay-List Check — {arg[:20]}…",
          f"Checked {now} from {vantage}. Your NIP-65 list is dated {listed_dt} "
          f"and names {len(relays)} relays. **{n_ok} of them serve your notes; {n_dead} unreachable.**", ""]
    for row in rows:
        p = row["probe"]
        lat = f", {p['latency_ms']} ms" if p["latency_ms"] else ""
        peer = f" · advertised by {row['advertised_by_pubkeys']} users in our daily audit" if row["advertised_by_pubkeys"] else ""
        md.append(f"- `{row['relay']}` ({row['marker']}): **{row['verdict']}**{lat}{peer}")
    md += ["", "Method: live WebSocket probe, REQ for your own kind-1 notes (limit 5). "
           "Audit context: seipa.github.io/agent-hq-page/relay-audit.html"]
    md_text = "\n".join(md)
    open(os.path.join(outdir, f"{pk16}.md"), "w").write(md_text)
    print(md_text)

asyncio.run(main())
