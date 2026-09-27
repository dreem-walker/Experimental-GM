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
CHAT_HISTORY_EVENT_LIMIT = 80
CHAT_COMPACTION_BATCH = 20
CHAT_SUMMARY_MAX_CHARS = 12000
RESET_CONFIRMATION = "RESET CAMPAIGN"
STAGE_TWO_INSTRUCTION = """You are the narrative prose and scene author (Stage 2) for a Pathfinder 1e solo tabletop roleplaying game. Your output must strictly adhere to the following behavioral and pacing rules on every turn:
1. Single-Beat Control: Narrate only the immediate response of the world, environment, or NPCs to the player's prompt. Stop immediately after that single beat resolves. Do not auto-pilot future steps, assume transitions, or rush to quest objectives.
2. Character Agency Protection: Never invent unprompted dialogue, decisions, or actions for the player character. Expound on the player's stated actions using sensory details, but do not rewrite their intent or parrot their prompt word-for-word.
"""

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


@st.cache_resource
def get_genai_client():
    if HAS_GENAI and gemini_api_key:
        return genai.Client(api_key=gemini_api_key)
    return None


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


def default_campaign_state():
    return {
        "is_in_combat": False,
        "current_round": 1,
        "initiative_order": [],
        "current_initiative_index": 0,
        "exploration_submitted_by": [],
        "exploration_passed_by": [],
        "pending_actions": [],
    }


def campaign_state():
    state = default_campaign_state()
    if db:
        document = db.collection("campaign_state").document("active_encounter").get()
        if document.exists:
            state.update(document.to_dict())
    return state


def save_state(state):
    if db:
        db.collection("campaign_state").document("active_encounter").set(state, merge=True)


def delete_collection_documents(collection_name):
    """Delete a Firestore collection in batches and return the number removed."""
    if not db:
        return 0
    deleted = 0
    while True:
        documents = list(db.collection(collection_name).limit(400).stream())
        if not documents:
            break
        batch = db.batch()
        for document in documents:
            batch.delete(document.reference)
        batch.commit()
        deleted += len(documents)
    return deleted


def reset_campaign_story():
    """Clear story/session history while preserving characters and module files."""
    if not db:
        return False, "Firebase is not connected."

    deleted = {}
    for collection_name in ("story_log", "ooc_log", "chat_summaries"):
        deleted[collection_name] = delete_collection_documents(collection_name)

    db.collection("campaign_state").document("active_encounter").set(
        default_campaign_state(), merge=False
    )
    return True, deleted


def update_character(character_id_value, updates):
    if not db or not character_id_value:
        return False
    db.collection("characters").document(character_id_value).set(updates, merge=True)
    return True


def gemini(prompt, instruction=None, temperature=.7):
    if not gemini_api_key:
        return "Gemini API key missing from Streamlit secrets (GEMINI_API_KEY)."

    client = get_genai_client()
    if not client:
        return "The Gemini package is not installed or client failed to initialize."

    contents = f"System Instruction: {instruction}\n\nUser Query: {prompt}" if instruction else prompt
    try:
        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=contents,
            config={"temperature": temperature}
        )
        return response.text or ""
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


def apply_combat_directives(state, facts, actor_name=None):
    started, ended, order = parse_combat_state(facts)
    if ended:
        state["is_in_combat"] = False
        state["current_round"] = 1
        state["current_initiative_index"] = 0
        state["initiative_order"] = []
        state["exploration_submitted_by"] = []
        state["exploration_passed_by"] = []
        state["pending_actions"] = []
    elif started and not state.get("is_in_combat"):
        state["is_in_combat"] = True
        state["current_round"] = 1
        state["initiative_order"] = order
        state["exploration_submitted_by"] = []
        state["exploration_passed_by"] = []
        state["pending_actions"] = []
        if order:
            state["current_initiative_index"] = (
                order.index(actor_name) if actor_name in order else len(order) - 1
            )
        else:
            state["current_initiative_index"] = 0
    elif state.get("is_in_combat") and order and not state.get("initiative_order"):
        state["initiative_order"] = order
    save_state(state)
    return started, ended


