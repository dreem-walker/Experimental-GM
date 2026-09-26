import io
import requests
import streamlit as st
from bs4 import BeautifulSoup
from google.cloud import firestore
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from googleapiclient.errors import HttpError

try:
    from google import genai
    HAS_GENAI = True
except ImportError:
    genai = None
    HAS_GENAI = False

try:
    import google.generativeai as legacy_genai
    HAS_LEGACY_GENAI = True
except ImportError:
    legacy_genai = None
    HAS_LEGACY_GENAI = False

try:
    import pypdf
except ImportError:
    pypdf = None

try:
    import docx
except ImportError:
    docx = None

st.set_page_config(
    page_title="Pathfinder 1e PBP GM Screen",
    layout="wide",
    initial_sidebar_state="expanded",
)

# -------------------- Configuration and services --------------------

def get_secret_credentials():
    for name in ("firebase", "FIREBASE", "firebase_credentials"):
        if name in st.secrets:
            return dict(st.secrets[name])
    raise KeyError("Firebase credentials missing from Streamlit secrets.")


@st.cache_resource
def get_firebase_db():
    key_dict = get_secret_credentials()
    credentials = service_account.Credentials.from_service_account_info(key_dict)
    return firestore.Client(credentials=credentials)


@st.cache_resource
def get_drive_service():
    try:
        key_dict = get_secret_credentials()
    except KeyError:
        return None
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    credentials = service_account.Credentials.from_service_account_info(key_dict, scopes=scopes)
    return build("drive", "v3", credentials=credentials)


try:
    db = get_firebase_db()
    st.sidebar.success("Firebase Connected")
except Exception as exc:
    db = None
    st.sidebar.error(f"Firebase Error: {exc}")

try:
    drive_service = get_drive_service()
    if drive_service:
        st.sidebar.success("Google Drive API Ready")
except Exception as exc:
    drive_service = None
    st.sidebar.warning(f"Drive API Note: {exc}")

gemini_api_key = st.secrets.get("GEMINI_API_KEY", "")
discord_webhook_url = st.secrets.get("DISCORD_WEBHOOK_URL", "")
folder_id = st.secrets.get("GOOGLE_DRIVE_FOLDER_ID", "") or st.secrets.get("google_drive", {}).get("folder_id", "")

# -------------------- Helper functions --------------------

def character_name(character):
    return character.get("character_name", character.get("name", character.get("id", "Unknown")))


def character_id(character):
    return character.get("id", character_name(character))


def fetch_characters():
    if not db:
        return []
    result = []
    for document in db.collection("characters").stream():
        data = document.to_dict()
        data["id"] = document.id
        result.append(data)
    return result


