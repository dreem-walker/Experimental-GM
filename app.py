import streamlit as st
from google.cloud import firestore
from google.oauth2 import service_account
import json

# --- Page Setup ---
st.set_page_config(page_title="Pathfinder 1e PBP GM Screen", layout="wide")

# --- Firebase Initialization ---
@st.cache_resource
def get_db():
    # Load Firebase credentials from Streamlit Secrets
    key_dict = json.loads(st.secrets["firebase_json_string"]) if "firebase_json_string" in st.secrets else dict(st.secrets["firebase"])
    creds = service_account.Credentials.from_service_account_info(key_dict)
    return firestore.Client(credentials=creds)

try:
    db = get_db()
    st.sidebar.success("Firebase Connected")
except Exception as e:
    st.sidebar.error(f"Firebase Connection Error: {e}")
    db = None

# --- Sidebar: Party Status ---
st.sidebar.title("Campaign Controls")

if db:
    st.sidebar.subheader("Active Party")
    # Fetch character documents from Firestore
    chars_ref = db.collection("characters")
    docs = chars_ref.stream()
    
    for doc in docs:
        c_data = doc.to_dict()
        name = c_data.get("character_name", doc.id)
        status = c_data.get("status", "Waiting")
        active = c_data.get("active", True)
        
        if active:
            st.sidebar.write(f"**{name}** — `{status}`")

# --- Main Dashboard Tabs ---
tab1, tab2, tab3 = st.tabs(["Combat & Encounter", "Module & Lore", "Player Actions"])

with tab1:
    st.header("Active Round Tracker")
    st.info("Pulling initiative order and current encounter state...")
    # Add your combat turn tracker / Google Sheets token sync here

with tab2:
    st.header("Module Data & Stat Blocks")
    st.caption("Shared Google Drive Module Folder")
    # Call your Google Drive reader function here to display active room notes or PDF summaries

with tab3:
    st.header("Pending Player Inputs")
    if db:
        # Query Firestore for player posts or action queue
        st.write("Awaiting player posts for the current round...")
