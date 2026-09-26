import io
import json
import re

import requests
import streamlit as st
from bs4 import BeautifulSoup
from google.cloud import firestore
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SYSTEM_TECHNICIAN = "(System Technician)"
ROLE_PLACEHOLDER = "— Select a role —"

try:
    from google import genai
    HAS_GENAI = True
except ImportError:
    genai = None
    HAS_GENAI = False

try:
    import pypdf
except ImportError:
    pypdf = None
try:
    import docx
except ImportError:
    docx = None

st.set_page_config(page_title="Pathfinder 1e PBP GM Screen", layout="wide", initial_sidebar_state="expanded")


def secret_credentials():
    for key in ("firebase", "FIREBASE", "firebase_credentials"):
        if key in st.secrets:
            return dict(st.secrets[key])
    raise KeyError("Firebase credentials missing from Streamlit secrets.")


@st.cache_resource
def get_firebase_db():
    return firestore.Client(credentials=service_account.Credentials.from_service_account_info(secret_credentials()))


@st.cache_resource
def get_drive_service():
    credentials = service_account.Credentials.from_service_account_info(
        secret_credentials(), scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=credentials)


def get_drive_folder_id():
    if "GOOGLE_DRIVE_FOLDER_ID" in st.secrets:
        return st.secrets.get("GOOGLE_DRIVE_FOLDER_ID", "")
    if "google_drive" in st.secrets:
        return st.secrets.get("google_drive", {}).get("folder_id", "")
    return ""


try:
    db = get_firebase_db()
except Exception:
    db = None

try:
    drive_service = get_drive_service()
except Exception:
    drive_service = None

gemini_api_key = st.secrets.get("GEMINI_API_KEY", "")
discord_webhook_url = st.secrets.get("DISCORD_WEBHOOK_URL", "")
folder_id = get_drive_folder_id()


def pretty_key_name(key):
    cleaned = str(key).replace("-", " ").replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "Field"
    parts = [part for part in cleaned.split(" ") if part]
    pretty = []
    for part in parts:
        lower = part.lower()
        mapping = {
            "hp": "HP",
            "ac": "AC",
            "xp": "XP",
            "id": "ID",
            "ooc": "OOC",
            "gm": "GM",
            "ai": "AI",
            "url": "URL",
            "ui": "UI",
        }
        pretty.append(mapping.get(lower, lower.capitalize()))
    return " ".join(pretty)


def normalize_field_name(name):
    text = str(name).strip()
    text = text.replace("-", " ").replace("/", " ")
    text = re.sub(r"[^a-zA-Z0-9_\s]", "", text)
    text = re.sub(r"\s+", "_", text).strip("_")
    return text.lower() or "custom_field"


def parse_field_value(value):
    value = str(value).strip()
    if value == "":
        return ""
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def character_name(character):
    return character.get("character_name", character.get("name", character.get("id", "Unknown")))


def character_id(character):
    return character.get("id", character_name(character))


def fetch_characters():
    if not db:
        return []
    result = []
    for document in db.collection("characters").stream():
        value = document.to_dict()
        value["id"] = document.id
        result.append(value)
    return result


