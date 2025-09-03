# festival_guess_app.py
# Streamlit app to run a "Guess the Volume" competition at a booth.
# Multi-user (admin + participants), with optional **Google Sheets** backend for public cloud use.
# If Google Sheets secrets are present, all data persists even when the app sleeps/restarts.
# Otherwise, it falls back to local CSV (good for LAN kiosk).
#
# Usage (Streamlit Community Cloud):
# - Add requirements.txt with deps (see README/snippet in chat)
# - Configure secrets with GCP service account + SHEET_ID (2 tabs: 'guesses' and 'config' will be created)
# - Optional: set PUBLIC_BASE_URL to your streamlit.app URL for a stable QR

import os
import json
from datetime import datetime
import hashlib
from typing import Tuple, Optional, List
import io
import base64

import pandas as pd
import numpy as np
import streamlit as st

# Optional deps
try:
    from filelock import FileLock
except Exception:  # pragma: no cover
    FileLock = None  # type: ignore
try:
    import socket
except Exception:  # pragma: no cover
    socket = None  # type: ignore
try:
    import qrcode
except Exception:  # pragma: no cover
    qrcode = None  # type: ignore

# Google Sheets deps (optional)
try:
    import gspread
    from google.oauth2 import service_account
except Exception:  # pragma: no cover
    gspread = None
    service_account = None  # type: ignore

APP_TITLE = " 🏆 Guess the Volume — Win Goodies! 🏆 "
CSV_PATH = "leaderboard.csv"
CONFIG_PATH = "config.json"
CONFIG_LOCK = "config.json.lock"
CSV_LOCK = "leaderboard.csv.lock"
ADMIN_PIN = os.environ.get("VOLUME_GUESS_ADMIN_PIN", st.secrets.get("VOLUME_GUESS_ADMIN_PIN", "2468"))
DEFAULT_PORT = os.environ.get("PUBLIC_PORT", "8501")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", st.secrets.get("PUBLIC_BASE_URL"))

DEFAULT_CONFIG = {
    "truth_liters": None,
    "tol_mode": "percent",
    "tolerance_value": 5.0,
}
# -------------------- Helper utils --------------------

def _standardize_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "Anonymous"
    parts = name.split()
    if len(parts) == 1:
        return parts[0][:18]
    return f"{parts[0][:14]} {parts[1][0].upper()}."


def _to_float(v: any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        return None


def _lock(path: str):
    if FileLock is None:
        class Dummy:
            def __enter__(self_inner):
                return None
            def __exit__(self_inner, *args):
                return False
        return Dummy()
    return FileLock(path)

# -------------------- Storage backends --------------------

class Storage:
    def load_guesses(self) -> pd.DataFrame: ...
    def append_guess(self, row: dict) -> None: ...
    def load_config(self) -> dict: ...
    def save_config(self, cfg: dict) -> None: ...

# ---- Google Sheets Backend ----
class SheetsStorage(Storage):
    def __init__(self, sheet_id: str):
        self.sheet_id = sheet_id
        self._gc = None
        self._sh = None
        self._guesses_ws = None
        self._config_ws = None
        self._ensure()

    def _client(self):
        if self._gc is None:
            sa = st.secrets.get("gcp_service_account")
            if not sa:
                raise RuntimeError("Missing gcp_service_account in secrets")
            creds = service_account.Credentials.from_service_account_info(sa, scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive.readonly",
            ])
            self._gc = gspread.authorize(creds)
        return self._gc

    def _ensure(self):
        gc = self._client()
        self._sh = gc.open_by_key(self.sheet_id)
        # guesses sheet
        try:
            self._guesses_ws = self._sh.worksheet("guesses")
        except gspread.WorksheetNotFound:
            self._guesses_ws = self._sh.add_worksheet("guesses", rows=1, cols=7)
            headers = [
                "timestamp", "display_name", "guess_liters", "abs_error_liters", "pct_error", "is_winner", "raw_name"
            ]
            self._guesses_ws.update("A1:G1", [headers])
        # config sheet
        try:
            self._config_ws = self._sh.worksheet("config")
        except gspread.WorksheetNotFound:
            self._config_ws = self._sh.add_worksheet("config", rows=3, cols=2)
            self._config_ws.update("A1:B3", [["key", "value"], ["tol_mode", "percent"], ["tolerance_value", "5"]])

    def load_guesses(self) -> pd.DataFrame:
        vals = self._guesses_ws.get_all_values()
        if not vals:
            return pd.DataFrame()
        df = pd.DataFrame(vals[1:], columns=vals[0])
        # coerce
        for col in ("guess_liters", "abs_error_liters", "pct_error"):
            if col in df.columns:
                df[col] = df[col].apply(_to_float)
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "is_winner" in df.columns:
            df["is_winner"] = df["is_winner"].astype(str).str.lower().isin(["true", "1", "yes"])  # type: ignore
        return df

    def append_guess(self, row: dict) -> None:
        order = ["timestamp", "display_name", "guess_liters", "abs_error_liters", "pct_error", "is_winner", "raw_name"]
        self._guesses_ws.append_row([row.get(k, "") for k in order], value_input_option="USER_ENTERED")

    def load_config(self) -> dict:
        cfg = DEFAULT_CONFIG.copy()
        vals = self._config_ws.get_all_records()
        for r in vals:
            k = str(r.get("key", ""))
            v = r.get("value")
            if k == "tol_mode" and v in ("percent", "absolute"):
                cfg["tol_mode"] = str(v)
            elif k == "tolerance_value" and v is not None:
                cfg["tolerance_value"] = _to_float(v) or cfg["tolerance_value"]
            elif k == "truth_liters" and v not in (None, ""):
                cfg["truth_liters"] = _to_float(v) or cfg["truth_liters"]
        return cfg

    def save_config(self, cfg: dict) -> None:
        rows = [
            ["key", "value"],
            ["tol_mode", cfg.get("tol_mode", "percent")],
            ["tolerance_value", cfg.get("tolerance_value", 5.0)],
            ["truth_liters", cfg.get("truth_liters") or ""]
        ]
        self._config_ws.clear()
        self._config_ws.update("A1:B4", rows)

