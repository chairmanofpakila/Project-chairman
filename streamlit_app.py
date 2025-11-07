from __future__ import annotations

import os
import sys
from typing import Dict, List, Tuple

# Allow running without editable install by adding ./src to sys.path
_ROOT = os.path.dirname(__file__)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import streamlit as st
from chairman import core


st.set_page_config(page_title="Fantasy Trade Helper", layout="wide")
st.title("Fantasy Trade Helper")
st.caption(
    "Compare two teams using last-N games per-player averages (Regular Season)."
)


# Sidebar controls
with st.sidebar:
    season = st.text_input("Season (e.g. 2025-26)", value="2025-26")
    n = st.number_input("Last N games", min_value=1, max_value=30, value=10, step=1)


# Session state for rosters: list of (player_id, full_name)
if "team1" not in st.session_state:
    st.session_state.team1: List[Tuple[int, str]] = []
if "team2" not in st.session_state:
    st.session_state.team2: List[Tuple[int, str]] = []


@st.cache_data(ttl=3600)
def cached_search(q: str) -> List[dict]:
    return core.search_players(q)


@st.cache_data(ttl=900)
def cached_player_avg(pid: int, season: str, n: int) -> Dict[str, float]:
    return core._last_n_averages_by_id(pid, season, n)


def compute_team_stats_cached(
    roster: List[Tuple[int, str]], season: str, n: int
) -> Tuple[Dict[str, float], List[str]]:
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
    warnings: List[str] = []
    for pid, name in roster:
        try:
            avgs = cached_player_avg(pid, season, n)
        except Exception as e:
            warnings.append(f"Failed to fetch {name}: {e}")
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
    fg_pct = (totals["FGM_pg"] / totals["FGA_pg"]) if totals["FGA_pg"] else 0.0
    ft_pct = (totals["FTM_pg"] / totals["FTA_pg"]) if totals["FTA_pg"] else 0.0
    out = {k: v for k, v in totals.items() if k not in ("FGM_pg", "FGA_pg", "FTM_pg", "FTA_pg")}
    out["FG%"] = fg_pct
    out["FT%"] = ft_pct
    return out, warnings


def roster_editor(label: str, key: str):
    st.subheader(label)
    qs = st.text_input(f"Search to add ({label})", key=f"search_{key}")
    if qs:
        matches = cached_search(qs)
        if not matches:
            st.info("No matches. Try another search.")
        else:
            names = [m["full_name"] for m in matches]
            name_choice = st.selectbox("Pick a player", [""] + names, key=f"pick_{key}")
            if name_choice and st.button("Add", key=f"add_{key}"):
                m = next((m for m in matches if m["full_name"] == name_choice), None)
                if m:
                    pid = int(m["id"])
                    tup = (pid, m["full_name"])  # (id, full_name)
                    if tup not in st.session_state[key]:
                        st.session_state[key].append(tup)
    if st.session_state[key]:
        st.write(", ".join(name for _, name in st.session_state[key]))
        rm_choice = st.selectbox(
            "Remove player",
            [""] + [name for _, name in st.session_state[key]],
            key=f"rm_{key}",
        )
        if rm_choice and st.button("Remove", key=f"btn_rm_{key}"):
            st.session_state[key] = [pn for pn in st.session_state[key] if pn[1] != rm_choice]


col1, col2 = st.columns(2)
with col1:
    roster_editor("My Team", "team1")
with col2:
    roster_editor("Team of the opponent", "team2")


if st.button("Fetch & Compare"):
    if not st.session_state.team1 or not st.session_state.team2:
        st.warning("Please add at least one player to both teams.")
    else:
        with st.spinner("Fetching player logs and computing averages…"):
            stats1, warn1 = compute_team_stats_cached(st.session_state.team1, season, n)
            stats2, warn2 = compute_team_stats_cached(st.session_state.team2, season, n)

        if warn1 or warn2:
            with st.expander("Warnings (data fetch issues)"):
                for w in warn1 + warn2:
                    st.write("• ", w)

        st.subheader("Results")
        st.write("My Team:")
        st.json({k: round(v, 3) for k, v in stats1.items()})
        st.write("Team of the opponent:")
        st.json({k: round(v, 3) for k, v in stats2.items()})

        # Comparison table
        st.subheader("Category Comparison (per-game, last-N window)")
        cats = ["FG%", "FT%", "3PM", "PTS", "REB", "AST", "STL", "BLK", "TOV"]
        rows = []
        my_wins = 0
        opp_wins = 0
        for c in cats:
            v1 = float(stats1.get(c, 0.0))
            v2 = float(stats2.get(c, 0.0))
            if c == "TOV":
                lead = "My Team" if v1 < v2 else ("Team of the opponent" if v2 < v1 else "=")
            else:
                lead = "My Team" if v1 > v2 else ("Team of the opponent" if v2 > v1 else "=")
            if lead == "My Team":
                my_wins += 1
            elif lead == "Team of the opponent":
                opp_wins += 1
            rows.append({
                "Category": c,
                "My Team": v1,
                "Team of the opponent": v2,
                "Lead": lead,
            })
        st.table(rows)

        # Final conclusion
        if my_wins > opp_wins:
            verdict = "Therefore I win this trade."
        elif opp_wins > my_wins:
            verdict = "Therefore I lose this trade."
        else:
            verdict = "Therefore this trade is a tie."

        st.write(
            f"my team gets better in {my_wins} categories while the opponent gets better in {opp_wins} categories. {verdict}"
        )
