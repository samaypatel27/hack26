from matching import MATCH_THRESHOLD, best_match, jaccard, score, words


def post(id: int, forum: str, description: str, tags: list[str]) -> dict:
    return {"id": id, "forum": forum, "description": description, "tags": tags}


POSTS = [
    post(1, "docker", "Docker Compose can't reach Postgres on localhost\nconnection refused 127.0.0.1:5432", ["docker", "postgres"]),
    post(2, "postgres", "Postgres connection refused on localhost\nconnection refused 127.0.0.1:5432", ["postgres"]),
    post(3, "prisma", "Prisma could not locate the Query Engine in Docker", ["prisma", "docker"]),
]


def test_words_drops_stopwords_and_punctuation():
    assert words("The engine is NOT found!") == {"engine", "found"}


def test_jaccard_edges():
    assert jaccard(set(), {"a"}) == 0.0
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_tags_add_to_word_score():
    p = POSTS[0]
    q = "connection refused localhost postgres"
    assert score(q, p, ["docker"]) > 0.6 * score(q, p)


def test_forum_is_the_first_filter():
    q = "connection refused localhost postgres 127.0.0.1:5432"
    # Without a forum, either near-identical post could win.
    assert best_match(q, POSTS) is not None
    # With a forum, posts in other forums are never compared.
    assert best_match(q, POSTS, forum="postgres")["id"] == 2
    assert best_match(q, POSTS, forum="docker")["id"] == 1
    assert best_match(q, POSTS, forum="prisma") is None


def test_threshold_rejects_weak_matches():
    assert best_match("completely unrelated words here", POSTS) is None
    assert MATCH_THRESHOLD == 0.3