# ---- Local CSV Backend ----
class CsvStorage(Storage):
    def __init__(self):
        self._ensure_csv()

    def _ensure_csv(self):
        if not os.path.exists(CSV_PATH):
            with _lock(CSV_LOCK):
                if not os.path.exists(CSV_PATH):
                    df = pd.DataFrame(columns=[
                        "timestamp", "display_name", "guess_liters", "abs_error_liters", "pct_error", "is_winner", "raw_name"
                    ])
                    df.to_csv(CSV_PATH, index=False)

    def load_guesses(self) -> pd.DataFrame:
        self._ensure_csv()
        with _lock(CSV_LOCK):
            try:
                df = pd.read_csv(CSV_PATH)
            except Exception:
                df = pd.DataFrame(columns=[
                    "timestamp", "display_name", "guess_liters", "abs_error_liters", "pct_error", "is_winner", "raw_name"
                ])
        if not df.empty:
            for col in ("guess_liters", "abs_error_liters", "pct_error"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            if "is_winner" in df.columns:
                df["is_winner"] = df["is_winner"].astype(bool)
        return df

    def append_guess(self, row: dict) -> None:
        self._ensure_csv()
        with _lock(CSV_LOCK):
            df = pd.read_csv(CSV_PATH) if os.path.exists(CSV_PATH) else pd.DataFrame()
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            df.to_csv(CSV_PATH, index=False)

    def load_config(self) -> dict:
        if not os.path.exists(CONFIG_PATH):
            return DEFAULT_CONFIG.copy()
        with _lock(CONFIG_LOCK):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        cfg = DEFAULT_CONFIG.copy()
        cfg.update({k: data.get(k, v) for k, v in DEFAULT_CONFIG.items()})
        return cfg

    def save_config(self, cfg: dict) -> None:
        with _lock(CONFIG_LOCK):
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)

# Choose backend
def _get_storage() -> Storage:
    sheet_id = st.secrets.get("SHEET_ID") if hasattr(st, "secrets") else None
    if sheet_id and gspread and service_account:
        try:
            return SheetsStorage(str(sheet_id))
        except Exception as e:
            st.warning(f"Google Sheets backend unavailable: {e}. Falling back to local CSV.")
            return CsvStorage()
    return CsvStorage()

STORAGE = _get_storage()

# -------------------- Scoring --------------------

def _compute_outcome(truth_liters: float, guess_liters: float, tol_mode: str, tol_val: float) -> Tuple[float, float, bool]:
    abs_err = abs(truth_liters - guess_liters)
    pct_err = (abs_err / truth_liters * 100.0) if truth_liters and truth_liters > 0 else float("inf")
    if tol_mode == "percent":
        is_win = pct_err <= tol_val
    else:
        is_win = abs_err <= tol_val
    return abs_err, pct_err, is_win

# -------------------- Networking helpers --------------------

def _get_local_ip() -> Optional[str]:
    try:
        if socket is None:
            return None
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip.startswith("127."):
            ip = socket.gethostbyname(socket.gethostname())
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return None


def _share_url() -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL.rstrip("/")
    ip = _get_local_ip() or "localhost"
    return f"http://{ip}:{DEFAULT_PORT}"

# -------------------- UI --------------------

