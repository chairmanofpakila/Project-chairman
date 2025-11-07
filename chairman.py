from __future__ import annotations
from nba_api.stats.static import players as static_players
from nba_api.stats.endpoints import playergamelog
import os

# Configure nba_api HTTP behavior early to avoid 403/blocks in some environments.
def _configure_nba_api() -> None:
    try:
        # Prefer HTTPS and set conservative rate limit
        os.environ.setdefault("NBA_API_USE_HTTPS", "true")
        from nba_api.stats.library.http import NBAStatsHTTP  # type: ignore

        # Robust default headers to satisfy stats.nba.com/Akamai checks
        hdrs = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.nba.com",
            "Referer": "https://www.nba.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Connection": "keep-alive",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            # Hints used by nba.com
            "x-nba-stats-origin": "stats",
            "x-nba-stats-token": "true",
        }

        # Some versions expose _DEFAULT_HEADERS, others _HEADERS; try both.
        if hasattr(NBAStatsHTTP, "_DEFAULT_HEADERS"):
            NBAStatsHTTP._DEFAULT_HEADERS.update(hdrs)  # type: ignore[attr-defined]
        if hasattr(NBAStatsHTTP, "_HEADERS"):
            try:
                NBAStatsHTTP._HEADERS.update(hdrs)  # type: ignore[attr-defined]
            except Exception:
                NBAStatsHTTP._HEADERS = hdrs  # type: ignore[attr-defined]

        # Tweak timeouts and rate limit for hosted environments
        if hasattr(NBAStatsHTTP, "_TIMEOUT"):
            NBAStatsHTTP._TIMEOUT = 30  # type: ignore[attr-defined]
        if hasattr(NBAStatsHTTP, "_RATE_LIMIT"):
            NBAStatsHTTP._RATE_LIMIT = 1  # type: ignore[attr-defined]
    except Exception:
        # Best-effort only; if this fails, nba_api will use its defaults
        pass

_configure_nba_api()
from typing import List, Dict, Tuple

def find_player_id(full_name: str) -> int:
    matches = static_players.find_players_by_full_name(full_name)
    if not matches:
        raise ValueError(f"No player found for '{full_name}'")
    # Prefer exact name match if present
    for m in matches:
        if m.get("full_name", "").lower() == full_name.lower():
            return int(m["id"])
    return int(matches[0]["id"])

def _extract_gamelog_rows(gl: playergamelog.PlayerGameLog) -> List[dict]:
    """Best-effort extraction supporting different nba_api response shapes.
    Tries normalized dict, then raw dict (resultSets/resultSet), then DataFrame.
    """
    # 1) Normalized dict
    try:
        nd = gl.get_normalized_dict()
        if isinstance(nd, dict) and "PlayerGameLog" in nd:
            rows = nd["PlayerGameLog"]
            if isinstance(rows, list):
                return rows
    except Exception:
        pass

    # 2) Raw dict with resultSets/resultSet
    try:
        rd = gl.get_dict()
        rs = rd.get("resultSets") or rd.get("resultSet")
        if isinstance(rs, list) and rs:
            headers = rs[0].get("headers", [])
            rowset = rs[0].get("rowSet", [])
        elif isinstance(rs, dict):
            headers = rs.get("headers", [])
            rowset = rs.get("rowSet", [])
        else:
            headers, rowset = [], []
        if headers and rowset:
            return [dict(zip(headers, row)) for row in rowset]
    except Exception:
        pass

    # 3) Fallback: pandas DataFrame
    try:
        dfs = gl.get_data_frames()
        if dfs:
            df = dfs[0]
            return df.to_dict(orient="records")  # type: ignore[attr-defined]
    except Exception:
        pass

    raise RuntimeError("Unable to parse PlayerGameLog response (no resultSets/normalized data).")


