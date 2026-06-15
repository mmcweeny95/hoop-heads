#!/usr/bin/env python3
"""
Hoop Heads Daily Dashboard Updater
===================================
Fetches fresh data from:
  1. Sleeper API  — live rosters, trades, waiver moves
  2. balldontlie.io — real 2024-25 NBA per-game averages

Then writes updated index.html to the repo root.
GitHub Actions commits + pushes it automatically.
"""

import os, re, json, time, requests

# ── CONFIG ───────────────────────────────────────────────────────────────────
LEAGUE_ID   = os.environ.get("SLEEPER_LEAGUE_ID", "1348512479650545664")
BDL_KEY     = os.environ.get("BDL_API_KEY", "")
BDL_BASE    = "https://api.balldontlie.io/v1"
TEMPLATE    = "index.html"   # the HTML file in the repo root
OUTPUT      = "index.html"

# League owner username → team name map
OWNER_MAP = {
    "harrisonplaza":  "Richmond Oilers",
    "cstouff":        "East High Wildcats",
    "wnewell20":      "Flint Tropics",
    "bguendel":       "Monstars",
    "kevinbowman34":  "LA Knights",
    "rpcrane23":      "Lincoln Railsplitters",
    "cklima":         "Hickory Huskers",
    "keyaaron0":      "No Limit Soldiers",
    "colinlesterpsu": "Dimmsdale Ballhogs",
    "mmcweeny":       "WesternU Dolphins",
    "blakeduffin":    "Mt. Vernon Smelters",
    "jtharley":       "Liberty City Penetrators",
}

# ── SLEEPER ───────────────────────────────────────────────────────────────────
def sleeper_get(path):
    url = f"https://api.sleeper.app/v1{path}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()

def fetch_sleeper_rosters():
    print("Fetching Sleeper rosters...")
    rosters = sleeper_get(f"/league/{LEAGUE_ID}/rosters")
    users   = sleeper_get(f"/league/{LEAGUE_ID}/users")

    user_by_id = {u["user_id"]: u for u in users}

    result = []
    for roster in rosters:
        user = user_by_id.get(roster["owner_id"])
        if not user:
            continue
        dn = user["display_name"].lower()
        team_name = OWNER_MAP.get(dn) or user.get("metadata", {}).get("team_name") or user["display_name"]

        starter_ids = set(str(x) for x in (roster.get("starters") or []) if x != "0")
        taxi_ids    = set(str(x) for x in (roster.get("taxi")     or []))
        ir_ids      = set(str(x) for x in (roster.get("reserve")  or []))

        for pid in (roster.get("players") or []):
            pid = str(pid)
            slot = "taxi" if pid in taxi_ids else "ir" if pid in ir_ids else \
                   "starter" if pid in starter_ids else "bench"
            result.append({"id": pid, "team": team_name, "slot": slot})

    print(f"  Got {len(result)} player-roster entries across {len(rosters)} rosters")
    return result

def fetch_sleeper_player_names(ids):
    """Look up unknown player IDs from Sleeper's player database."""
    print(f"  Looking up {len(ids)} unknown player IDs from Sleeper...")
    try:
        data = sleeper_get("/players/nba")
        out = {}
        for pid in ids:
            p = data.get(str(pid))
            if p:
                fn = p.get("first_name","") or ""
                ln = p.get("last_name","")  or ""
                out[str(pid)] = {
                    "name": p.get("full_name") or f"{fn} {ln}".strip() or f"Player #{pid}",
                    "pos":  p.get("position","?"),
                    "nba":  p.get("team","FA"),
                    "age":  p.get("age", 0),
                }
        print(f"  Resolved {len(out)}/{len(ids)} unknown IDs")
        return out
    except Exception as e:
        print(f"  Sleeper player lookup failed: {e}")
        return {}

