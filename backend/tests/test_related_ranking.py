from datetime import datetime, timezone

from related import rank_related


def post(id, text, tags=(), **fields):
    return {"id": id, "description": text, "tags": list(tags),
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc), **fields}


def test_combined_match_outranks_tags_alone():
    source = post(1, "Proxy timeout\nUpstream connection reset", ["networking"])
    matches = rank_related(source, [post(2, "CSS layout\nGrid columns", ["networking"]),
                                   post(3, "Proxy timeout\nUpstream connection reset persists", ["networking"])])
    assert [m["id"] for m in matches] == [3, 2]
    assert matches[0]["shared_keywords"] == ["connection", "proxy", "reset", "timeout", "upstream"]


def test_keyword_only_match_can_cross_tags_and_case():
    source = post(1, "PROXY timeout\nUpstream reset", ["python"])
    matches = rank_related(source, [post(2, "proxy timeout\nupstream reset persists", ["node"])])
    assert [m["id"] for m in matches] == [2]
    assert matches[0]["shared_tags"] == []


def test_boilerplate_and_single_incidental_word_do_not_link_posts():
    source = post(1, "[Demo] Proxy timeout\nIllustrative sample: votes and outcomes are synthetic, not observed agent activity.")
    assert rank_related(source, [post(2, "[Demo] CAD extrusion\nIllustrative sample: votes and outcomes are synthetic, not observed agent activity."),
                                 post(3, "Proxy layout\nBrowser fonts dimensions wrapping")]) == []


def test_current_fields_take_precedence_and_solutions_do_not_match():
    source = post(1, "Legacy proxy timeout", problem="CAD extrusion", general_description="Sketch geometry")
    assert rank_related(source, [post(2, "Proxy timeout", solution="CAD extrusion sketch geometry")]) == []


def test_caps_three_excludes_self_and_ties_by_recency_then_id():
    source = post(1, "Proxy timeout", ["networking"])
    candidates = [source] + [post(i, f"Proxy timeout\nConfiguration {i}", ["networking"]) for i in range(2, 7)]
    candidates[1]["created_at"] = datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert [m["id"] for m in rank_related(source, candidates)] == [2, 6, 5]


def test_no_keywords_or_tags_has_no_recommendations():
    assert rank_related(post(1, "the problem"), [post(2, "a question")]) == []


def test_excludes_copies_of_current_question_with_different_ids():
    source = post(1, "Spreadsheet totals change\nRegional date conventions differ", ["spreadsheets"])
    duplicate = post(2, "spreadsheet totals change\n\nRegional  date conventions differ", ["spreadsheets"])
    structured_duplicate = post(3, "Old format", ["spreadsheets"],
        problem="Spreadsheet totals change", general_description="Regional date conventions differ")
    related = post(4, "Spreadsheet totals change\nCurrency conversion changes the totals", ["spreadsheets"])
    assert [item["id"] for item in rank_related(source, [source, duplicate, structured_duplicate, related])] == [4]
