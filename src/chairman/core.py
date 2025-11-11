from __future__ import annotations

import os
from typing import Dict, List, Tuple

import requests
from nba_api.stats.static import players as static_players
from nba_api.stats.endpoints import playergamelog


# Configure nba_api HTTP behavior early to avoid 403/blocks in some environments.
def _configure_nba_api() -> None:
    try:
        os.environ.setdefault("NBA_API_USE_HTTPS", "true")
        from nba_api.stats.library.http import NBAStatsHTTP as _HTTP  # type: ignore

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
            "x-nba-stats-origin": "stats",
            "x-nba-stats-token": "true",
        }

        if hasattr(_HTTP, "_DEFAULT_HEADERS"):
            _HTTP._DEFAULT_HEADERS.update(hdrs)  # type: ignore[attr-defined]
        if hasattr(_HTTP, "_HEADERS"):
            try:
                _HTTP._HEADERS.update(hdrs)  # type: ignore[attr-defined]
            except Exception:
                _HTTP._HEADERS = hdrs  # type: ignore[attr-defined]

        if hasattr(_HTTP, "_TIMEOUT"):
            _HTTP._TIMEOUT = 30  # type: ignore[attr-defined]
        if hasattr(_HTTP, "_RATE_LIMIT"):
            _HTTP._RATE_LIMIT = 1  # type: ignore[attr-defined]
    except Exception:
        pass


_configure_nba_api()


def _normalize_nba_season(season: str) -> str:
    s = (season or "").strip()
    if not s:
        return s
    # Already in NBA format YYYY-YY
    if "-" in s and len(s.split("-", 1)[1]) in (1, 2):
        return s
    try:
        if "-" in s:
            start, end = s.split("-", 1)
            sy = int(start[:4])
            ey = int(end[:4]) if len(end) == 4 else int(end)
            # Normalize end to last two digits
            return f"{sy}-{str(ey)[-2:]}"
        # Single year like 2024 -> 2024-25
        sy = int(s[:4])
        ey2 = (sy + 1) % 100
        return f"{sy}-{ey2:02d}"
    except Exception:
        return season


def _season_start_year(season: str) -> int | None:
    s = (season or "").strip()
    try:
        if "-" in s:
            sy = int(s.split("-", 1)[0][:4])
            return sy
        return int(s[:4])
    except Exception:
        return None


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

    raise RuntimeError(
        "Unable to parse PlayerGameLog response (no resultSets/normalized data)."
    )


# -------- balldontlie fallback (best-effort) --------
_BDL_BASE = os.getenv("BALLDONTLIE_API_URL", "https://api.balldontlie.io/v1")
_BDL_KEY = os.getenv("BALLDONTLIE_API_KEY", "").strip()


def _bdl_headers() -> Dict[str, str]:
    if _BDL_KEY:
        # v2 commonly uses Bearer; keep flexible
        return {"Authorization": f"Bearer {_BDL_KEY}"}
    return {}


def _bdl_find_player_id(full_name: str) -> int | None:
    try:
        resp = requests.get(
            f"{_BDL_BASE}/players",
            params={"search": full_name, "per_page": 50},
            timeout=20,
            headers=_bdl_headers(),
        )
        resp.raise_for_status()
        data = resp.json() or {}
        items = data.get("data", [])
        if not items:
            return None
        # Prefer exact full name match
        def full_name_of(it: dict) -> str:
            return (f"{it.get('first_name','').strip()} {it.get('last_name','').strip()}").strip()

        for it in items:
            if full_name_of(it).lower() == full_name.lower():
                return int(it.get("id"))
        return int(items[0].get("id"))
    except Exception:
        return None


def _bdl_last_n_averages(full_name: str, season: str, n: int) -> dict:
    pid = _bdl_find_player_id(full_name)
    if not pid:
        raise RuntimeError(f"balldontlie: no player found for '{full_name}'")
    sy = _season_start_year(season)
    if sy is None:
        raise RuntimeError("balldontlie: invalid season format")
    # Pull up to N most recent regular-season games
    try:
        resp = requests.get(
            f"{_BDL_BASE}/stats",
            params={
                "player_ids[]": pid,
                "seasons[]": sy,
                "postseason": "false",
                "per_page": max(25, n),
                "page": 1,
                "sort": "game.date",
                "order": "desc",
            },
            timeout=20,
            headers=_bdl_headers(),
        )
        resp.raise_for_status()
        data = resp.json() or {}
        games = data.get("data", [])
    except Exception as e:
        raise RuntimeError(f"balldontlie fetch failed: {e}")

    recent = games[:n] if len(games) >= n else games
    if not recent:
        return {
            "games_used": 0,
            "PTS": 0.0,
            "REB": 0.0,
            "AST": 0.0,
            "STL": 0.0,
            "BLK": 0.0,
            "TOV": 0.0,
            "3PM": 0.0,
            "FGM_pg": 0.0,
            "FGA_pg": 0.0,
            "FTM_pg": 0.0,
            "FTA_pg": 0.0,
            "FG%": 0.0,
            "FT%": 0.0,
        }

    # Aggregate from balldontlie field names
    fgm = sum(float(g.get("fgm", 0)) for g in recent)
    fga = sum(float(g.get("fga", 0)) for g in recent)
    ftm = sum(float(g.get("ftm", 0)) for g in recent)
    fta = sum(float(g.get("fta", 0)) for g in recent)
    pts = sum(float(g.get("pts", 0)) for g in recent)
    reb = sum(float(g.get("reb", 0)) for g in recent)
    ast = sum(float(g.get("ast", 0)) for g in recent)
    stl = sum(float(g.get("stl", 0)) for g in recent)
    blk = sum(float(g.get("blk", 0)) for g in recent)
    tov = sum(float(g.get("turnover", 0)) for g in recent)
    threes_made = sum(float(g.get("fg3m", 0)) for g in recent)

    games_count = max(1, len(recent))
    return {
        "games_used": len(recent),
        "PTS": pts / games_count,
        "REB": reb / games_count,
        "AST": ast / games_count,
        "STL": stl / games_count,
        "BLK": blk / games_count,
        "TOV": tov / games_count,
        "3PM": threes_made / games_count,
        "FGM_pg": fgm / games_count,
        "FGA_pg": fga / games_count,
        "FTM_pg": ftm / games_count,
        "FTA_pg": fta / games_count,
        "FG%": (fgm / fga) if fga else 0.0,
        "FT%": (ftm / fta) if fta else 0.0,
    }