def _last_n_averages_by_id(player_id: int, season: str, n: int = 10) -> dict:
    gl = playergamelog.PlayerGameLog(
        player_id=player_id,
        season=season,
        season_type_all_star="Regular Season",
        timeout=30,
    )
    games = _extract_gamelog_rows(gl)

    # nba_api returns most-recent first; slice the last N games actually means first N entries
    recent = games[:n] if len(games) >= n else games

    # Totals for rate stats (to compute true percentages)
    fgm = sum(float(g["FGM"]) for g in recent)
    fga = sum(float(g["FGA"]) for g in recent)
    ftm = sum(float(g["FTM"]) for g in recent)
    fta = sum(float(g["FTA"]) for g in recent)

    # Simple sums for counting stats
    pts = sum(float(g["PTS"]) for g in recent)
    reb = sum(float(g["REB"]) for g in recent)
    ast = sum(float(g["AST"]) for g in recent)
    stl = sum(float(g["STL"]) for g in recent)
    blk = sum(float(g["BLK"]) for g in recent)
    tov = sum(float(g["TOV"]) for g in recent)
    threes_made = sum(float(g["FG3M"]) for g in recent)

    games_count = max(1, len(recent))  # avoid division by zero if no games

    return {
        "games_used": len(recent),
        # per-game counting stats
        "PTS": pts / games_count,
        "REB": reb / games_count,
        "AST": ast / games_count,
        "STL": stl / games_count,
        "BLK": blk / games_count,
        "TOV": tov / games_count,
        "3PM": threes_made / games_count,
        # per-game makes/attempts to weight team percentages later
        "FGM_pg": fgm / games_count,
        "FGA_pg": fga / games_count,
        "FTM_pg": ftm / games_count,
        "FTA_pg": fta / games_count,
        # attempt-weighted percentages over the window (player-level)
        "FG%": (fgm / fga) if fga else 0.0,
        "FT%": (ftm / fta) if fta else 0.0,
    }

def last_n_averages(full_name: str, season: str, n: int = 10) -> dict:
    pid = find_player_id(full_name)
    return _last_n_averages_by_id(pid, season, n)


def search_players(query: str) -> List[dict]:
    """Return players matching a name fragment, active first."""
    results = static_players.find_players_by_full_name(query)
    # De-duplicate by id and sort active first then name
    seen = set()
    deduped: List[dict] = []
    for r in results:
        pid = int(r.get("id"))
        if pid not in seen:
            seen.add(pid)
            deduped.append(r)
    deduped.sort(key=lambda r: (not bool(r.get("is_active", False)), r.get("full_name", "")))
    return deduped


def prompt_select_player() -> Tuple[int, str] | None:
    """Interactive prompt to search and select a single player.
    Returns (player_id, full_name) or None if aborted.
    """
    while True:
        query = input("Search player (or leave blank to cancel): ").strip()
        if not query:
            return None
        matches = search_players(query)
        if not matches:
            print("No matches. Try again.")
            continue
        print("Matches:")
        for idx, m in enumerate(matches, start=1):
            active = "ACTIVE" if m.get("is_active") else ""
            print(f"  [{idx:>2}] {m.get('full_name')} {active}")
        sel = input("Pick number (or 'r' to retry): ").strip()
        if sel.lower() == 'r':
            continue
        try:
            i = int(sel)
        except ValueError:
            print("Invalid input. Enter a number from the list.")
            continue
        if 1 <= i <= len(matches):
            chosen = matches[i - 1]
            return int(chosen["id"]), str(chosen["full_name"])
        else:
            print("Out of range. Try again.")


def build_team(team_name: str, season: str, n: int) -> List[Tuple[int, str]]:
    """Interactively build a team list of (player_id, full_name)."""
    roster: List[Tuple[int, str]] = []
    print(f"\n--- Build {team_name} ---")
    print("Type a name fragment to search, select a player, repeat. Type 'done' to finish.")
    while True:
        cmd = input(f"Add to {team_name} (search or 'done'): ").strip()
        if cmd.lower() == 'done':
            if not roster:
                print("Team is empty; add at least one player.")
                continue
            return roster
        if not cmd:
            continue
        # Use provided fragment for initial search, then let user confirm or refine
        matches = search_players(cmd)
        if not matches:
            print("No matches. Try again.")
            continue
        print("Matches:")
        for idx, m in enumerate(matches, start=1):
            active = "ACTIVE" if m.get("is_active") else ""
            print(f"  [{idx:>2}] {m.get('full_name')} {active}")
        sel = input("Pick number (or 'r' to retry): ").strip()
        if sel.lower() == 'r':
            continue
        try:
            i = int(sel)
        except ValueError:
            print("Invalid input. Enter a number from the list.")
            continue
        if 1 <= i <= len(matches):
            chosen = matches[i - 1]
            pid = int(chosen["id"])
            name = str(chosen["full_name"]) 
            if any(pid == p for p, _ in roster):
                print(f"{name} already on {team_name}.")
                continue
            roster.append((pid, name))
            print(f"Added {name} to {team_name}.")
        else:
            print("Out of range. Try again.")