def threshold_for_party_size(size):
    if size <= 0:
        return 0
    return {1: 1, 2: 2, 3: 2, 4: 2, 5: 3, 6: 3, 7: 4, 8: 4}.get(size, size // 2 + 1)


def campaign_state():
    state = {
        "is_in_combat": False,
        "current_round": 1,
        "initiative_order": [],
        "current_initiative_index": 0,
        "exploration_submitted_by": [],
    }
    if db:
        document = db.collection("campaign_state").document("active_encounter").get()
        if document.exists:
            state.update(document.to_dict())
    return state


def save_state(state):
    if db:
        db.collection("campaign_state").document("active_encounter").set(state, merge=True)


def update_character(character_id_value, updates):
    if not db or not character_id_value:
        return False
    db.collection("characters").document(character_id_value).set(updates, merge=True)
    return True


def gemini(prompt, instruction=None, temperature=.7):
    if not gemini_api_key:
        return "Gemini API key missing from Streamlit secrets (GEMINI_API_KEY)."
    prompt = f"System Instruction: {instruction}\n\nUser Query: {prompt}" if instruction else prompt
    try:
        if HAS_GENAI:
            response = genai.Client(api_key=gemini_api_key).models.generate_content(
                model="gemini-3.5-flash-lite", contents=prompt, config={"temperature": temperature}
            )
            return response.text or ""
        return "The Gemini package is not installed."
    except Exception as exc:
        return f"Gemini API Error: {exc}"


def parse_combat_state(facts):
    started = bool(re.search(r"COMBAT_STARTED\s*:\s*YES", facts, re.IGNORECASE))
    ended = bool(re.search(r"COMBAT_ENDED\s*:\s*YES", facts, re.IGNORECASE))
    order = []
    match = re.search(r"INITIATIVE_ORDER\s*:\s*(\[.*?\])", facts, re.IGNORECASE | re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, list):
                order = [str(item).strip() for item in parsed if str(item).strip()]
        except (json.JSONDecodeError, TypeError):
            pass
    return started, ended, order


def apply_combat_directives(state, facts):
    started, ended, order = parse_combat_state(facts)
    if ended:
        state["is_in_combat"] = False
        state["current_round"] = 1
        state["current_initiative_index"] = 0
        state["initiative_order"] = []
        state["exploration_submitted_by"] = []
    elif started and not state.get("is_in_combat"):
        state["is_in_combat"] = True
        state["current_round"] = 1
        state["current_initiative_index"] = 0
        state["initiative_order"] = order
        state["exploration_submitted_by"] = []
    elif state.get("is_in_combat") and order and not state.get("initiative_order"):
        state["initiative_order"] = order
    save_state(state)
    return started, ended


def two_stage(speaker, action, state, characters):
    known_party = [character_name(character) for character in characters]
    facts = gemini(
        f"Character: {speaker}\nAction/Rolls: {action}\nKnown party: {known_party}\nCurrent combat state: {state.get('is_in_combat', False)}\n"
        "Determine the strict Pathfinder 1e mechanical outcome and whether immediate danger has begun or ended.",
        """You are an objective Pathfinder 1e rules engine using the supplied campaign/source context. Output mechanical facts, DCs, hits, misses, and state changes. At the end, always output exactly these directives on separate lines:
COMBAT_STARTED: YES or NO
COMBAT_ENDED: YES or NO
INITIATIVE_ORDER: a valid JSON array of strings, in turn order, including known party members and enemies. Use Enemy for unidentified creatures; use labels such as Goblin A or Thug 3 when identified. If combat has not started, use []. Never claim combat ended unless immediate danger has passed or enemies are dealt with.""", 0.0,
    )
    started, ended = apply_combat_directives(state, facts)
    prose = gemini(
        f"Player Action: {action}\nMechanical Outcome: {facts}\nCombat started in this response: {started}\nCombat ended in this response: {ended}\nWrite the GM narrative response.",
        """You are a Pathfinder 1e Play-By-Post GM. Write concise, dramatic prose based only on the supplied facts. If combat started, begin the prose with the exact marker **Combat Started!**. If combat ended, begin it with the exact marker **Combat Ended**. Otherwise do not add either marker. Include no hidden directives or JSON in the prose.""", 0.3,
    )
    if started and "Combat Started!" not in prose:
        prose = f"**Combat Started!**\n\n{prose}"
    if ended and "Combat Ended" not in prose:
        prose = f"**Combat Ended**\n\n{prose}"
    return facts, prose, started, ended


def log_story(value):
    if db:
        value = dict(value)
        value["timestamp"] = firestore.SERVER_TIMESTAMP
        db.collection("story_log").add(value)


def log_ooc(speaker, message, response=""):
    if db:
        db.collection("ooc_log").add({"speaker": speaker, "message": message, "ai_response": response, "timestamp": firestore.SERVER_TIMESTAMP})


def active_combatant(state, characters):
    order = state.get("initiative_order", [])
    if not order:
        return None
    active = order[int(state.get("current_initiative_index", 0)) % len(order)]
    return next((c for c in characters if character_id(c) == active or character_name(c) == active), {"id": active, "name": active})


def advance_turn(state):
    order = state.get("initiative_order", [])
    if order:
        next_index = (int(state.get("current_initiative_index", 0)) + 1) % len(order)
        if next_index == 0:
            state["current_round"] = int(state.get("current_round", 1)) + 1
        state["current_initiative_index"] = next_index
        save_state(state)


def initiative_display(state):
    order = state.get("initiative_order", [])
    if not state.get("is_in_combat") or not order:
        return
    st.markdown("### Initiative")
    active_index = int(state.get("current_initiative_index", 0)) % len(order)
    cols = st.columns(len(order))
    for index, name in enumerate(order):
        with cols[index]:
            if index == active_index:
                st.success(f"**{name}**\nCurrent turn")
            else:
                st.markdown(f"{index + 1}. {name}")
    st.divider()


def exploration_status(state, characters):
    if state.get("is_in_combat"):
        return
    player_names = [character_name(c) for c in characters if character_name(c) != SYSTEM_TECHNICIAN]
    submitted = state.get("exploration_submitted_by", [])
    required = threshold_for_party_size(len(characters))

    st.markdown(f"### Exploration Responses — {len(submitted)}/{required} required")
    for player in player_names:
        if player in submitted:
            st.write(f"Yes: {player}")
        else:
            st.write(f"No: {player}")
    st.divider()


def render_character_summary(character):
    ordered = []
    skip = {"id", "api", "created_at", "updated_at"}
    for key, value in character.items():
        if key in skip or key == "character_name":
            continue
        label = pretty_key_name(key)
        if value is None:
            display = "None"
        elif isinstance(value, bool):
            display = "True" if value else "False"
        elif isinstance(value, (int, float)):
            display = str(value)
        else:
            display = str(value)
        ordered.append((label, display))
    if "character_name" in character:
        ordered.insert(0, ("Character Name", str(character.get("character_name", ""))))
    elif "name" in character:
        ordered.insert(0, ("Character Name", str(character.get("name", ""))))
    return ordered


def inventory_edit_form(character, profile):
    if profile != SYSTEM_TECHNICIAN and profile != character_name(character):
        st.info("You can only edit your own character sheet.")
        return

    with st.form(f"edit_character_{character_id(character)}"):
        updates = {}
        for key, value in character.items():
            if key in {"id", "character_name", "name"}:
                continue
            if isinstance(value, (dict, list)):
                continue
            label = pretty_key_name(key)
            field_value = st.text_input(label, value=str(value) if value is not None else "")
            if field_value != str(value) if value is not None else "":
                updates[key] = parse_field_value(field_value)

        custom_name = st.text_input("Custom field name")
        custom_value = st.text_input("Custom field value")
        submitted = st.form_submit_button("Save character sheet")

        if submitted:
            if custom_name.strip():
                normalized = normalize_field_name(custom_name)
                updates[normalized] = parse_field_value(custom_value)
            if updates:
                if update_character(character.get("id"), updates):
                    st.success("Character sheet updated.")
                else:
                    st.error("Could not save changes.")
            else:
                st.info("No changes to save.")


def dual_chat(profile, state, characters):
    left, right = st.columns(2)
    combatant = active_combatant(state, characters)
    active_name = character_name(combatant) if combatant else ""
    unlocked = not state.get("is_in_combat") or profile == SYSTEM_TECHNICIAN or profile == active_name

    with left:
        st.markdown("### Story Log")
        story = st.session_state.setdefault("ic_messages", [])
        with st.container(height=380):
            for message in story:
                with st.chat_message(message["role"]):
                    st.markdown(f"**{message['sender']}**: {message['content']}")
        if state.get("is_in_combat") and combatant:
            st.info(f"Active initiative: **{active_name}**")
        with st.form("ic_input_form", clear_on_submit=True):
            speaker = profile
            st.text_input("Speaking As", value=profile, disabled=True)
            action = st.text_area("IC Action / Speech", disabled=not unlocked)
            submitted = st.form_submit_button("Post Action to Story", disabled=not unlocked)
        if not unlocked:
            st.caption(f"This input is locked until {active_name}'s turn.")
        if submitted and action.strip():
            story.append({"role": "user", "sender": speaker, "content": action})
            should_resolve = state.get("is_in_combat") or len(set(state.get("exploration_submitted_by", [])) | {speaker}) >= threshold_for_party_size(len(characters))
            if should_resolve:
                facts, prose, started, ended = two_stage(speaker, action, state, characters)
                story.append({"role": "assistant", "sender": "GM (Gemini)", "content": prose})
                log_story({"round": state.get("current_round", 1), "character_name": speaker, "action": action, "mechanical_outcome": facts, "narrative_prose": prose})
                if state.get("is_in_combat") and not ended:
                    advance_turn(state)
            else:
                submitted_by = set(state.get("exploration_submitted_by", []))
                submitted_by.add(speaker)
                state["exploration_submitted_by"] = sorted(submitted_by)
                save_state(state)
            st.rerun()

    with right:
        st.markdown("### OOC")
        messages = st.session_state.setdefault("ooc_messages", [])
        with st.container(height=380):
            for message in messages:
                with st.chat_message(message["role"]):
                    st.markdown(f"**{message['sender']}**: {message['content']}")
        with st.form("ooc_input_form", clear_on_submit=True):
            speaker = st.text_input("OOC Name", value=profile, disabled=True)
            message = st.text_area("OOC Message")
            ask_ai = st.toggle("Ask AI GM to respond")
            submitted = st.form_submit_button("Post OOC Message")
        if submitted and message.strip():
            response = gemini(message, "You are a helpful Pathfinder 1e assistant GM. Give concise OOC advice.", .2) if ask_ai else ""
            messages.append({"role": "user", "sender": speaker, "content": message})
            if response:
                messages.append({"role": "assistant", "sender": "OOC AI Assistant", "content": response})
            log_ooc(speaker, message, response)
            st.rerun()


if "profile" not in st.session_state:
    st.title("Pathfinder 1e PBP GM Console")
    st.markdown("---")
    st.subheader("Select Your Role")
    st.markdown("Choose your role to enter the campaign. You will be locked into it for this session.")
    roles = [SYSTEM_TECHNICIAN] + [character_name(c) for c in fetch_characters()]
    selected_profile = st.selectbox("Select Role", [ROLE_PLACEHOLDER] + roles)
    if st.button("Enter Campaign", type="primary", use_container_width=True, disabled=selected_profile == ROLE_PLACEHOLDER):
        st.session_state.profile = selected_profile
        st.rerun()
    st.stop()

profile = st.session_state.profile
characters = fetch_characters()
state = campaign_state()

st.sidebar.title("Campaign Control")
st.sidebar.info(f"Playing as: {profile}")
if profile == SYSTEM_TECHNICIAN:
    if db:
        st.sidebar.success("Firebase Connected")
    else:
        st.sidebar.error("Firebase Error: Not connected")
    if drive_service:
        st.sidebar.success("Google Drive API Ready")
    else:
        st.sidebar.warning("Drive API: Not available")
if st.sidebar.button("Change Role (Clear Session)"):
    del st.session_state.profile
    st.rerun()

st.sidebar.subheader(f"Active Party ({len(characters)})")
for character in characters:
    st.sidebar.markdown(f"**{character_name(character)}**")
    st.sidebar.caption(f"HP: {character.get('current_hp', character.get('hp', 'N/A'))}/{character.get('max_hp', 'N/A')} | Status: {character.get('status', 'Active')}")

st.title("Pathfinder 1e PBP GM Console")
if profile == SYSTEM_TECHNICIAN:
    tab_combat, tab_module, tab_players = st.tabs(["Combat & Turn Runner", "Module & Drive Notes", "Character Roster"])
else:
    tab_combat, tab_players = st.tabs(["Combat & Turn Runner", "Character Roster"])

with tab_combat:
    st.markdown("### Encounter Status")
    if state.get("is_in_combat"):
        st.success("Combat mode active — controlled by the AI GM")
    else:
        exploration_status(state, characters)
    initiative_display(state)
    st.subheader("Campaign Communications")
    dual_chat(profile, state, characters)

if profile == SYSTEM_TECHNICIAN:
    with tab_module:
        st.header("Campaign Module Browser")
        if folder_id and drive_service:
            try:
                results = drive_service.files().list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    spaces="drive",
                    fields="files(id, name, mimeType, webViewLink)",
                    pageSize=50,
                ).execute()
                files = results.get("files", [])
                if files:
                    st.success(f"Found {len(files)} file(s) in the configured folder")
                    for file in files:
                        col1, col2 = st.columns([3, 1])
                        with col1:
                            st.write(f"{file['name']}")
                        with col2:
                            st.link_button("Open", file.get("webViewLink", "#"))
                else:
                    st.info("No files found in the configured folder.")
            except Exception as exc:
                st.error(f"Error accessing Google Drive: {exc}")
        else:
            st.warning("Google Drive folder ID or API not configured. Check secrets.")

with tab_players:
    st.header("Character Roster")
    if not characters:
        st.info("No character documents found in Firestore.")
    else:
        available = [character_name(c) for c in characters]
        if profile == SYSTEM_TECHNICIAN:
            selected_name = st.selectbox("Select Character", available)
        else:
            selected_name = profile if profile in available else available[0]
        selected_character = next(c for c in characters if character_name(c) == selected_name)

        st.subheader("Character Details")
        for label, value in render_character_summary(selected_character):
            st.markdown(f"**{label}:** {value}")

        st.subheader("Update Character Sheet")
        inventory_edit_form(selected_character, profile)
