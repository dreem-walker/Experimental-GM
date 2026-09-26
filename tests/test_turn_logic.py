from app import (
    apply_combat_directives,
    chat_event_messages,
    is_player_combatant,
    threshold_for_party_size,
    STAGE_TWO_INSTRUCTION,
    discord_story_notifications,
)


def test_exploration_threshold_table():
    expected = {1: 1, 2: 2, 3: 2, 4: 2, 5: 3, 6: 3, 7: 4, 8: 4}
    for party_size, threshold in expected.items():
        assert threshold_for_party_size(party_size) == threshold


def test_empty_party_requires_no_responses():
    assert threshold_for_party_size(0) == 0


def test_large_party_uses_strict_majority():
    assert threshold_for_party_size(9) == 5
    assert threshold_for_party_size(10) == 6


def test_combat_start_tracks_triggering_character():
    state = {"is_in_combat": False, "current_round": 1, "current_initiative_index": 0}
    facts = "COMBAT_STARTED: YES\nCOMBAT_ENDED: NO\nINITIATIVE_ORDER: [\"Goblin A\", \"Aria\", \"Goblin B\"]"

    apply_combat_directives(state, facts, "Aria")

    assert state["is_in_combat"] is True
    assert state["current_initiative_index"] == 1


def test_player_detection_leaves_enemies_for_ai():
    characters = [{"id": "aria-id", "character_name": "Aria"}]

    assert is_player_combatant("Aria", characters) is True
    assert is_player_combatant("aria-id", characters) is True
    assert is_player_combatant("Goblin A", characters) is False


def test_persisted_story_event_rehydrates_as_chat_messages():
    event = {
        "character_name": "Aria",
        "action": "I open the door.",
        "narrative_prose": "Cold air spills into the room.",
    }

    assert chat_event_messages("story", event) == [
        {"role": "user", "sender": "Aria", "content": "I open the door."},
        {"role": "assistant", "sender": "GM (Gemini)", "content": "Cold air spills into the room."},
    ]


def test_persisted_enemy_event_rehydrates_as_gm_message():
    event = {
        "character_name": "Goblin A",
        "action": "[AI enemy turn]",
        "narrative_prose": "The goblin fires an arrow.",
    }

    assert chat_event_messages("story", event) == [
        {"role": "assistant", "sender": "Goblin A", "content": "The goblin fires an arrow."},
    ]


def test_persisted_ooc_event_rehydrates_as_chat_messages():
    event = {"speaker": "Aria", "message": "What is my bonus?", "ai_response": "Check your sheet."}

    assert chat_event_messages("ooc", event) == [
        {"role": "user", "sender": "Aria", "content": "What is my bonus?"},
        {"role": "assistant", "sender": "OOC AI Assistant", "content": "Check your sheet."},
    ]


def test_stage_two_prompt_protects_pacing_and_player_agency():
    assert "Single-Beat Control" in STAGE_TWO_INSTRUCTION
    assert "Character Agency Protection" in STAGE_TWO_INSTRUCTION
    assert "Stop immediately after that single beat resolves" in STAGE_TWO_INSTRUCTION
    assert "Never invent unprompted dialogue, decisions, or actions for the player character" in STAGE_TWO_INSTRUCTION


def test_discord_notifications_skip_single_player_parties():
    characters = [{"id": "aria-id", "character_name": "Aria"}]
    state = {"is_in_combat": True, "initiative_order": ["Aria"], "current_initiative_index": 0}

    assert discord_story_notifications(state, characters) == []


def test_discord_story_update_waits_for_resolved_story_beat():
    characters = [
        {"id": "aria-id", "character_name": "Aria"},
        {"id": "borin-id", "character_name": "Borin"},
    ]
    state = {"is_in_combat": True, "initiative_order": ["Goblin A", "Aria"], "current_initiative_index": 0}

    assert discord_story_notifications(state, characters) == ["📖 Story updated."]


def test_discord_initiative_notification_names_active_player():
    characters = [
        {"id": "aria-id", "character_name": "Aria"},
        {"id": "borin-id", "character_name": "Borin"},
    ]
    state = {"is_in_combat": True, "initiative_order": ["Goblin A", "Aria"], "current_initiative_index": 1}

    assert discord_story_notifications(state, characters, combat_started=True) == [
        "📖 Story updated.",
        "⚔️ Combat started — Aria is up in initiative.",
    ]
