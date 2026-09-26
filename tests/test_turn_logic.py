from app import threshold_for_party_size


def test_exploration_threshold_table():
    expected = {1: 1, 2: 2, 3: 2, 4: 2, 5: 3, 6: 3, 7: 4, 8: 4}
    for party_size, threshold in expected.items():
        assert threshold_for_party_size(party_size) == threshold


def test_empty_party_requires_no_responses():
    assert threshold_for_party_size(0) == 0


def test_large_party_uses_strict_majority():
    assert threshold_for_party_size(9) == 5
    assert threshold_for_party_size(10) == 6
