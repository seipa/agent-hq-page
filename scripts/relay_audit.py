#!/usr/bin/env python3
"""
Nostr Relay Reality Check — reproducible measurement.

Question: of the relays that active Nostr users actually advertise in their
NIP-65 relay lists (kind 10002), how many are reachable and serving events?

Method (three steps, all in-protocol, no third-party API):
  1. SAMPLE FRAME: connect to a set of seed relays, pull the most recent
     kind-1 notes, keep the distinct author pubkeys. That is "people who
     actually posted recently" — not a directory, not a scrape.
  2. RELAY LISTS: for those authors, query kind-10002 (NIP-65) against several
     index relays. Keep the newest list per pubkey (kind 10002 is replaceable).
  3. PROBE: open a WebSocket to every advertised relay URL and send
     REQ {"kinds":[1],"limit":1}. Classify by what actually comes back.

Everything is written to data/nostr/: the raw probe results, a CSV, and a
summary. Anyone can re-run this file and compare.

Known limitations (documented on purpose):
  - One vantage point (GitHub Actions runner). A relay unreachable from here
    may be reachable elsewhere, e.g. if it geo-blocks or is behind Tor.
  - The frame is biased toward users whose notes reach the seed relays.
  - A relay that answers AUTH or requires payment is not "broken" — it is
    counted in its own class, not as a failure.
  - Bridged/bot accounts (Mastodon bridges, test swarms) are part of Nostr and
    are not filtered out; the fingerprint count next to the pubkey count makes
    swarms visible instead of hiding them.
"""
import asyncio
import collections
import json
import ssl
import sys
import time

import websockets

SEEDS = [
    "wss://relay.primal.net", "wss://nos.lol", "wss://offchain.pub",
    "wss://nostr.mom", "wss://relay.snort.social", "wss://relay.damus.io",
]
INDEX = [
    "wss://purplepag.es", "wss://relay.primal.net", "wss://nos.lol",
    "wss://relay.nostr.net", "wss://nostr.wine", "wss://relay.damus.io",
]
NOTE_LIMIT = 500
PROBE_CONCURRENCY = 25
CONNECT_TIMEOUT = 12
READ_TIMEOUT = 15


async def req(relay, filt, timeout=20):
    """One REQ against one relay. Returns (status, events)."""
    evs = []
    try:
        async with websockets.connect(
            relay, open_timeout=CONNECT_TIMEOUT, close_timeout=3, max_size=None
        ) as ws:
            await ws.send(json.dumps(["REQ", "q", filt]))
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
                d = json.loads(msg)
                if d[0] == "EVENT":
                    evs.append(d[2])
                elif d[0] in ("EOSE", "CLOSED", "NOTICE", "AUTH"):
                    break
    except Exception as e:  # noqa: BLE001 - status is data here
        return f"FAIL {type(e).__name__}: {str(e)[:80]}", evs
    return "OK", evs


def classify(r):
    if r["error"]:
        e = r["error"]
        if "InvalidStatus" in e or "HTTP" in e:
            return "http_error"
        if "gaierror" in e or "getaddrinfo" in e or "Name or service" in e:
            return "dns_fail"
        if "SSL" in e or "ertificate" in e:
            return "tls_fail"
        if "Timeout" in e or "timed out" in e:
            return "timeout"
        if "ConnectionReset" in e or "ConnectionRefused" in e or "Connect call failed" in e:
            return "refused"
        return "other_fail"
    if r["auth_required"]:
        return "auth_required"
    if r["payment_required"]:
        return "payment_required"
    if r["closed_reason"]:
        return "rejected"
    if r["events"] > 0:
        return "ok_serving"
    if r["eose"]:
        return "ok_empty"
    return "no_response"


async def probe(url, sem):
    r = {"relay": url, "error": None, "connect_ms": None, "first_ms": None,
         "events": 0, "eose": False, "auth_required": False,
         "payment_required": False, "closed_reason": None, "notice": None}
    async with sem:
        t0 = time.perf_counter()
        try:
            kw = {"open_timeout": CONNECT_TIMEOUT, "close_timeout": 3, "max_size": 2 ** 20}
            if url.startswith("wss://"):
                kw["ssl"] = ssl.create_default_context()
            async with websockets.connect(url, **kw) as ws:
                r["connect_ms"] = round((time.perf_counter() - t0) * 1000)
                await ws.send(json.dumps(["REQ", "p", {"kinds": [1], "limit": 1}]))
                deadline = time.perf_counter() + READ_TIMEOUT
                while time.perf_counter() < deadline:
                    try:
                        msg = await asyncio.wait_for(
                            ws.recv(), timeout=max(1, deadline - time.perf_counter()))
                    except asyncio.TimeoutError:
                        break
                    if r["first_ms"] is None:
                        r["first_ms"] = round((time.perf_counter() - t0) * 1000)
                    try:
                        d = json.loads(msg)
                    except Exception:
                        continue
                    if not isinstance(d, list) or not d:
                        continue
                    t = d[0]
                    if t == "EVENT":
                        r["events"] += 1
                    elif t == "EOSE":
                        r["eose"] = True
                        break
                    elif t == "AUTH":
                        r["auth_required"] = True
                        break
                    elif t == "CLOSED":
                        reason = (d[2] if len(d) > 2 else "")[:200]
                        r["closed_reason"] = reason
                        low = reason.lower()
                        r["auth_required"] |= "auth" in low
                        r["payment_required"] |= ("pay" in low or "sats" in low
                                                  or "subscription" in low)
                        break
                    elif t == "NOTICE":
                        n = (d[1] if len(d) > 1 else "")[:200]
                        r["notice"] = n
                        low = n.lower()
                        r["auth_required"] |= "auth" in low
                        r["payment_required"] |= ("pay" in low or "sats" in low)
        except Exception as e:  # noqa: BLE001
            r["error"] = f"{type(e).__name__}: {str(e)[:140]}"
    r["class"] = classify(r)
    return r