def two_stage(speaker, action, state, characters):
    known_party = [character_name(character) for character in characters]
    facts = gemini(
        f"Character: {speaker}\nAction/Rolls: {action}\nKnown party: {known_party}\nCurrent combat state: {state.get('is_in_combat', False)}\n"
        "Determine the strict Pathfinder 1e mechanical outcome and whether immediate danger has begun or ended.",
        """You are an objective Pathfinder 1e rules engine using the supplied campaign/source context. Output mechanical facts, DCs, hits, misses, and state changes. At the end, always output exactly:
COMBAT_STARTED: YES or NO
COMBAT_ENDED: YES or NO
INITIATIVE_ORDER: a valid JSON array of strings, in turn order, including known party members and enemies. Use Enemy for unidentified creatures; use labels such as Goblin A or Thug 3 when identified.""",
        0.0,
    )
    started, ended = apply_combat_directives(state, facts, speaker)
    prose = gemini(
        f"Player Action: {action}\nMechanical Outcome: {facts}\nCombat started in this response: {started}\nCombat ended in this response: {ended}\nWrite the GM narrative response.",
        STAGE_TWO_INSTRUCTION + """
Additional output requirements: Base the prose only on the supplied mechanical outcome. If combat started, begin the prose with the exact marker **Combat Started!**. If combat ended, begin with **Combat Ended**.""",
        0.3,
    )
    if started and "Combat Started!" not in prose:
        prose = f"**Combat Started!**\n\n{prose}"
    if ended and "Combat Ended" not in prose:
        prose = f"**Combat Ended**\n\n{prose}"
    return facts, prose, started, ended


def pending_story_messages(state):
    """Return queued player actions so every session can see them before resolution."""
    messages = []
    for pending in state.get("pending_actions", []):
        action = str(pending.get("action", "")).strip()
        if action:
            messages.append({
                "role": "user",
                "sender": f"{pending.get('character_name', 'Unknown')} (pending)",
                "content": action,
            })
    return messages


def exploration_ready(state, characters):
    """Return whether enough players are posted or passed and an action is queued."""
    pending = state.get("pending_actions", [])
    ready_by = set(state.get("exploration_submitted_by", []))
    return bool(pending) and len(ready_by) >= threshold_for_party_size(len(characters))


def resolve_pending_actions(state, characters):
    """Resolve all queued actions as one shared beat, then persist each player action."""
    pending = [item for item in state.get("pending_actions", []) if item.get("action", "").strip()]
    if not pending:
        return []

    speaker = pending[-1].get("character_name", "Unknown")
    combined_action = "\n".join(
        f"{item.get('character_name', 'Unknown')}: {item.get('action', '').strip()}"
        for item in pending
    )
    round_number = state.get("current_round", 1)
    facts, prose, started, ended = two_stage(speaker, combined_action, state, characters)

    events = []
    for index, item in enumerate(pending):
        event = {
            "round": round_number,
            "character_name": item.get("character_name", "Unknown"),
            "action": item.get("action", "").strip(),
            "mechanical_outcome": facts if index == len(pending) - 1 else "",
            "narrative_prose": prose if index == len(pending) - 1 else "",
        }
        events.append(event)
        log_story(event)

    state["pending_actions"] = []
    state["exploration_submitted_by"] = []
    state["exploration_passed_by"] = []
    if state.get("is_in_combat") and not ended:
        advance_turn(state)
        events.extend(resolve_enemy_turns(state, characters))
    save_state(state)
    return [message for event in events for message in chat_event_messages("story", event)]


def chat_event_messages(channel, event):
    """Convert a persisted event into the messages shown in the chat UI."""
    if channel == "story":
        messages = []
        speaker = event.get("character_name", "Unknown")
        action = str(event.get("action", "")).strip()
        prose = str(event.get("narrative_prose", "")).strip()
        if action == "[AI enemy turn]":
            if prose:
                return [{"role": "assistant", "sender": speaker, "content": prose}]
            return []
        if action:
            messages.append({"role": "user", "sender": speaker, "content": action})
        if prose:
            messages.append({"role": "assistant", "sender": "GM (Gemini)", "content": prose})
        return messages

    messages = []
    speaker = event.get("speaker", "Unknown")
    message = str(event.get("message", "")).strip()
    response = str(event.get("ai_response", "")).strip()
    if message:
        messages.append({"role": "user", "sender": speaker, "content": message})
    if response:
        messages.append({"role": "assistant", "sender": "OOC AI Assistant", "content": response})
    return messages


