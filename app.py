import streamlit as st
import json
import requests
import io
import re
from bs4 import BeautifulSoup
from google.cloud import firestore
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError

# Import Gemini SDK (google-genai preferred, fallback to google-generativeai)
try:
    from google import genai
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False
    try:
        import google.generativeai as legacy_genai
        HAS_LEGACY_GENAI = True
    except ImportError:
        HAS_LEGACY_GENAI = False

# Import binary document parsers safely
try:
    import pypdf
except ImportError:
    pypdf = None

try:
    import docx
except ImportError:
    docx = None

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
    """Initializes Google Drive API service using service account credentials."""
    if "firebase" in st.secrets:
        key_dict = dict(st.secrets["firebase"])
    elif "FIREBASE" in st.secrets:
        key_dict = dict(st.secrets["FIREBASE"])
    elif "firebase_credentials" in st.secrets:
        key_dict = dict(st.secrets["firebase_credentials"])
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

# Read Secrets
gemini_api_key = st.secrets.get("GEMINI_API_KEY", "")
discord_webhook_url = st.secrets.get("DISCORD_WEBHOOK_URL", "")

folder_id = (
    st.secrets.get("GOOGLE_DRIVE_FOLDER_ID") or 
    st.secrets.get("google_drive", {}).get("folder_id", "")
)

# Sidebar integration status indicators
if gemini_api_key:
    st.sidebar.caption("🤖 Gemini API Key Configured")
if discord_webhook_url:
    st.sidebar.caption("💬 Discord Webhook Configured")

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

def fetch_drive_files(target_folder_id):
    """Lists files inside the specified Google Drive folder with explicit error handling."""
    if not drive_service or not target_folder_id:
        return []
    try:
        query = f"'{target_folder_id}' in parents and trashed = false"
        results = drive_service.files().list(
            q=query,
            fields="files(id, name, mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()
        return results.get('files', [])
    except HttpError as err:
        st.error(f"Google Drive API Error: {err.resp.status} - {err._get_reason()}")
        return []
    except Exception as e:
        st.error(f"Drive Fetch Error: {e}")
        return []

def clean_extracted_text(text):
    """Strips HTML tags, messy code fragments, and unescapes formatting."""
    if "<html" in text.lower() or "<body" in text.lower() or "</" in text:
        soup = BeautifulSoup(text, "html.parser")
        for script in soup(["script", "style"]):
            script.decompose()
        text = soup.get_text(separator="\n")

    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for phrase in lines if phrase.strip())
    return "\n\n".join(chunks)

def parse_pdf_stream(stream):
    """Extracts text content from a PDF byte stream."""
    if not pypdf:
        return "PDF parsing requires 'pypdf' library in requirements.txt."
    try:
        reader = pypdf.PdfReader(stream)
        text = []
        for page in reader.pages:
            extracted = page.extract_text()
            if extracted:
                text.append(extracted)
        return "\n\n".join(text) if text else "No text could be extracted from this PDF."
    except Exception as e:
        return f"PDF Parsing Error: {e}"

def parse_docx_stream(stream):
    """Extracts text content from a Word document byte stream."""
    if not docx:
        return "DOCX parsing requires 'python-docx' library in requirements.txt."
    try:
        doc = docx.Document(stream)
        text = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(text) if text else "No text found in Word document."
    except Exception as e:
        return f"DOCX Parsing Error: {e}"