async def main():
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # 1) sample frame: recent note authors
    res = await asyncio.gather(*[req(r, {"kinds": [1], "limit": NOTE_LIMIT}) for r in SEEDS])
    seed_status = {}
    authors = {}
    for (st, evs), relay in zip(res, SEEDS):
        seed_status[relay] = {"status": st, "notes": len(evs)}
        for e in evs:
            authors.setdefault(e["pubkey"], e["created_at"])
    pks = list(authors)
    print(f"authors sampled: {len(pks)}", file=sys.stderr)

    # 2) their NIP-65 lists
    batches = [pks[i:i + 100] for i in range(0, len(pks), 100)]
    tasks = [req(idx, {"kinds": [10002], "authors": b, "limit": 200})
             for b in batches for idx in INDEX]
    out = await asyncio.gather(*tasks)
    newest = {}
    for _st, evs in out:
        for e in evs:
            pk = e["pubkey"]
            if pk not in newest or e["created_at"] > newest[pk]["created_at"]:
                newest[pk] = e
    lists = [{"pubkey": e["pubkey"], "created_at": e["created_at"],
              "relays": sorted({t[1].strip().rstrip("/").lower()
                                for t in e.get("tags", [])
                                if t and t[0] == "r" and len(t) > 1
                                and t[1].strip().startswith("ws")})}
             for e in newest.values()]
    print(f"authors with NIP-65 list: {len(lists)}", file=sys.stderr)

    by_pubkey = collections.Counter()
    by_fingerprint = collections.Counter()
    seen_fp = collections.defaultdict(set)
    for l in lists:
        fp = tuple(l["relays"])
        for u in l["relays"]:
            by_pubkey[u] += 1
            seen_fp[u].add(fp)
    for u, fps in seen_fp.items():
        by_fingerprint[u] = len(fps)

    relays = sorted(by_pubkey)
    print(f"distinct relays advertised: {len(relays)}", file=sys.stderr)

    # 3) probe
    sem = asyncio.Semaphore(PROBE_CONCURRENCY)
    results = await asyncio.gather(*[probe(u, sem) for u in relays])
    for r in results:
        r["advertised_by_pubkeys"] = by_pubkey[r["relay"]]
        r["advertised_by_distinct_lists"] = by_fingerprint[r["relay"]]

    counts = collections.Counter(r["class"] for r in results)
    lat = sorted(r["connect_ms"] for r in results if r["connect_ms"])
    reachable = [r for r in results
                 if r["class"] in ("ok_serving", "ok_empty", "auth_required",
                                   "payment_required", "rejected")]

    summary = {
        "started_at": started,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "vantage": "GitHub Actions runner (ubuntu-latest)",
        "seed_relays": seed_status,
        "authors_sampled": len(pks),
        "authors_with_list": len(lists),
        "relays_advertised": len(relays),
        "classes": dict(counts.most_common()),
        "reachable": len(reachable),
        "serving_events": counts.get("ok_serving", 0),
        "connect_ms_median": lat[len(lat) // 2] if lat else None,
        "connect_ms_p90": lat[int(len(lat) * 0.9)] if lat else None,
    }

    import os
    os.makedirs("data/nostr", exist_ok=True)
    json.dump({"summary": summary, "results": results},
              open("data/nostr/relay-audit.json", "w"), indent=1)
    json.dump({"summary": summary, "lists": lists},
              open("data/nostr/relay-lists.json", "w"), indent=1)

    # Zeitreihe: ein kompakter Snapshot pro Tag (letzter Lauf des Tages gewinnt).
    # Der eigentliche Wert des Datensatzes entsteht erst im Tagesvergleich —
    # "heute tot" vs. "seit einer Woche tot" ist ohne Historie nicht trennbar.
    os.makedirs("data/nostr/history", exist_ok=True)
    json.dump({"date": started[:10], "summary": summary,
               "relay_classes": {r["relay"]: r["class"] for r in results}},
              open(f"data/nostr/history/{started[:10]}.json", "w"), indent=1)

    with open("data/nostr/relay-audit.csv", "w") as f:
        f.write("relay,class,advertised_by_pubkeys,advertised_by_distinct_lists,"
                "connect_ms,first_response_ms,events,error\n")
        for r in sorted(results, key=lambda x: (-x["advertised_by_pubkeys"], x["relay"])):
            err = (r["error"] or "").replace(",", ";").replace("\n", " ")
            f.write(f'{r["relay"]},{r["class"]},{r["advertised_by_pubkeys"]},'
                    f'{r["advertised_by_distinct_lists"]},{r["connect_ms"] or ""},'
                    f'{r["first_ms"] or ""},{r["events"]},"{err}"\n')

    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