def compute_team_stats(roster: List[Tuple[int, str]], season: str, n: int) -> Dict[str, float]:
    """Aggregate per-game team stats based on last-N window per player.
    Percentages are properly weighted by attempts.
    """
    totals = {
        "PTS": 0.0, "REB": 0.0, "AST": 0.0, "STL": 0.0, "BLK": 0.0, "TOV": 0.0, "3PM": 0.0,
        "FGM_pg": 0.0, "FGA_pg": 0.0, "FTM_pg": 0.0, "FTA_pg": 0.0,
    }
    for pid, name in roster:
        try:
            avgs = _last_n_averages_by_id(pid, season, n)
        except Exception as e:
            print(f"Warning: failed to fetch {name}: {e}")
            continue
        for k in ["PTS", "REB", "AST", "STL", "BLK", "TOV", "3PM", "FGM_pg", "FGA_pg", "FTM_pg", "FTA_pg"]:
            totals[k] += float(avgs.get(k, 0.0))
    # Compute team percentages (attempt-weighted across players)
    fg_pct = (totals["FGM_pg"] / totals["FGA_pg"]) if totals["FGA_pg"] else 0.0
    ft_pct = (totals["FTM_pg"] / totals["FTA_pg"]) if totals["FTA_pg"] else 0.0
    out = {k: v for k, v in totals.items() if k not in ("FGM_pg", "FGA_pg", "FTM_pg", "FTA_pg")}
    out["FG%"] = fg_pct
    out["FT%"] = ft_pct
    return out


def print_team(team_name: str, roster: List[Tuple[int, str]], stats: Dict[str, float]) -> None:
    print(f"\n{team_name} roster ({len(roster)}):")
    print("  " + ", ".join(name for _, name in roster))
    print(f"  PTS {stats['PTS']:.2f}  REB {stats['REB']:.2f}  AST {stats['AST']:.2f}")
    print(f"  STL {stats['STL']:.2f}  BLK {stats['BLK']:.2f}  TOV {stats['TOV']:.2f}  3PM {stats['3PM']:.2f}")
    print(f"  FG% {stats['FG%']:.3f}  FT% {stats['FT%']:.3f}")


def print_comparison(team1_name: str, s1: Dict[str, float], team2_name: str, s2: Dict[str, float]) -> None:
    print("\n--- Category Comparison (per-game, last-N window) ---")
    cats = ["FG%", "FT%", "3PM", "PTS", "REB", "AST", "STL", "BLK", "TOV"]
    headers = f"{'Category':<6}  {team1_name:>12}  {team2_name:>12}   Lead"
    print(headers)
    print("-" * len(headers))
    for c in cats:
        v1 = s1.get(c, 0.0)
        v2 = s2.get(c, 0.0)
        # In 9-cat, lower TOV is better
        if c == "TOV":
            lead = team1_name if v1 < v2 else (team2_name if v2 < v1 else "=")
        else:
            lead = team1_name if v1 > v2 else (team2_name if v2 > v1 else "=")
        fmt = ".3f" if c in ("FG%", "FT%") else ".2f"
        spec = f">12{fmt}"
        print(f"{c:<6}  {format(v1, spec)}  {format(v2, spec)}   {lead}")

if __name__ == "__main__":
    print("Fantasy trade helper - compare two teams using last-N games per-player averages.")
    # Fixed season per request
    season = "2025-26"
    try:
        n_in = input("Number of most recent games to use [10]: ").strip() or "10"
        n = int(n_in)
        if n <= 0:
            raise ValueError
    except ValueError:
        print("Invalid number, using 10.")
        n = 10

    team1 = build_team("My Team", season, n)
    team2 = build_team("Team of the opponent", season, n)

    stats1 = compute_team_stats(team1, season, n)
    stats2 = compute_team_stats(team2, season, n)

    print_team("My Team", team1, stats1)
    print_team("Team of the opponent", team2, stats2)
    print_comparison("My Team", stats1, "Team of the opponent", stats2)
