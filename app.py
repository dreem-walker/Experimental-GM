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
folder_id = st.secrets.get("GOOGLE_DRIVE_FOLDER_ID", "") or st.secrets.get("google_drive", {}).get("folder_id", "")


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
    """Read the structured state directives returned by the factual stage."""
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
    st.subheader(f"⚔️ Initiative — Round {state.get('current_round', 1)}")
    active_index = int(state.get("current_initiative_index", 0)) % len(order)
    cols = st.columns(len(order))
    for index, name in enumerate(order):
        with cols[index]:
            if index == active_index:
                st.success(f"**▶ {name}**\n\nCurrent turn")
            else:
                st.container().markdown(f"**{index + 1}. {name}**")
    st.divider()


def dual_chat(profile, state, characters):
    left, right = st.columns(2)
    combatant = active_combatant(state, characters)
    active_name = character_name(combatant) if combatant else ""
    unlocked = not state.get("is_in_combat") or profile == SYSTEM_TECHNICIAN or profile == active_name

    with left:
        st.markdown("### 📜 Story Log (In-Character)")
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
        st.markdown("### 💬 Out-of-Character (OOC)")
        messages = st.session_state.setdefault("ooc_messages", [])
        with st.container(height=380):
            for message in messages:
                with st.chat_message(message["role"]):
                    st.markdown(f"**{message['sender']}**: {message['content']}")
        with st.form("ooc_input_form", clear_on_submit=True):
            speaker = st.text_input("OOC Name", value=profile, disabled=True)
            message = st.text_area("OOC Message")
            ask_ai = st.toggle("🤖 Ask AI GM to respond")
            submitted = st.form_submit_button("Post OOC Message")
        if submitted and message.strip():
            response = gemini(message, "You are a helpful Pathfinder 1e assistant GM. Give concise OOC advice.", .2) if ask_ai else ""
            messages.append({"role": "user", "sender": speaker, "content": message})
            if response:
                messages.append({"role": "assistant", "sender": "OOC AI Assistant", "content": response})
            log_ooc(speaker, message, response)
            st.rerun()


# Role selection gate. The selected role is fixed for this browser session.
if "profile" not in st.session_state:
    st.title("🎲 Pathfinder 1e PBP GM Console")
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
st.sidebar.info(f"🎭 **Playing as:** {profile}")
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
tab_combat, tab_module, tab_ai, tab_players = st.tabs(["⚔️ Combat & Turn Runner", "📜 Module & Drive Notes", "🤖 Gemini Assistant", "👤 Character Roster"])

with tab_combat:
    st.header("Encounter Status")
    if state.get("is_in_combat"):
        st.success("Combat mode active — controlled by the AI GM")
    else:
        st.info(f"Exploration responses: {len(state.get('exploration_submitted_by', []))}/{threshold_for_party_size(len(characters))} required")
    initiative_display(state)
    st.subheader("Campaign Communications")
    dual_chat(profile, state, characters)

with tab_module:
    st.header("Campaign Module Browser")
    st.info("Google Drive document browsing remains available after configuring GOOGLE_DRIVE_FOLDER_ID.")

with tab_ai:
    st.header("🤖 Pathfinder 1e AI Assistant GM")
    prompt = st.text_area("Enter your prompt")
    if st.button("Send to Gemini") and prompt.strip():
        st.write(gemini(prompt, "You are an expert Pathfinder 1e Game Master assistant."))

with tab_players:
    st.header("Player Character Sheet Inspector")
    if characters:
        selected = st.selectbox("Select Character", [character_name(c) for c in characters])
        st.json(next(c for c in characters if character_name(c) == selected))
    else:
        st.info("No character documents found in Firestore.")
