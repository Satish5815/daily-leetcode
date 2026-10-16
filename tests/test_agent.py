
import json
from agent import select_problems


def test_select_problems_avoids_history_and_selects_mixed():
    problems = [
        {"questionFrontendId": "1", "title": "A", "titleSlug": "a", "difficulty": "EASY"},
        {"questionFrontendId": "2", "title": "B", "titleSlug": "b", "difficulty": "MEDIUM"},
        {"questionFrontendId": "3", "title": "C", "titleSlug": "c", "difficulty": "HARD"},
        {"questionFrontendId": "4", "title": "D", "titleSlug": "d", "difficulty": "EASY"},
    ]
    history = {"completed": ["1"], "attempted": []}
    selected = select_problems(problems, history)
    ids = {p["questionFrontendId"] for p in selected}
    assert len(selected) == 3
    assert "1" not in ids
    assert ids == {"2", "3", "4"}
