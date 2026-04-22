import os
from datetime import datetime, timezone

import requests
import streamlit as st
from alpaca_trade_api import REST

# -------------------------
# Page & Theme Configuration
# -------------------------
st.set_page_config(
    page_title="GemTrader Command Center",
    page_icon="📈",
    layout="wide",
)

st.markdown(
    """
    <style>
        .stApp {
            background-color: #0b1220;
            color: #e5e7eb;
        }
        .metric-card {
            background: linear-gradient(145deg, #111827, #0f172a);
            border: 1px solid #1f2937;
            border-radius: 18px;
            padding: 1.1rem 1.3rem;
            box-shadow: 0 8px 30px rgba(0, 0, 0, 0.35);
            margin-bottom: 1rem;
        }
        .metric-label {
            color: #9ca3af;
            font-size: 0.9rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.4rem;
        }
        .metric-value {
            color: #f9fafb;
            font-size: 2rem;
            font-weight: 700;
            line-height: 1.2;
        }
        .heartbeat-card {
            background: #111827;
            border: 1px solid #1f2937;
            border-radius: 14px;
            padding: 1rem 1.2rem;
        }
        div[data-testid="stButton"] button[kind="primary"] {
            background-color: #b91c1c !important;
            color: white !important;
            border: 2px solid #ef4444 !important;
            border-radius: 14px !important;
            min-height: 80px;
            font-size: 1.2rem;
            font-weight: 800;
            width: 100%;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# -------------------------
# Runtime Configuration
# -------------------------
STATUS_API_URL = os.getenv("STATUS_API_URL", "http://localhost:8000/status")
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")


@st.cache_data(ttl=30)
def fetch_status():
    """Fetch bot status from FastAPI service."""
    response = requests.get(STATUS_API_URL, timeout=10)
    response.raise_for_status()
    return response.json()


def make_alpaca_client():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError(
            "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY. Configure them as environment variables."
        )
    return REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, base_url=ALPACA_BASE_URL)


def metric_card(label: str, value: str):
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-label">{label}</div>
            <div class="metric-value">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# -------------------------
# Header
# -------------------------
st.title("🏛️ GemTrader Command Center")
st.caption("Institutional live-monitoring and kill-switch dashboard")

# Auto-refresh the page every 30 seconds so heartbeat/metrics stay current.
st.autorefresh(interval=30_000, key="status_autorefresh")

# -------------------------
# Metrics Section
# -------------------------
col1, col2, col3 = st.columns(3)

try:
    status_payload = fetch_status()
except Exception as exc:
    status_payload = {}
    st.error(f"Unable to fetch /status endpoint ({STATUS_API_URL}): {exc}")

account_value = status_payload.get("total_account_value", status_payload.get("account_value", "--"))
today_pnl = status_payload.get("todays_pnl", status_payload.get("today_pnl", "--"))
active_positions = status_payload.get("active_positions", status_payload.get("positions", "--"))

with col1:
    metric_card("Total Account Value", f"${account_value}" if account_value != "--" else "--")
with col2:
    metric_card("Today's PnL", f"${today_pnl}" if today_pnl != "--" else "--")
with col3:
    metric_card("Active Positions", str(active_positions))

# -------------------------
# Heartbeat Section
# -------------------------
st.subheader("🫀 Live Heartbeat")
heartbeat_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

st.markdown('<div class="heartbeat-card">', unsafe_allow_html=True)
left, right = st.columns([2, 1])
with left:
    bot_status = status_payload.get("status", "UNKNOWN")
    st.write(f"**Bot Status:** `{bot_status}`")
    st.write(f"**/status Endpoint:** `{STATUS_API_URL}`")
    st.write(f"**Last Refresh:** `{heartbeat_time}`")
with right:
    st.write("**Raw /status JSON**")
    st.json(status_payload or {"message": "No status data yet"})
st.markdown("</div>", unsafe_allow_html=True)

# -------------------------
# Emergency Liquidation
# -------------------------
st.divider()
st.subheader("🚨 Emergency Controls")
st.warning(
    "This action is irreversible in the current session and will attempt to close ALL open positions immediately."
)

if st.button("EMERGENCY LIQUIDATE", type="primary", use_container_width=True):
    try:
        client = make_alpaca_client()
        liquidation_result = client.close_all_positions(cancel_orders=True)
        st.success("Emergency liquidation request submitted to Alpaca.")
        st.json({"close_all_positions_result": str(liquidation_result)})
    except Exception as exc:
        st.error(f"Emergency liquidation failed: {exc}")

st.caption(
    "Tip: In Railway, set STATUS_API_URL to your FastAPI service URL ending in /status, "
    "and configure ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_BASE_URL."
)
