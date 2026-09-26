import streamlit as st
import json
from google.cloud import firestore
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --- 1. PAGE SETUP (MUST BE FIRST) ---
st.set_page_config(
    page_title="Pathfinder 1e PBP GM Screen",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- 2. AUTHENTICATION & CLIENT INITIALIZATION ---
@st.cache_resource
def get_firebase_db():
    """Initializes Firestore client using Streamlit Secrets."""
    if "firebase" in st.secrets:
        key_dict = dict(st.secrets["firebase"])
    elif "FIREBASE" in st.secrets:
        key_dict = dict(st.secrets["FIREBASE"])
    elif "firebase_credentials" in st.secrets:
        key_dict = dict(st.secrets["firebase_credentials"])
    else:
        raise KeyError("Firebase credentials missing from Streamlit secrets.")

    creds = service_account.Credentials.from_service_account_info(key_dict)
    return firestore.Client(credentials=creds)

@st.cache_resource
def get_drive_service():
    """Initializes Google Drive API service."""
    if "firebase" in st.secrets:
        key_dict = dict(st.secrets["firebase"])
    elif "FIREBASE" in st.secrets:
        key_dict = dict(st.secrets["FIREBASE"])
    else:
        return None
    
    scopes = ['https://www.googleapis.com/auth/drive.readonly']
    creds = service_account.Credentials.from_service_account_info(key_dict, scopes=scopes)
    return build('drive', 'v3', credentials=creds)

# Connect to Services
try:
    db = get_firebase_db()
    st.sidebar.success("Firebase Connected")
except Exception as e:
    db = None
    st.sidebar.error(f"Firebase Error: {e}")

try:
    drive_service = get_drive_service()
    if drive_service:
        st.sidebar.success("Google Drive API Ready")
except Exception as e:
    drive_service = None
    st.sidebar.warning(f"Drive API Note: {e}")

# --- 3. HELPER FUNCTIONS ---
def fetch_characters():
    """Fetches all documents from the 'characters' collection."""
    if not db:
        return []
    docs = db.collection("characters").stream()
    char_list = []
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        char_list.append(data)
    return char_list

def fetch_drive_files(folder_id):
    """Lists files inside the specified Google Drive folder."""
    if not drive_service or not folder_id:
        return []
    query = f"'{folder_id}' in parents and trashed = false"
    results = drive_service.files().list(q=query, fields="files(id, name, mimeType)").execute()
    return results.get('files', [])

# --- 4. SIDEBAR LOGIC ---
st.sidebar.title("Campaign Control")

if db:
    characters = fetch_characters()
    st.sidebar.subheader(f"Active Party ({len(characters)})")
    
    for c in characters:
        name = c.get("character_name", c.get("name", c["id"]))
        hp = c.get("current_hp", c.get("hp", "N/A"))
        max_hp = c.get("max_hp", "N/A")
        status = c.get("status", "Active")
        
        st.sidebar.markdown(f"**{name}**")
        st.sidebar.caption(f"HP: {hp}/{max_hp} | Status: {status}")
        st.sidebar.markdown("---")

# --- 5. MAIN DASHBOARD UI ---
st.title("Pathfinder 1e PBP GM Console")

tab_combat, tab_module, tab_players = st.tabs([
    "⚔️ Combat & Turn Runner", 
    "📜 Module & Drive Notes", 
    "👤 Character Roster"
])

# --- TAB 1: COMBAT & TURN RUNNER ---
with tab_combat:
    st.header("Encounter Status")
    
    col1, col2 = st.columns([2, 1])
    
    with col1:
        st.subheader("Current Round Tracker")
        
        # Test state fetching or fallback
        current_round = st.number_input("Round Number", min_value=1, value=1)
        active_turn = st.selectbox("Active Character Turn", [c.get("character_name", c["id"]) for c in characters] if characters else ["No Characters Loaded"])
        
        st.info(f"Currently processing turn for: **{active_turn}** in Round {current_round}")
        
        st.text_area("GM Action Log / Prompt Preview", value="Select targets and roll actions...", height=120)
        
        if st.button("Advance Turn"):
            st.success(f"Turn updated for {active_turn}!")

    with col2:
        st.subheader("Quick Actions")
        st.button("🎲 Roll Party Perception")
        st.button("🛡️ Check Party Defenses")
        st.button("💾 Sync State to Firestore")

# --- TAB 2: MODULE & DRIVE DATA ---
with tab_module:
    st.header("Google Drive Campaign Files")
    
    folder_id = st.secrets.get("google_drive", {}).get("folder_id", "") if "google_drive" in st.secrets else ""
    
    if folder_id and drive_service:
        files = fetch_drive_files(folder_id)
        if files:
            st.write(f"Found {len(files)} module asset(s) in Drive folder:")
            for f in files:
                st.markdown(f"- 📄 **{f['name']}** `(ID: {f['id']})`")
        else:
            st.info("No files found or folder is empty.")
    else:
        st.warning("Google Drive Folder ID not configured in Streamlit Secrets.")

# --- TAB 3: CHARACTER ROSTER INSPECTOR ---
with tab_players:
    st.header("Player Character Sheet Inspector")
    
    if characters:
        selected_char_name = st.selectbox("Select Character to Inspect", [c.get("character_name", c["id"]) for c in characters])
        selected_char = next((c for c in characters if c.get("character_name", c["id"]) == selected_char_name), None)
        
        if selected_char:
            st.json(selected_char)
    else:
        st.info("No character documents found in the Firestore `characters` collection.")