def _last_n_averages_by_id(
    player_id: int, season: str, n: int = 10, *, full_name: str | None = None
) -> dict:
    normalized = _normalize_nba_season(season)
    try:
        gl = playergamelog.PlayerGameLog(
            player_id=player_id,
            season=normalized or season,
            season_type_all_star="Regular Season",
            timeout=30,
        )
        games = _extract_gamelog_rows(gl)

        recent = games[:n] if len(games) >= n else games

        fgm = sum(float(g["FGM"]) for g in recent)
        fga = sum(float(g["FGA"]) for g in recent)
        ftm = sum(float(g["FTM"]) for g in recent)
        fta = sum(float(g["FTA"]) for g in recent)

        pts = sum(float(g["PTS"]) for g in recent)
        reb = sum(float(g["REB"]) for g in recent)
        ast = sum(float(g["AST"]) for g in recent)
        stl = sum(float(g["STL"]) for g in recent)
        blk = sum(float(g["BLK"]) for g in recent)
        tov = sum(float(g["TOV"]) for g in recent)
        threes_made = sum(float(g["FG3M"]) for g in recent)

        games_count = max(1, len(recent))
        return {
            "games_used": len(recent),
            "PTS": pts / games_count,
            "REB": reb / games_count,
            "AST": ast / games_count,
            "STL": stl / games_count,
            "BLK": blk / games_count,
            "TOV": tov / games_count,
            "3PM": threes_made / games_count,
            "FGM_pg": fgm / games_count,
            "FGA_pg": fga / games_count,
            "FTM_pg": ftm / games_count,
            "FTA_pg": fta / games_count,
            "FG%": (fgm / fga) if fga else 0.0,
            "FT%": (ftm / fta) if fta else 0.0,
        }
    except Exception:
        # Fallback to balldontlie if available and we have a name
        if full_name:
            return _bdl_last_n_averages(full_name, season, n)
        raise


def last_n_averages(full_name: str, season: str, n: int = 10) -> dict:
    pid = find_player_id(full_name)
    return _last_n_averages_by_id(pid, season, n, full_name=full_name)


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
    deduped.sort(
        key=lambda r: (not bool(r.get("is_active", False)), r.get("full_name", ""))
    )
    return deduped


def compute_team_stats(roster: List[Tuple[int, str]], season: str, n: int) -> Dict[str, float]:
    """Aggregate per-game team stats based on last-N window per player.
    Percentages are properly weighted by attempts.
    """
    totals = {
        "PTS": 0.0,
        "REB": 0.0,
        "AST": 0.0,
        "STL": 0.0,
        "BLK": 0.0,
        "TOV": 0.0,
        "3PM": 0.0,
        "FGM_pg": 0.0,
        "FGA_pg": 0.0,
        "FTM_pg": 0.0,
        "FTA_pg": 0.0,
    }
    for pid, name in roster:
        try:
            avgs = _last_n_averages_by_id(pid, season, n)
        except Exception as e:
            # Keep behavior simple: skip player on failure
            # Upstream callers (e.g., UI) can collect/report failures separately if needed.
            continue
        for k in [
            "PTS",
            "REB",
            "AST",
            "STL",
            "BLK",
            "TOV",
            "3PM",
            "FGM_pg",
            "FGA_pg",
            "FTM_pg",
            "FTA_pg",
        ]:
            totals[k] += float(avgs.get(k, 0.0))
    # Compute team percentages (attempt-weighted across players)
    fg_pct = (totals["FGM_pg"] / totals["FGA_pg"]) if totals["FGA_pg"] else 0.0
    ft_pct = (totals["FTM_pg"] / totals["FTA_pg"]) if totals["FTA_pg"] else 0.0
    out = {
        k: v
        for k, v in totals.items()
        if k not in ("FGM_pg", "FGA_pg", "FTM_pg", "FTA_pg")
    }
    out["FG%"] = fg_pct
    out["FT%"] = ft_pct
    return out