# ── BALLDONTLIE STATS ─────────────────────────────────────────────────────────
def bdl_get(path, params=None):
    headers = {"Authorization": BDL_KEY}
    url = BDL_BASE + path
    r = requests.get(url, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def fetch_live_stats(player_names):
    """
    Returns dict: name → {pts,reb,ast,stl,blk,tpm,to,gp}
    Uses 2024-25 season (season param = year season STARTED = 2024).
    """
    if not BDL_KEY:
        print("No BDL_API_KEY set — skipping live stats fetch")
        return {}

    print("Fetching live stats from balldontlie.io...")

    # Step 1: resolve names → BDL player IDs
    name_to_id = {}
    for i, name in enumerate(player_names):
        try:
            data = bdl_get("/players", {"search": name, "per_page": 5})
            players = data.get("data", [])
            if players:
                # prefer exact last-name match
                last = name.split()[-1].lower()
                match = next((p for p in players if p["last_name"].lower()==last), players[0])
                name_to_id[name] = match["id"]
        except Exception as e:
            print(f"  Search failed for {name}: {e}")
        # Stay under 60 req/min free tier
        if i % 10 == 9:
            time.sleep(1)

    print(f"  Resolved {len(name_to_id)}/{len(player_names)} player names")

    # Step 2: batch fetch season averages (up to 25 IDs per request)
    all_ids = list(name_to_id.values())
    avg_by_id = {}
    CHUNK = 25

    for i in range(0, len(all_ids), CHUNK):
        chunk = all_ids[i:i+CHUNK]
        try:
            # Try 2024-25 season first
            data = bdl_get("/season_averages", {
                "season": 2024,
                "player_ids[]": chunk,
            })
            for avg in data.get("data", []):
                avg_by_id[avg["player_id"]] = avg
        except Exception as e:
            print(f"  Averages batch failed: {e}")
        if i + CHUNK < len(all_ids):
            time.sleep(0.5)

    print(f"  Got averages for {len(avg_by_id)} players")

    # Step 3: map back to name → stats
    result = {}
    for name, pid in name_to_id.items():
        avg = avg_by_id.get(pid)
        if avg:
            result[name] = {
                "pts": round(avg.get("pts")       or 0, 1),
                "reb": round(avg.get("reb")       or 0, 1),
                "ast": round(avg.get("ast")       or 0, 1),
                "stl": round(avg.get("stl")       or 0, 1),
                "blk": round(avg.get("blk")       or 0, 1),
                "tpm": round(avg.get("fg3m")      or 0, 1),
                "to":  round(avg.get("turnover")  or 0, 1),
                "gp":  avg.get("games_played") or avg.get("gp") or 0,
            }

    print(f"  Successfully mapped {len(result)} player stat lines")
    return result

# ── PATCH HTML ────────────────────────────────────────────────────────────────
def patch_html(rosters, live_stats, player_lookup):
    """
    Read index.html, inject fresh roster + stats data, return updated HTML.
    The JS arrays in the file have sentinel comments we can replace safely.
    """
    with open(TEMPLATE, "r", encoding="utf-8") as f:
        html = f.read()

    # ── 1. Inject timestamps ──────────────────────────────────────────────────
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    now_iso = datetime.now(timezone.utc).isoformat()

    # Replace or insert the auto-update timestamp in the header subtitle
    html = re.sub(
        r'(12-team NBA dynasty[^<"]*)',
        rf'12-team NBA dynasty · Auto-updated {ts}',
        html, count=1
    )

    # Inject roster refresh timestamp into sentinel variable
    html = re.sub(
        r'var LAST_ROSTER_REFRESH = .*?;',
        f'var LAST_ROSTER_REFRESH = "{now_iso}";',
        html, count=1
    )

    # Inject stats refresh timestamp if we actually fetched stats
    if live_stats:
        html = re.sub(
            r'var LAST_STATS_REFRESH  = .*?;',
            f'var LAST_STATS_REFRESH  = "{now_iso}";',
            html, count=1
        )

    # ── 2. Inject live stats into STATS object ────────────────────────────────
    if live_stats:
        stats_lines = []
        for name, s in live_stats.items():
            safe = name.replace("'", "\\'").replace('"', '\\"')
            stats_lines.append(
                f'  "{safe}":{{'
                f'pts:{s["pts"]},reb:{s["reb"]},ast:{s["ast"]},'
                f'stl:{s["stl"]},blk:{s["blk"]},tpm:{s["tpm"]},'
                f'to:{s["to"]},gp:{s["gp"]}}}'
            )
        new_stats_block = "const STATS={\n" + ",\n".join(stats_lines) + "\n};"

        # Replace the STATS block
        html = re.sub(
            r'const STATS=\{.*?\};',
            new_stats_block,
            html, flags=re.DOTALL, count=1
        )
        print(f"  Injected {len(live_stats)} live stat lines into STATS block")

    # ── 3. Inject roster data into LIVE_ROSTER_DATA sentinel ─────────────────
    # We inject a JS variable the dashboard reads on startup (auto-applies rosters)
    roster_json = json.dumps(rosters, separators=(',', ':'))
    player_lookup_json = json.dumps(player_lookup, separators=(',', ':'))

    auto_init = f"""
// ── AUTO-INJECTED BY GITHUB ACTIONS — DO NOT EDIT MANUALLY ──────────────────
// Last updated: {ts}
var AUTO_ROSTER_DATA = {roster_json};
var AUTO_PLAYER_LOOKUP = {player_lookup_json};
"""
    # Replace previous auto-injected block or insert before STATS
    if "AUTO_ROSTER_DATA" in html:
        html = re.sub(
            r'// ── AUTO-INJECTED.*?var AUTO_PLAYER_LOOKUP = \{.*?\};',
            auto_init.strip(),
            html, flags=re.DOTALL, count=1
        )
    else:
        html = html.replace("const STATS={", auto_init + "\nconst STATS={", 1)

    print(f"  Injected {len(rosters)} roster entries")
    return html


def patch_html_auto_apply(html):
    """
    Patch the App's useEffect to auto-apply AUTO_ROSTER_DATA on startup.
    Only needed once — checks if patch is already there.
    """
    if "AUTO_ROSTER_DATA" not in html or "useEffect" in html:
        return html  # already patched or no data

    # Add a useEffect that fires on mount to apply the auto roster data
    auto_apply = """
  // Auto-apply roster data injected by GitHub Actions
  useEffect(function(){
    if(typeof AUTO_ROSTER_DATA !== "undefined" && AUTO_ROSTER_DATA.length > 0){
      // Build players from auto roster using same logic as manual refresh
      var allKnown = [...BASE_PLAYERS, ...customPlayers];
      var idMap = {};
      allKnown.forEach(function(p){ idMap[p.id] = p; });
      var lookup = typeof AUTO_PLAYER_LOOKUP !== "undefined" ? AUTO_PLAYER_LOOKUP : {};
      var updated = AUTO_ROSTER_DATA.map(function(entry){
        var base = idMap[entry.id];
        if(base) return Object.assign({}, base, {slot: entry.slot, team: entry.team});
        var info = lookup[entry.id];
        return {
          id: entry.id,
          name: info ? info.name : "Unknown #"+entry.id,
          pos: info ? info.pos : "?",
          nba: info ? info.nba : "FA",
          age: info ? info.age : 0,
          team: entry.team, slot: entry.slot,
          dyn:30,proj27:28,proj3:25,proj5:20,
          unknown: !info,
          note: info
            ? "Auto-resolved. Use Add Player to set dynasty values."
            : "Not in database — use Add Player to fill in details."
        };
      });
      setLiveRosters(updated);
      setLastRefresh(new Date());
    }
  }, []);  // runs once on mount

"""
    # Inject right after App state declarations (after doRefreshStats)
    insert_after = "  const getFPLive = function(name){"
    if insert_after in html:
        html = html.replace(insert_after, auto_apply + "  const getFPLive = function(name){", 1)

    return html


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*60}")
    print(f"Hoop Heads Daily Update")
    print(f"{'='*60}\n")

    # 1. Fetch rosters from Sleeper
    rosters = []
    player_lookup = {}
    try:
        roster_entries = fetch_sleeper_rosters()
        rosters = roster_entries

        # Look up any IDs not in base player list
        # Read known IDs from the HTML file
        with open(TEMPLATE, "r") as f:
            html = f.read()
        known_ids = set(re.findall(r'\{id:"(\d+)"', html))
        unknown_ids = [e["id"] for e in rosters if e["id"] not in known_ids]
        if unknown_ids:
            player_lookup = fetch_sleeper_player_names(unknown_ids)

    except Exception as e:
        print(f"Sleeper fetch failed: {e}")

    # 2. Gather player names to fetch stats for
    with open(TEMPLATE, "r") as f:
        html_content = f.read()
    # Extract all player names from BASE_PLAYERS in the HTML
    player_names = list(set(re.findall(r'name:"([^"]+)"', html_content)))
    # Filter to likely NBA player names (2+ words, no special keywords)
    player_names = [n for n in player_names
                    if len(n.split()) >= 2
                    and n not in ("Top asset","No notes","New acquisition")]
    print(f"\nFound {len(player_names)} player names to fetch stats for")

    # 3. Fetch live stats
    live_stats = {}
    if BDL_KEY:
        try:
            live_stats = fetch_live_stats(player_names)
        except Exception as e:
            print(f"Stats fetch failed: {e}")
    else:
        print("BDL_API_KEY not set — skipping live stats (set it in GitHub Secrets)")

    # 4. Patch HTML
    print(f"\nPatching {TEMPLATE}...")
    updated_html = patch_html(rosters, live_stats, player_lookup)
    updated_html = patch_html_auto_apply(updated_html)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(updated_html)

    print(f"\n✓ Done — {OUTPUT} updated ({len(updated_html):,} bytes)")
    print(f"  Rosters: {len(rosters)} entries")
    print(f"  Stats updated: {len(live_stats)} players")
    print(f"  Unknown IDs resolved: {len(player_lookup)}")


if __name__ == "__main__":
    main()