def read_drive_file_content(file_id, mime_type):
    """Downloads and extracts clean text content from Google Drive files."""
    if not drive_service:
        return "Drive service unavailable."
    try:
        if 'application/vnd.google-apps.document' in mime_type:
            request = drive_service.files().export_media(fileId=file_id, mimeType='text/plain')
            file_stream = io.BytesIO()
            downloader = MediaIoBaseDownload(file_stream, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            file_stream.seek(0)
            raw_text = file_stream.read().decode('utf-8', errors='ignore')
            return clean_extracted_text(raw_text)

        request = drive_service.files().get_media(fileId=file_id)
        file_stream = io.BytesIO()
        downloader = MediaIoBaseDownload(file_stream, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        file_stream.seek(0)

        if 'pdf' in mime_type.lower():
            return parse_pdf_stream(file_stream)
        elif 'wordprocessingml' in mime_type.lower() or 'docx' in mime_type.lower():
            return parse_docx_stream(file_stream)
        else:
            raw_text = file_stream.read().decode('utf-8', errors='ignore')
            return clean_extracted_text(raw_text)

    except Exception as e:
        return f"Error reading file content: {e}"

def generate_gemini_response(prompt, system_instruction=None, temperature=0.7):
    """Generates text response from Gemini API with flexible temperature controls."""
    if not gemini_api_key:
        return "⚠️ Gemini API key missing from Streamlit secrets (`GEMINI_API_KEY`)."
    
    full_prompt = prompt
    if system_instruction:
        full_prompt = f"System Instruction: {system_instruction}\n\nUser Query: {prompt}"

    try:
        if HAS_GENAI:
            client = genai.Client(api_key=gemini_api_key)
            config = {"temperature": temperature}
            response = client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=full_prompt,
                config=config
            )
            return response.text
        elif HAS_LEGACY_GENAI:
            legacy_genai.configure(api_key=gemini_api_key)
            model = legacy_genai.GenerativeModel('gemini-3.5-flash-lite')
            generation_config = legacy_genai.types.GenerationConfig(temperature=temperature)
            response = model.generate_content(full_prompt, generation_config=generation_config)
            return response.text
        else:
            return "⚠️ Neither `google-genai` nor `google-generativeai` package is installed."
    except Exception as e:
        return f"Gemini API Error: {e}"
        
def send_discord_message(content):
    """Sends a message directly to the configured Discord Webhook."""
    if not discord_webhook_url:
        st.error("Discord Webhook URL not found in secrets.")
        return False
    
    payload = {"content": content}
    response = requests.post(discord_webhook_url, json=payload)
    return response.status_code in (200, 204)

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
else:
    characters = []

# --- 5. MAIN DASHBOARD UI ---
st.title("Pathfinder 1e PBP GM Console")

tab_combat, tab_module, tab_ai, tab_players = st.tabs([
    "⚔️ Combat & Turn Runner", 
    "📜 Module & Drive Notes", 
    "🤖 Gemini Assistant",
    "👤 Character Roster"
])

# --- TAB 1: COMBAT & TURN RUNNER ---
with tab_combat:
    st.header("Encounter Status")
    
    col1, col2 = st.columns([2, 1])
    
    with col1:
        st.subheader("Current Round Tracker")
        
        current_round = st.number_input("Round Number", min_value=1, value=1)
        active_turn = st.selectbox(
            "Active Character Turn", 
            [c.get("character_name", c["id"]) for c in characters] if characters else ["No Characters Loaded"]
        )
        
        st.info(f"Currently processing turn for: **{active_turn}** in Round {current_round}")
        
        gm_post = st.text_area("GM Action Log / Prompt Preview", value="Select targets and roll actions...", height=120)
        
        col_btn1, col_btn2 = st.columns(2)
        with col_btn1:
            if st.button("Advance Turn"):
                st.success(f"Turn updated for {active_turn}!")
        with col_btn2:
            if st.button("Post Update to Discord"):
                if send_discord_message(f"**[Round {current_round}] {active_turn}'s Turn**\n{gm_post}"):
                    st.success("Sent to Discord successfully!")
                else:
                    st.error("Failed to send message to Discord.")

    with col2:
        st.subheader("Quick Actions")
        st.button("🎲 Roll Party Perception")
        st.button("🛡️ Check Party Defenses")
        st.button("💾 Sync State to Firestore")

    st.divider()

def render_dual_chat_system():
    st.subheader("Campaign Communications")
    
    col_ic, col_ooc = st.columns(2)

    # --- LEFT COLUMN: IN-CHARACTER STORY LOG ---
    with col_ic:
        st.markdown("### 📜 Story Log (In-Character)")
        
        story_container = st.container(height=380)
        with story_container:
            if "ic_messages" not in st.session_state:
                st.session_state.ic_messages = []
                
            for msg in st.session_state.ic_messages:
                with st.chat_message(msg["role"]):
                    st.markdown(f"**{msg['sender']}**: {msg['content']}")

        # Dedicated IC Input Form
        with st.form("ic_input_form", clear_on_submit=True):
            ic_speaker = st.selectbox("Speaking As", ["GM", "Valeros", "Ezren", "Harsk", "Kyra"])
            ic_text = st.text_area("IC Action / Speech", placeholder="I swing my longsword at the hobgoblin sergeant...", height=80)
            submit_ic = st.form_submit_button("Post Action to Story")

            if submit_ic and ic_text:
                # 1. Log the user's action
                st.session_state.ic_messages.append({"role": "user", "sender": ic_speaker, "content": ic_text})
                
                with st.spinner("Processing two-stage GM narrative..."):
                    # STAGE 1: Mechanical Analysis & Collation (Temp = 0.0)
                    stage1_sys = (
                        "You are an objective Pathfinder 1e rules engine. Analyze the character action and output "
                        "ONLY the strict mechanical facts, DCs, hits/misses, and state changes. Do not write creative prose."
                    )
                    stage1_prompt = f"Character: {ic_speaker}\nAction/Rolls: {ic_text}\nDetermine the strict mechanical outcome:"
                    
                    mechanical_facts = generate_gemini_response(
                        stage1_prompt, 
                        system_instruction=stage1_sys, 
                        temperature=0.0
                    )
                    
                    # STAGE 2: Prose & Storytelling Execution (Temp = 0.3)
                    stage2_sys = (
                        "You are a Play-By-Post Pathfinder 1e Game Master. Transform the provided mechanical outcome "
                        "into a concise, dramatic 1-2 paragraph narrative update. Stick strictly to the facts provided."
                    )
                    stage2_prompt = f"Player Action: {ic_text}\nMechanical Outcome: {mechanical_facts}\nWrite the GM narrative response:"
                    
                    prose_narrative = generate_gemini_response(
                        stage2_prompt, 
                        system_instruction=stage2_sys, 
                        temperature=0.3
                    )
                    
                    # 2. Append AI GM response
                    st.session_state.ic_messages.append({"role": "assistant", "sender": "GM (Gemini)", "content": prose_narrative})
                
                st.rerun()

    # --- RIGHT COLUMN: OOC LOG WITH ASK AI TOGGLE ---
    with col_ooc:
        st.markdown("### 💬 Out-of-Character (OOC)")
        
        ooc_container = st.container(height=380)
        with ooc_container:
            if "ooc_messages" not in st.session_state:
                st.session_state.ooc_messages = []
                
            for msg in st.session_state.ooc_messages:
                with st.chat_message(msg["role"]):
                    st.markdown(f"**{msg['sender']}**: {msg['content']}")

        # Dedicated OOC Input Form
        with st.form("ooc_input_form", clear_on_submit=True):
            ooc_speaker = st.text_input("OOC Name", value="Player/GM")
            ooc_text = st.text_area("OOC Message", placeholder="Heading out for lunch, back in an hour...", height=80)
            
            # AI Response Toggle Switch
            ask_ai = st.toggle("🤖 Ask AI GM to respond", value=False)
            submit_ooc = st.form_submit_button("Post OOC Message")

            if submit_ooc and ooc_text:
                # 1. Log human OOC message
                st.session_state.ooc_messages.append({"role": "user", "sender": ooc_speaker, "content": ooc_text})
                
                # 2. Respond only if toggle is enabled
                if ask_ai:
                    with st.spinner("AI GM is answering OOC query..."):
                        ooc_sys = "You are a Pathfinder 1e assistant GM. Give clear, helpful, and concise OOC advice or rules reference."
                        ooc_response = generate_gemini_response(
                            ooc_text, 
                            system_instruction=ooc_sys, 
                            temperature=0.2
                        )
                        st.session_state.ooc_messages.append({"role": "assistant", "sender": "OOC AI Assistant", "content": ooc_response})
                
                st.rerun()

# --- TAB 2: MODULE & DRIVE DATA ---
with tab_module:
    st.header("Campaign Module Browser")
    
    if folder_id and drive_service:
        files = fetch_drive_files(folder_id)
        if files:
            file_options = {f['name']: f for f in files}
            selected_filename = st.selectbox("Select Module Document to Read", list(file_options.keys()))
            
            selected_file = file_options[selected_filename]
            st.caption(f"File ID: `{selected_file['id']}` | Type: `{selected_file['mimeType']}`")
            
            col_doc1, col_doc2 = st.columns([1, 1])
            
            with col_doc1:
                read_btn = st.button("📖 Read Selected Document")
            with col_doc2:
                summarize_btn = st.button("✨ Summarize Document with Gemini")

            if 'doc_content' not in st.session_state:
                st.session_state.doc_content = ""

            if read_btn or summarize_btn:
                with st.spinner("Fetching document content..."):
                    st.session_state.doc_content = read_drive_file_content(selected_file['id'], selected_file['mimeType'])

            if st.session_state.doc_content:
                if summarize_btn:
                    with st.spinner("Gemini is summarizing module notes..."):
                        summary_prompt = f"You are a helpful Pathfinder 1e Assistant GM. Summarize the following campaign/module document into clear GM notes, encounters, and key details:\n\n{st.session_state.doc_content[:15000]}"
                        summary = generate_gemini_response(summary_prompt)
                        st.markdown("### ✨ Gemini Summary")
                        st.info(summary)

                st.markdown("### Document Content")
                view_mode = st.radio("Display Mode", ["Rendered Markdown", "Clean Text Area"], horizontal=True)
                
                if view_mode == "Rendered Markdown":
                    st.markdown(st.session_state.doc_content)
                else:
                    st.text_area("Clean Text View", value=st.session_state.doc_content, height=450)
        else:
            st.info("No files found or folder is empty.")
    else:
        st.warning("Google Drive Folder ID not configured in Streamlit Secrets.")

# --- TAB 3: GEMINI AI ASSISTANT ---
with tab_ai:
    st.header("🤖 Pathfinder 1e AI Assistant GM")
    st.caption("Powered by Gemini API (`gemini-3.5-flash`)")

    system_prompt = (
        "You are an expert Pathfinder 1e Game Master assistant. Provide concise, mechanically accurate answers "
        "regarding Pathfinder 1e rules, spell descriptions, monster stat blocks, tactical advice, and Play-By-Post narrative descriptions."
    )

    ai_mode = st.radio("Query Mode", ["Rules & Stat Block Lookup", "Generate Combat Narrative", "Custom Prompt"], horizontal=True)

    if ai_mode == "Rules & Stat Block Lookup":
        query = st.text_input("Enter Pathfinder 1e Rule, Spell, or Creature name:", placeholder="e.g. Haste spell mechanics or Goblin Commando stat block")
        if st.button("🔍 Search / Ask Gemini"):
            if query:
                with st.spinner("Consulting Pathfinder 1e rules..."):
                    response = generate_gemini_response(f"Explain the Pathfinder 1e mechanics or provide details for: {query}", system_instruction=system_prompt)
                    st.markdown("### Gemini Answer")
                    st.write(response)

    elif ai_mode == "Generate Combat Narrative":
        action_desc = st.text_area("Describe action/rolls for narrative boost:", value="Hyren swings his longsword at the goblin leader, dealing 14 damage.")
        if st.button("✍️ Draft PBP Narrative"):
            if action_desc:
                with st.spinner("Drafting Play-by-Post narrative..."):
                    prompt = f"Write a dramatic, immersive 1-2 paragraph Play-by-Post combat description based on these mechanics:\n{action_desc}"
                    narrative = generate_gemini_response(prompt, system_instruction=system_prompt)
                    st.markdown("### Generated Narrative Preview")
                    st.write(narrative)
                    if st.button("📋 Copy to Combat GM Log"):
                        st.session_state.gm_post = narrative
                        st.success("Copied to Combat tab prompt preview!")

    else:
        user_prompt = st.text_area("Enter any prompt for Gemini:", height=150)
        if st.button("🚀 Send Prompt"):
            if user_prompt:
                with st.spinner("Processing prompt..."):
                    result = generate_gemini_response(user_prompt, system_instruction=system_prompt)
                    st.markdown("### Gemini Output")
                    st.write(result)

# --- TAB 4: CHARACTER ROSTER INSPECTOR ---
with tab_players:
    st.header("Player Character Sheet Inspector")
    
    if characters:
        selected_char_name = st.selectbox("Select Character to Inspect", [c.get("character_name", c["id"]) for c in characters])
        selected_char = next((c for c in characters if c.get("character_name", c["id"]) == selected_char_name), None)
        
        if selected_char:
            st.json(selected_char)
    else:
        st.info("No character documents found in the Firestore 'characters' collection.")
