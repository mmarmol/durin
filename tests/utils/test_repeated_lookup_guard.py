"""The repeated-lookup guard counts list-form web calls per item."""

from durin.utils.runtime import repeated_external_lookup_error

_BUDGET = 2  # lookups of one target allowed before the next is blocked


def _call(name, arguments, counts):
    return repeated_external_lookup_error(name, arguments, counts)


def _first_blocked_attempt(name, make_args):
    counts: dict[str, int] = {}
    for attempt in range(1, 10):
        if _call(name, make_args(), counts):
            return attempt
    return None


def test_single_and_list_form_hit_the_guard_at_the_same_count():
    url = "https://example.com/a"
    single = _first_blocked_attempt("web_fetch", lambda: {"url": url})
    listed = _first_blocked_attempt("web_fetch", lambda: {"urls": [url]})
    assert single == _BUDGET + 1
    assert listed == single

    query = "acme pricing"
    single_q = _first_blocked_attempt("web_search", lambda: {"query": query})
    listed_q = _first_blocked_attempt("web_search", lambda: {"queries": [query]})
    assert single_q == _BUDGET + 1
    assert listed_q == single_q


def test_mixed_single_and_list_repeats_accumulate_together():
    counts: dict[str, int] = {}
    url = "https://example.com/a"
    assert _call("web_fetch", {"url": url}, counts) is None
    assert _call("web_fetch", {"urls": [url]}, counts) is None
    assert _call("web_fetch", {"urls": [url, "https://example.com/b"]}, counts) is None
    # Third sighting of `a` but `b` is fresh: still goes through.
    assert _call("web_fetch", {"url": url}, counts) is not None


def test_list_with_a_fresh_item_is_not_blocked_but_all_seen_list_is():
    counts: dict[str, int] = {}
    a, b = "https://example.com/a", "https://example.com/b"
    for _ in range(_BUDGET):
        assert _call("web_fetch", {"urls": [a, b]}, counts) is None
    assert _call("web_fetch", {"urls": [a, b]}, counts) is not None
    assert _call("web_fetch", {"urls": [a, "https://example.com/new"]}, counts) is None


def test_list_item_order_does_not_matter():
    for order in (["a", "b"], ["b", "a"]):
        counts: dict[str, int] = {}
        for _ in range(_BUDGET):
            _call("web_search", {"queries": ["a", "b"]}, counts)
        assert _call("web_search", {"queries": order}, counts) is not None
        assert _call("web_search", {"queries": [order[0], "fresh"]}, counts) is None


def test_list_items_are_normalised_like_the_single_form():
    counts: dict[str, int] = {}
    for variant in ("Acme Docs", "  acme docs ", "ACME DOCS"):
        _call("web_search", {"queries": [variant]}, counts)
    assert _call("web_search", {"query": "acme docs"}, counts) is not None


def test_all_fresh_list_is_never_blocked():
    counts: dict[str, int] = {}
    for i in range(10):
        urls = [f"https://example.com/{i}-{j}" for j in range(3)]
        assert _call("web_fetch", {"urls": urls}, counts) is None


def test_duplicate_items_inside_one_call_count_once():
    counts: dict[str, int] = {}
    url = "https://example.com/a"
    assert _call("web_fetch", {"urls": [url, url, url, url]}, counts) is None
    assert _call("web_fetch", {"url": url}, counts) is None
    assert _call("web_fetch", {"url": url}, counts) is not None


def test_single_form_and_untracked_tools_unchanged():
    counts: dict[str, int] = {}
    assert _call("read_file", {"urls": ["x"] * 9}, counts) is None
    assert _call("web_search", {"search_term": "x"}, counts) is None
    assert counts == {"web_search:x": 1}
    assert _call("web_fetch", {}, counts) is None
    assert _call("web_fetch", {"urls": []}, counts) is None