def threshold_for_party_size(size):
    """Return the required exploration responses from the project's strict table."""
    if size <= 0:
        return 0
    return {1: 1, 2: 2, 3: 2, 4: 2, 5: 3, 6: 3, 7: 4, 8: 4}.get(size, (size // 2) + 1)


def get_campaign_state():
    default = {
        "is_in_combat": False,
        "current_round": 1,
        "initiative_order": [],
        "current_initiative_index": 0,
        "exploration_submitted_by": [],
    }
    if not db:
        return default
    data = db.collection("campaign_state").document("active_encounter").get()
    if data.exists:
        default.update(data.to_dict())
    return default


def save_campaign_state(state):
    if db:
        db.collection("campaign_state").document("active_encounter").set(state, merge=True)


def log_story_event(event):
    if db:
        event = dict(event)
        event["timestamp"] = firestore.SERVER_TIMESTAMP
        db.collection("story_log").add(event)


def log_ooc_event(speaker, message, ai_response=""):
    if db:
        db.collection("ooc_log").add({
            "speaker": speaker,
            "message": message,
            "ai_response": ai_response,
            "timestamp": firestore.SERVER_TIMESTAMP,
        })


def run_two_stage_narrative(speaker, action):
    mechanical = generate_gemini_response(
        f"Character: {speaker}\nAction/Rolls: {action}\nDetermine the strict mechanical outcome.",
        "You are an objective Pathfinder 1e rules engine. Output only mechanical facts, DCs, hits/misses, and state changes. Do not write creative prose.",
        temperature=0.0,
    )
    prose = generate_gemini_response(
        f"Player Action: {action}\nMechanical Outcome: {mechanical}\nWrite the GM narrative response.",
        "You are a Play-By-Post Pathfinder 1e Game Master. Turn the supplied mechanical outcome into a concise, dramatic 1-2 paragraph narrative. Do not invent facts.",
        temperature=0.3,
    )
    return mechanical, prose


def generate_gemini_response(prompt, system_instruction=None, temperature=0.7):
    if not gemini_api_key:
        return "Gemini API key missing from Streamlit secrets (GEMINI_API_KEY)."
    full_prompt = f"System Instruction: {system_instruction}\n\nUser Query: {prompt}" if system_instruction else prompt
    try:
        if HAS_GENAI:
            client = genai.Client(api_key=gemini_api_key)
            response = client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=full_prompt,
                config={"temperature": temperature},
            )
            return response.text
        if HAS_LEGACY_GENAI:
            legacy_genai.configure(api_key=gemini_api_key)
            model = legacy_genai.GenerativeModel("gemini-3.5-flash-lite")
            response = model.generate_content(
                full_prompt,
                generation_config=legacy_genai.types.GenerationConfig(temperature=temperature),
            )
            return response.text
        return "Neither Gemini package is installed."
    except Exception as exc:
        return f"Gemini API Error: {exc}"


def send_discord_message(content):
    if not discord_webhook_url:
        st.error("Discord Webhook URL not found in secrets.")
        return False
    try:
        response = requests.post(discord_webhook_url, json={"content": content}, timeout=15)
        return response.status_code in (200, 204)
    except requests.RequestException as exc:
        st.error(f"Discord Error: {exc}")
        return False


def fetch_drive_files(target_folder_id):
    if not drive_service or not target_folder_id:
        return []
    try:
        result = drive_service.files().list(
            q=f"'{target_folder_id}' in parents and trashed = false",
            fields="files(id, name, mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        return result.get("files", [])
    except HttpError as exc:
        st.error(f"Google Drive API Error: {exc}")
        return []


def clean_extracted_text(text):
    if any(marker in text.lower() for marker in ("<html", "<body", "</")):
        soup = BeautifulSoup(text, "html.parser")
        for script in soup(["script", "style"]):
            script.decompose()
        text = soup.get_text(separator="\n")
    return "\n\n".join(line.strip() for line in text.splitlines() if line.strip())


def parse_pdf_stream(stream):
    if not pypdf:
        return "PDF parsing requires pypdf in requirements.txt."
    try:
        reader = pypdf.PdfReader(stream)
        return "\n\n".join(page.extract_text() or "" for page in reader.pages).strip() or "No text could be extracted."
    except Exception as exc:
        return f"PDF Parsing Error: {exc}"


def parse_docx_stream(stream):
    if not docx:
        return "DOCX parsing requires python-docx in requirements.txt."
    try:
        document = docx.Document(stream)
        return "\n\n".join(p.text for p in document.paragraphs if p.text.strip()) or "No text found."
    except Exception as exc:
        return f"DOCX Parsing Error: {exc}"


def read_drive_file_content(file_id, mime_type):
    if not drive_service:
        return "Drive service unavailable."
    try:
        if "application/vnd.google-apps.document" in mime_type:
            request = drive_service.files().export_media(fileId=file_id, mimeType="text/plain")
        else:
            request = drive_service.files().get_media(fileId=file_id)
        stream = io.BytesIO()
        downloader = MediaIoBaseDownload(stream, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        stream.seek(0)
        if "pdf" in mime_type.lower():
            return clean_extracted_text(parse_pdf_stream(stream))
        if "wordprocessingml" in mime_type.lower() or "docx" in mime_type.lower():
            return clean_extracted_text(parse_docx_stream(stream))
        return clean_extracted_text(stream.read().decode("utf-8", errors="ignore"))
    except Exception as exc:
        return f"Error reading file content: {exc}"


def active_combatant(state, characters):
    order = state.get("initiative_order", [])
    if not order:
        return None
    index = int(state.get("current_initiative_index", 0)) % len(order)
    active_id = order[index]
    return next((c for c in characters if character_id(c) == active_id or character_name(c) == active_id), {"id": active_id, "name": active_id})


def advance_initiative(state, characters):
    order = state.get("initiative_order", [])
    if not order:
        return state
    next_index = (int(state.get("current_initiative_index", 0)) + 1) % len(order)
    state["current_initiative_index"] = next_index
    if next_index == 0:
        state["current_round"] = int(state.get("current_round", 1)) + 1
    save_campaign_state(state)
    return state


def render_dual_chat_system(profile, state, characters):
    """Render the side-by-side IC/OOC chat below the combat controls."""
    st.subheader("Campaign Communications")
    col_ic, col_ooc = st.columns(2)
    combatant = active_combatant(state, characters)
    active_name = character_name(combatant) if combatant else ""
    is_gm = profile == "GM"
    can_submit_ic = is_gm or not state.get("is_in_combat") or profile == active_name

    with col_ic:
        st.markdown("### 📜 Story Log (In-Character)")
        messages = st.session_state.setdefault("ic_messages", [])
        with st.container(height=380):
            for message in messages:
                with st.chat_message(message["role"]):
                    st.markdown(f"**{message['sender']}**: {message['content']}")
        if state.get("is_in_combat") and combatant:
            st.info(f"Active initiative: **{active_name}**")
        elif state.get("is_in_combat"):
            st.warning("Combat is active, but initiative order is not configured.")
        with st.form("ic_input_form", clear_on_submit=True):
            speaker_options = ["GM"] + [character_name(c) for c in characters]
            default_index = speaker_options.index(profile) if profile in speaker_options else 0
            speaker = st.selectbox("Speaking As", speaker_options, index=default_index)
            action = st.text_area("IC Action / Speech", placeholder="I swing my longsword at the hobgoblin sergeant...", disabled=not can_submit_ic)
            submitted = st.form_submit_button("Post Action to Story", disabled=not can_submit_ic)
        if not can_submit_ic:
            st.caption(f"This input is locked until {active_name}'s turn.")
        if submitted and action.strip():
            messages.append({"role": "user", "sender": speaker, "content": action})
            if state.get("is_in_combat"):
                mechanical, prose = run_two_stage_narrative(speaker, action)
                messages.append({"role": "assistant", "sender": "GM (Gemini)", "content": prose})
                log_story_event({"round": state.get("current_round", 1), "character_name": speaker, "action": action, "mechanical_outcome": mechanical, "narrative_prose": prose})
                advance_initiative(state, characters)
            else:
                submitted_by = set(state.get("exploration_submitted_by", []))
                submitted_by.add(speaker)
                state["exploration_submitted_by"] = sorted(submitted_by)
                required = threshold_for_party_size(len(characters))
                if len(submitted_by) >= required:
                    _, prose = run_two_stage_narrative("the party", action)
                    messages.append({"role": "assistant", "sender": "GM (Gemini)", "content": prose})
                    log_story_event({"round": state.get("current_round", 1), "character_name": "the party", "action": action, "mechanical_outcome": "Exploration threshold reached", "narrative_prose": prose})
                    state["exploration_submitted_by"] = []
                save_campaign_state(state)
            st.rerun()

    with col_ooc:
        st.markdown("### 💬 Out-of-Character (OOC)")
        messages = st.session_state.setdefault("ooc_messages", [])
        with st.container(height=380):
            for message in messages:
                with st.chat_message(message["role"]):
                    st.markdown(f"**{message['sender']}**: {message['content']}")
        with st.form("ooc_input_form", clear_on_submit=True):
            ooc_speaker = st.text_input("OOC Name", value=profile)
            ooc_text = st.text_area("OOC Message", placeholder="Heading out for lunch...")
            ask_ai = st.toggle("🤖 Ask AI GM to respond", value=False)
            submit_ooc = st.form_submit_button("Post OOC Message")
        if submit_ooc and ooc_text.strip():
            ai_response = ""
            messages.append({"role": "user", "sender": ooc_speaker, "content": ooc_text})
            if ask_ai:
                ai_response = generate_gemini_response(ooc_text, "You are a helpful Pathfinder 1e assistant GM. Give concise OOC advice.", 0.2)
                messages.append({"role": "assistant", "sender": "OOC AI Assistant", "content": ai_response})
            log_ooc_event(ooc_speaker, ooc_text, ai_response)
            st.rerun()


# -------------------- Sidebar and main interface --------------------
st.sidebar.title("Campaign Control")
characters = fetch_characters()
profile_options = ["GM"] + [character_name(c) for c in characters]
profile = st.sidebar.selectbox("Viewing as", profile_options, key="current_profile")
state = get_campaign_state()
state["is_in_combat"] = st.sidebar.toggle("⚔️ Combat Mode Active", value=bool(state.get("is_in_combat", False)), key="combat_mode_toggle")
if state["is_in_combat"] != get_campaign_state().get("is_in_combat"):
    save_campaign_state({"is_in_combat": state["is_in_combat"]})

st.sidebar.subheader(f"Active Party ({len(characters)})")
for character in characters:
    hp = character.get("current_hp", character.get("hp", "N/A"))
    max_hp = character.get("max_hp", "N/A")
    st.sidebar.markdown(f"**{character_name(character)}**")
    st.sidebar.caption(f"HP: {hp}/{max_hp} | Status: {character.get('status', 'Active')}")

if gemini_api_key:
    st.sidebar.caption("🤖 Gemini API Key Configured")
if discord_webhook_url:
    st.sidebar.caption("💬 Discord Webhook Configured")

st.title("Pathfinder 1e PBP GM Console")
tab_combat, tab_module, tab_ai, tab_players = st.tabs(["⚔️ Combat & Turn Runner", "📜 Module & Drive Notes", "🤖 Gemini Assistant", "👤 Character Roster"])

with tab_combat:
    st.header("Encounter Status")
    col1, col2 = st.columns([2, 1])
    with col1:
        current_round = st.number_input("Round Number", min_value=1, value=int(state.get("current_round", 1)), key="round_number")
        if profile == "GM":
            initiative_text = st.text_input("Initiative order (IDs or names, comma-separated)", value=", ".join(state.get("initiative_order", [])))
            if st.button("Save Initiative Order"):
                state["initiative_order"] = [item.strip() for item in initiative_text.split(",") if item.strip()]
                state["current_round"] = current_round
                state["current_initiative_index"] = 0
                save_campaign_state(state)
                st.success("Initiative order saved.")
        combatant = active_combatant(state, characters)
        st.info(f"Current turn: **{character_name(combatant) if combatant else 'Not configured'}** | Round {current_round}")
        if not state.get("is_in_combat"):
            required = threshold_for_party_size(len(characters))
            st.info(f"Exploration responses: {len(state.get('exploration_submitted_by', []))}/{required} required")
    with col2:
        st.subheader("Quick Actions")
        if st.button("🎲 Roll Party Perception"):
            st.info("Dice rolling is not connected yet.")
        if st.button("🛡️ Check Party Defenses"):
            st.info("Defense checks are not connected yet.")
        if st.button("Post Turn Update to Discord"):
            if send_discord_message(f"Round {current_round}: {character_name(combatant) if combatant else 'Campaign update'}"):
                st.success("Sent to Discord successfully.")
    st.divider()
    render_dual_chat_system(profile, state, characters)

with tab_module:
    st.header("Campaign Module Browser")
    if folder_id and drive_service:
        files = fetch_drive_files(folder_id)
        if files:
            file_options = {item["name"]: item for item in files}
            selected = st.selectbox("Select Module Document", list(file_options))
            selected_file = file_options[selected]
            read_btn = st.button("📖 Read Selected Document")
            summarize_btn = st.button("✨ Summarize Document with Gemini")
            if read_btn or summarize_btn:
                st.session_state.doc_content = read_drive_file_content(selected_file["id"], selected_file["mimeType"])
            content = st.session_state.get("doc_content", "")
            if content:
                if summarize_btn:
                    st.info(generate_gemini_response(f"Summarize these Pathfinder module notes into encounters and GM details:\n\n{content}"))
                st.markdown(content)
        else:
            st.info("No files found or folder is empty.")
    else:
        st.warning("Google Drive Folder ID not configured in Streamlit Secrets.")

with tab_ai:
    st.header("🤖 Pathfinder 1e AI Assistant GM")
    system_prompt = "You are an expert Pathfinder 1e Game Master assistant. Be concise and mechanically accurate."
    mode = st.radio("Query Mode", ["Rules & Stat Block Lookup", "Generate Combat Narrative", "Custom Prompt"], horizontal=True)
    prompt = st.text_area("Enter your prompt")
    if st.button("Send to Gemini") and prompt.strip():
        st.write(generate_gemini_response(prompt, system_prompt))

with tab_players:
    st.header("Player Character Sheet Inspector")
    if characters:
        selected_name = st.selectbox("Select Character", [character_name(c) for c in characters])
        selected = next(c for c in characters if character_name(c) == selected_name)
        st.json(selected)
    else:
        st.info("No character documents found in Firestore.")