def load_chat_messages(channel):
    if not db:
        return []
    collection_name = "story_log" if channel == "story" else "ooc_log"
    try:
        snapshots = list(
            db.collection(collection_name)
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(CHAT_HISTORY_EVENT_LIMIT)
            .stream()
        )
    except Exception:
        return []

    messages = []
    for snapshot in reversed(snapshots):
        messages.extend(chat_event_messages(channel, snapshot.to_dict()))
    return messages


def load_chat_summary(channel):
    if not db:
        return ""
    try:
        summary = db.collection("chat_summaries").document(channel).get()
        return str(summary.to_dict().get("summary", "")) if summary.exists else ""
    except Exception:
        return ""


def compact_chat_log(channel):
    """Keep recent events and fold older events into one bounded summary."""
    if not db:
        return
    collection_name = "story_log" if channel == "story" else "ooc_log"
    try:
        recent = list(
            db.collection(collection_name)
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(CHAT_HISTORY_EVENT_LIMIT + 1)
            .stream()
        )
        if len(recent) <= CHAT_HISTORY_EVENT_LIMIT:
            return

        old_events = list(
            db.collection(collection_name)
            .order_by("timestamp", direction=firestore.Query.ASCENDING)
            .limit(CHAT_COMPACTION_BATCH)
            .stream()
        )
        if not old_events:
            return

        existing_summary = load_chat_summary(channel)
        event_text = "\n".join(
            f"{message['sender']}: {message['content']}"
            for snapshot in old_events
            for message in chat_event_messages(channel, snapshot.to_dict())
        )
        generated = gemini(
            f"Existing campaign summary:\n{existing_summary}\n\nOlder {channel} events:\n{event_text}",
            """Create a compact factual continuity summary for a Pathfinder campaign. Preserve
important NPCs, locations, unresolved hooks, decisions, combat consequences, and player
preferences. Omit greetings and repeated prose. Keep it under 1500 words and do not invent facts.""",
            0.1,
        )
        if generated.startswith(("Gemini API Error:", "Gemini API key missing", "The Gemini package")):
            generated = "\n".join(part for part in (existing_summary, event_text) if part).strip()
        generated = generated[:CHAT_SUMMARY_MAX_CHARS]

        db.collection("chat_summaries").document(channel).set(
            {"summary": generated, "updated_at": firestore.SERVER_TIMESTAMP}, merge=True
        )
        batch = db.batch()
        for snapshot in old_events:
            batch.delete(snapshot.reference)
        batch.commit()
    except Exception:
        return


def log_story(value):
    if db:
        value = dict(value)
        value["timestamp"] = firestore.SERVER_TIMESTAMP
        db.collection("story_log").add(value)
        compact_chat_log("story")


def log_ooc(speaker, message, response=""):
    if db:
        db.collection("ooc_log").add({"speaker": speaker, "message": message, "ai_response": response, "timestamp": firestore.SERVER_TIMESTAMP})
        compact_chat_log("ooc")


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


def is_player_combatant(combatant_name, characters):
    value = str(combatant_name)
    return any(
        character_name(character) != SYSTEM_TECHNICIAN
        and value in {str(character_name(character)), str(character_id(character))}
        for character in characters
    )


