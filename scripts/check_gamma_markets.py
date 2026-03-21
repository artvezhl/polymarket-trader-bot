#!/usr/bin/env python3
import json
import urllib.request

CONDITION_IDS = [
    "0x01e18fb46daf1c2707b971c766f537daa9bf29930d8750f1b1a046c6bc679a3c",
    "0x052f8245f65adb09b2525a45dd61e8bc279354a2ac3eb06f655a39b471431c15",
    "0x056c4eb11efdf10e225b22b1e7061a587200320c4deb2c54415c98814be09f70",
    "0x087b849fd03d3fee0f353c5d60958f19a1fc3c71e35ebede8a2222ee34e5e014",
]

for cid in CONDITION_IDS:
    url = f"https://gamma-api.polymarket.com/markets?conditionId={cid}"
    req = urllib.request.Request(url, headers={"User-Agent": "PolymarketBot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        r = json.loads(resp.read().decode())

    if not r:
        print(f"{cid[:12]}… → НЕ НАЙДЕН")
        continue

    m = r[0]
    print(f"condition_id: {cid[:12]}…")
    print(f"  question:   {m.get('question')}")
    print(f"  resolved:   {m.get('resolved')}")
    print(f"  neg_risk:   {m.get('negRisk')}")
    print(f"  closed:     {m.get('closed')}")
    print(f"  tokens:     {[t.get('token_id') for t in m.get('tokens', [])]}")
    print()