st.set_page_config(page_title=APP_TITLE, layout="wide")
st.title(APP_TITLE)

# Sidebar: Admin controls
with st.sidebar:
    try:
        with open("BW_logo.png", "rb") as f:
            data = f.read()
        b64_string = base64.b64encode(data).decode()
        st.markdown(f"""
        <div style="display: flex; justify-content: center;">
            <img src="data:image/png;base64,{b64_string}" width="100">
        </div>
        """, unsafe_allow_html=True)
    except FileNotFoundError:
        st.warning("Logo image not found.")
    
    url = _share_url()
    st.caption("Share this link/QR:")
    st.code(url)
    if qrcode is not None:
        try:
            img = qrcode.make(url)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            st.image(buf, caption="Scan to join", use_container_width=True)
        except Exception as e:
            st.error(f"Could not generate QR code: {e}")

    st.header("Admin")
    pin = st.text_input("PIN", type="password", placeholder="Enter PIN")
    admin = (pin == ADMIN_PIN)

    cfg = STORAGE.load_config()

    if admin:
        st.success("Admin mode enabled")
        st.subheader("Ground Truth Volume")
        truth_liters_new = st.number_input("True volume (liters)",
                                         min_value=0.0,
                                         value=float(cfg.get("truth_liters") or 0.0),
                                         step=0.1, format="%.3f")

        st.subheader("Winner Tolerance")
        tol_mode = st.radio("Tolerance mode", ["percent", "absolute"], index=0 if cfg.get("tol_mode") == "percent" else 1, horizontal=True)
        if tol_mode == "percent":
            tol_val = st.slider("±% from truth", min_value=1, max_value=50, value=int(cfg.get("tolerance_value", 5)))
        else:
            tol_val = st.number_input("± tolerance (liters)", min_value=0.0, value=float(cfg.get("tolerance_value", 2.0)), step=0.5)

        if st.button("Save & broadcast settings", use_container_width=True):
            new_cfg = {
                "truth_liters": float(truth_liters_new),
                "tol_mode": tol_mode,
                "tolerance_value": float(tol_val),
            }
            STORAGE.save_config(new_cfg)
            st.success("Settings saved for all participants.")

        st.divider()
        if st.button("Reset leaderboard"):
            # For Sheets, clear to header; for CSV, recreate file
            if isinstance(STORAGE, SheetsStorage):
                sh_storage: SheetsStorage = STORAGE
                sh_storage._guesses_ws.resize(rows=1)
                sh_storage._guesses_ws.update("A1:G1", [[
                    "timestamp", "display_name", "guess_liters", "abs_error_liters", "pct_error", "is_winner", "raw_name"
                ]])
            else:
                with _lock(CSV_LOCK):
                    pd.DataFrame(columns=["timestamp", "display_name", "guess_liters", "abs_error_liters", "pct_error", "is_winner", "raw_name"]).to_csv(CSV_PATH, index=False)
            st.warning("Leaderboard reset.")

        df_all = STORAGE.load_guesses()
        if not df_all.empty:
            st.download_button(
                label="Download CSV",
                data=df_all.to_csv(index=False).encode("utf-8"),
                file_name="volume_guess_leaderboard.csv",
                mime="text/csv",
                use_container_width=True,
            )
    else:
        st.info("Admin-only area. A host will enable the game.")
        if cfg.get("truth_liters"):
            st.caption("Game is configured. Good luck!")
        else:
            st.caption("Waiting for the host to configure the game…")

# Main content tabs
guess_tab, board_tab, usecases_tab = st.tabs([
    "Enter your guess",
    "Leaderboard",
    "Use cases",
])

with guess_tab:
    st.subheader("Your shot at glory ✨")
    cfg = STORAGE.load_config()
    truth_liters = cfg.get("truth_liters")
    if not truth_liters or truth_liters <= 0:
        st.warning("Host is setting up the game. Please check back in a moment.")
    name = st.text_input("Your name (optional)", placeholder="First name or nickname")

    guess_value = st.number_input("Your guess (liters)", min_value=0.0, value=0.0, step=0.1)

    if st.button("Submit guess", use_container_width=True):
        if not truth_liters or truth_liters <= 0:
            st.error("Sorry — scoring isn't ready yet.")
        else:
            guess_liters = guess_value
            abs_err, pct_err, is_win = _compute_outcome(truth_liters, guess_liters, cfg.get("tol_mode", "percent"), float(cfg.get("tolerance_value", 5.0)))

            row = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "display_name": _standardize_name(name),
                "guess_liters": guess_liters,
                "abs_error_liters": abs_err,
                "pct_error": pct_err,
                "is_winner": bool(is_win),
                "raw_name": name,
            }
            STORAGE.append_guess(row)

            st.success(f"You guessed {guess_value:.3f} liters.")
            st.info(f"Actual: {truth_liters:.3f} liters • Error: {abs_err:.3f} liters ({pct_err:.1f}%).")

            if is_win:
                st.balloons()
                st.success("🎉 You’re within the winning tolerance! Claim **2 goodies** at the desk.")
            else:
                st.write("Thanks for playing — show this screen to claim **1 goodie**!")