def resolve_enemy_turns(state, characters):
    """Resolve consecutive AI-controlled turns until the next player turn."""
    events = []
    order = state.get("initiative_order", [])
    max_enemy_turns = max(len(order), 1)

    for _ in range(max_enemy_turns):
        if not state.get("is_in_combat"):
            break
        active = active_combatant(state, characters)
        if not active:
            break
        enemy_name = character_name(active)
        if is_player_combatant(enemy_name, characters):
            break

        facts = gemini(
            f"Active enemy: {enemy_name}\nKnown party: {[character_name(character) for character in characters]}\nCombat state: {state}",
            """You are the AI GM controlling the active enemy in Pathfinder 1e. Resolve exactly
one legal turn for that enemy using only the supplied campaign context. Do not decide or
speak for any player character. Include the enemy's action, target, rolls or DCs when known,
and consequences. At the end, always output exactly:
COMBAT_STARTED: NO
COMBAT_ENDED: YES or NO
INITIATIVE_ORDER: a valid JSON array of strings, in turn order.""",
            0.0,
        )
        _, ended = apply_combat_directives(state, facts, enemy_name)
        prose = gemini(
            f"Enemy: {enemy_name}\nMechanical Outcome: {facts}\nCombat ended: {ended}",
            STAGE_TWO_INSTRUCTION + """
Additional output requirements: Narrate only the supplied enemy turn and its consequences. Do not write a player action. If combat ended, begin with **Combat Ended**.""",
            0.3,
        )
        if ended and "Combat Ended" not in prose:
            prose = f"**Combat Ended**\n\n{prose}"

        event = {
            "round": state.get("current_round", 1),
            "character_name": enemy_name,
            "action": "[AI enemy turn]",
            "mechanical_outcome": facts,
            "narrative_prose": prose,
        }
        events.append(event)
        log_story(event)
        if ended:
            break
        advance_turn(state)

    return events


def send_discord_notification(message):
    """Send one notification without allowing webhook failures to break the app."""
    if not discord_webhook_url:
        return False
    try:
        response = requests.post(
            discord_webhook_url,
            json={"content": message},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.RequestException:
        return False


def player_characters(characters):
    return [character for character in characters if character_name(character) != SYSTEM_TECHNICIAN]


def active_player_name(state, characters):
    combatant = active_combatant(state, characters)
    if not combatant:
        return None
    name = character_name(combatant)
    return name if is_player_combatant(name, characters) else None


def discord_story_notifications(state, characters, combat_started=False):
    """Build low-noise notifications for one resolved story beat."""
    if len(player_characters(characters)) <= 1:
        return []

    notifications = ["📖 Story updated."]
    if state.get("is_in_combat"):
        player_name = active_player_name(state, characters)
        if player_name:
            prefix = "⚔️ Combat started" if combat_started else "⚔️ Initiative"
            notifications.append(f"{prefix} — {player_name} is up in initiative.")
    return notifications


def notify_discord_story_update(state, characters, combat_started=False):
    for message in discord_story_notifications(state, characters, combat_started):
        send_discord_notification(message)


def pending_responses(state, characters):
    if state.get("is_in_combat"):
        st.markdown("### Initiative")
        order = state.get("initiative_order", [])
        if not order:
            st.info("No active initiative order yet.")
            return
        active_index = int(state.get("current_initiative_index", 0)) % len(order)
        for index, name in enumerate(order):
            badge = "Current turn" if index == active_index else "Waiting"
            st.write(f"{name}: {badge}")
        st.divider()
        return

    st.markdown("### Pending Responses")
    player_names = [character_name(c) for c in characters if character_name(c) != SYSTEM_TECHNICIAN]
    submitted = set(state.get("exploration_submitted_by", []))
    passed = set(state.get("exploration_passed_by", []))
    threshold = threshold_for_party_size(len(characters))
    st.caption(f"Ready: {len(submitted)}/{threshold} players")
    for player in player_names:
        if player in passed:
            status = "Passed / ready"
        elif player in submitted:
            status = "Posted"
        else:
            status = "(pending)"
        st.write(f"{player}: {status}")
    if state.get("pending_actions"):
        st.caption("Posted actions are visible in Story Log while waiting for the remaining players.")
    st.divider()


def ordered_character_fields(character):
    """Return core identity/HP fields first, followed by custom resource fields."""
    field_groups = [
        ("character_name", ("character_name", "name")),
        ("player_name", ("player_name", "player")),
        ("current_hp", ("current_hp", "hp")),
        ("max_hp", ("max_hp",)),
    ]
    ordered = []
    reserved = set()
    for _, candidates in field_groups:
        reserved.update(candidates)
        match = next((key for key in candidates if key in character), None)
        if match:
            ordered.append(match)

    ignored = {"id", "api", "created_at", "updated_at", "status", "active"}
    ordered.extend(
        key for key in character
        if key not in reserved and key not in ignored
    )
    return ordered


def character_field_label(key):
    if key in {"character_name", "name"}:
        return "Character Name"
    if key in {"player_name", "player"}:
        return "Player Name"
    if key in {"current_hp", "hp"}:
        return "Current HP"
    if key == "max_hp":
        return "Max HP"
    return pretty_key_name(key)


def render_character_summary(character):
    summary = []
    for key in ordered_character_fields(character):
        value = character.get(key)
        label = character_field_label(key)
        if value is None:
            display = "None"
        elif isinstance(value, bool):
            display = "True" if value else "False"
        elif isinstance(value, (int, float)):
            display = str(value)
        else:
            display = str(value)
        summary.append((label, display))
    return summary


def editable_character_fields(character):
    """Return roster fields and guarantee visible Current HP and Max HP inputs."""
    fields = ordered_character_fields(character)
    current_hp_key = next((key for key in ("current_hp", "hp") if key in fields), None)
    if current_hp_key is None:
        identity_keys = [key for key in ("character_name", "name", "player_name", "player") if key in fields]
        insert_at = max((fields.index(key) for key in identity_keys), default=-1) + 1
        fields.insert(insert_at, "current_hp")
        current_hp_key = "current_hp"

    if "max_hp" not in fields:
        fields.insert(fields.index(current_hp_key) + 1, "max_hp")
    return fields


def inventory_edit_form(character, profile):
    if profile != SYSTEM_TECHNICIAN and profile != character_name(character):
        st.info("You can only edit your own character sheet.")
        return

    with st.form(f"edit_character_{character_id(character)}"):
        updates = {}
        for key in editable_character_fields(character):
            if key in {"id", "character_name", "name"}:
                continue
            value = character.get(key)
            if isinstance(value, (dict, list)):
                continue
            label = character_field_label(key)
            original_value = str(value) if value is not None else ""
            if key in {"current_hp", "hp", "max_hp"}:
                hp_value = int(original_value) if re.fullmatch(r"-?\d+", original_value.strip()) else 0
                field_value = st.number_input(
                    label,
                    value=hp_value,
                    step=1,
                    key=f"character_{character_id(character)}_{key}",
                )
                if not original_value or field_value != hp_value:
                    updates[key] = int(field_value)
            else:
                field_value = st.text_input(label, value=original_value)
                if field_value != original_value:
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
        if "ic_messages" not in st.session_state:
            st.session_state.ic_messages = load_chat_messages("story")
        story = st.session_state.ic_messages
        story_summary = load_chat_summary("story")
        if story_summary:
            with st.expander("Earlier campaign summary"):
                st.markdown(story_summary)
        with st.container(height=380):
            for message in story + pending_story_messages(state):
                with st.chat_message(message["role"]):
                    st.markdown(f"**{message['sender']}**: {message['content']}")
        if state.get("pending_actions"):
            st.caption("Posted actions are visible to everyone while waiting for the GM response.")
        if state.get("is_in_combat") and combatant:
            st.info(f"Active initiative: **{active_name}**")
        with st.form("ic_input_form", clear_on_submit=True):
            speaker = profile
            st.text_input("Speaking As", value=profile, disabled=True)
            action = st.text_area("IC Action / Speech", disabled=not unlocked)
            post_submitted = st.form_submit_button("Post Action to Story", disabled=not unlocked)
            pass_submitted = st.form_submit_button(
                "Pass / Ready for GM",
                disabled=not unlocked or state.get("is_in_combat"),
            )
        if not unlocked:
            st.caption(f"This input is locked until {active_name}'s turn.")
        if post_submitted and action.strip():
            if state.get("is_in_combat"):
                story.append({"role": "user", "sender": speaker, "content": action})
                facts, prose, started, ended = two_stage(speaker, action, state, characters)
                story.append({"role": "assistant", "sender": "GM (Gemini)", "content": prose})
                log_story({
                    "round": state.get("current_round", 1),
                    "character_name": speaker,
                    "action": action,
                    "mechanical_outcome": facts,
                    "narrative_prose": prose,
                })
                if state.get("is_in_combat") and not ended:
                    advance_turn(state)
                    for enemy_event in resolve_enemy_turns(state, characters):
                        story.append({
                            "role": "assistant",
                            "sender": enemy_event["character_name"],
                            "content": enemy_event["narrative_prose"],
                        })
                notify_discord_story_update(state, characters, combat_started=started)
            else:
                pending_actions = list(state.get("pending_actions", []))
                pending_actions.append({
                    "round": state.get("current_round", 1),
                    "character_name": speaker,
                    "action": action,
                })
                state["pending_actions"] = pending_actions
                submitted_by = set(state.get("exploration_submitted_by", []))
                submitted_by.add(speaker)
                state["exploration_submitted_by"] = sorted(submitted_by)
                passed_by = set(state.get("exploration_passed_by", []))
                passed_by.discard(speaker)
                state["exploration_passed_by"] = sorted(passed_by)
                if exploration_ready(state, characters):
                    story.extend(resolve_pending_actions(state, characters))
                    notify_discord_story_update(state, characters)
                else:
                    save_state(state)
            st.rerun()
        elif pass_submitted:
            submitted_by = set(state.get("exploration_submitted_by", []))
            submitted_by.add(speaker)
            state["exploration_submitted_by"] = sorted(submitted_by)
            passed_by = set(state.get("exploration_passed_by", []))
            passed_by.add(speaker)
            state["exploration_passed_by"] = sorted(passed_by)
            if exploration_ready(state, characters):
                story.extend(resolve_pending_actions(state, characters))
                notify_discord_story_update(state, characters)
            else:
                save_state(state)
            st.rerun()

    with right:
        st.markdown("### OOC")
        if "ooc_messages" not in st.session_state:
            st.session_state.ooc_messages = load_chat_messages("ooc")
        messages = st.session_state.ooc_messages
        ooc_summary = load_chat_summary("ooc")
        if ooc_summary:
            with st.expander("Earlier OOC summary"):
                st.markdown(ooc_summary)
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

# Campaign tray
with st.sidebar:
    st.title("Campaign Control")
    st.info(f"Playing as: {profile}")
    if profile == SYSTEM_TECHNICIAN:
        if db:
            st.success("Firebase Connected")
        else:
            st.error("Firebase Error: Not connected")
        if drive_service:
            st.success("Google Drive API Ready")
        else:
            st.warning("Drive API: Not available")

        with st.expander("Danger zone"):
            st.warning(
                "This permanently deletes the Story Log, OOC Log, chat summaries, "
                "and active encounter state. Character sheets and module files are preserved. "
                "This cannot be undone."
            )
            with st.form("reset_campaign_form"):
                acknowledged = st.checkbox("I understand this permanently deletes campaign history.")
                confirmation = st.text_input(f"Type {RESET_CONFIRMATION} to continue")
                reset_submitted = st.form_submit_button("Reset story permanently")

            if reset_submitted:
                if not acknowledged or confirmation.strip() != RESET_CONFIRMATION:
                    st.error(f"Check the acknowledgment and type {RESET_CONFIRMATION} exactly.")
                else:
                    reset_ok, reset_result = reset_campaign_story()
                    if reset_ok:
                        st.session_state.pop("ic_messages", None)
                        st.session_state.pop("ooc_messages", None)
                        st.session_state["reset_notice"] = (
                            "Campaign story reset. Character sheets and module files were preserved."
                        )
                        st.rerun()
                    else:
                        st.error(reset_result)

    if st.button("Change Role", use_container_width=True):
        del st.session_state.profile
        st.rerun()

reset_notice = st.session_state.pop("reset_notice", "")
if reset_notice:
    st.success(reset_notice)


st.title("Pathfinder 1e PBP GM Console")
if profile == SYSTEM_TECHNICIAN:
    tab_combat, tab_module, tab_players = st.tabs(["Combat & Turn Runner", "Module & Drive Notes", "Character Roster"])
else:
    tab_combat, tab_players = st.tabs(["Combat & Turn Runner", "Character Roster"])

with tab_combat:
    if state.get("is_in_combat"):
        st.success("Combat mode active — controlled by the AI GM")
    pending_responses(state, characters)
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