from streamlit.components.v1 import html as st_html  # put near your imports if you prefer

with board_tab:
    st.subheader("Live Leaderboard 🏁")
    df = STORAGE.load_guesses()
    if df.empty:
        st.caption("No entries yet. Be the first!")
    else:
        df = df.copy()
        if {"abs_error_liters", "pct_error", "timestamp"}.issubset(df.columns):
            # sort and take top 10
            df.sort_values(by=["pct_error", "abs_error_liters", "timestamp"], inplace=True)
            top = df.copy()
            top.insert(0, "Position", range(1, len(top) + 1))

            # format columns
            top["Name"] = top["display_name"].astype(str).str.strip().replace({"": "Anonymous"})
            top["% error"] = pd.to_numeric(top["pct_error"], errors="coerce").round(2).map(lambda x: f"{x:.2f}")
            t = pd.to_datetime(top["timestamp"], errors="coerce")
            top["Time"] = np.where(t.notna(), t.dt.strftime("%H:%M:%S"), top["timestamp"].astype(str))

            # build HTML rows
            import html as ihtml  # for safe escaping of names/times
            def medal(p):
                return "🥇" if p == 1 else "🥈" if p == 2 else "🥉" if p == 3 else str(p)

            rows = []
            for _, r in top.iterrows():
                rows.append(
                    f"<tr>"
                    f"<td class='pos'>{medal(int(r['Position']))}</td>"
                    f"<td>{ihtml.escape(str(r['Name']))}</td>"
                    f"<td>{r['% error']}</td>"
                    f"<td>{ihtml.escape(str(r['Time']))}</td>"
                    f"</tr>"
                )

            # table + CSS (all cells centered)
            html_str = f"""
<style>
.lb {{
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  font-size: 1.05rem;
  font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: white;
}}
.lb th, .lb td {{
  text-align: center;
  padding: 10px 12px;
  border-bottom: 1px solid rgba(255,255,255,0.1);
}}
.lb thead th {{
  position: sticky; top: 0;
  background: rgba(0,0,0,0.3);
  backdrop-filter: blur(4px);
  border-bottom: 1px solid rgba(255,255,255,0.2);
}}
.lb tr:nth-child(even) td {{ background: rgba(255,255,255,0.05); }}
.pos {{
  font-weight: 700;
  font-size: 1.1rem;
}}
</style>
<table class="lb">
  <thead>
    <tr><th>Position</th><th>Name</th><th>% error</th><th>Time</th></tr>
  </thead>
  <tbody>
    {''.join(rows)}
  </tbody>
</table>
"""
            st_html(html_str, height=600, scrolling=True)

            # Best so far (from fully sorted df)
            best = df.iloc[0]
            st.caption(f"Best so far: {best['display_name']} ({best['pct_error']:.2f}% error)")
        else:
            st.warning("Leaderboard data is in an old format and cannot be displayed.")


with usecases_tab:
    st.subheader("Where this helps on site")
    uc1, uc2, uc3, uc4 = st.columns(4)
    with uc1:
        st.markdown(
            """**Stockpile volumes**

Measure earth, gravel or debris piles fast to estimate haulage or billing."""
        )
        if os.path.exists("images/use_cases/stockpile.jpeg"):
            st.image("images/use_cases/stockpile.jpeg")
    with uc2:
        st.markdown(
            """**Walls & roofs**

Compute areas and volumes for materials, insulation, and waste planning."""
        )
        if os.path.exists("images/use_cases/roof_with_area.jpeg"):
            st.image("images/use_cases/roof_with_area.jpeg")
    with uc3:
        st.markdown(
            """**Room & MEP**

Quick room volumes and clearances for HVAC or prefab checks."""
        )
        if os.path.exists("images/use_cases/prefab.png"):
            st.image("images/use_cases/prefab.png")
    with uc4:
        st.markdown(
            """**Progress tracking**

Compare volumes over time — pour volumes, excavation progress, or fill/void changes."""
        )
        if os.path.exists("images/use_cases/oslo.png"):
            st.image("images/use_cases/oslo.png")

st.divider()
st.caption(
    f"🎁 **Prize Rules:** Everyone gets **one goodie** for playing. Guess correctly within the tolerance of {cfg.get('tolerance_value')} {cfg.get('tol_mode')} from the true volume to win **two goodies**!"
)
