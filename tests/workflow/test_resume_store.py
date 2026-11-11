"""What a step receives is kept whole in files that belong to the run, beside its
working folder: the input of the step the record names, and the outputs of the
steps others read by name."""
from durin.workflow import resume_store


def test_an_input_is_kept_whole_and_the_older_ones_are_dropped(tmp_path):
    big = "x" * 500_000
    resume_store.save_input(tmp_path, "r1", 1, "a", None)
    resume_store.save_input(tmp_path, "r1", 2, "b", big)
    resume_store.save_input(tmp_path, "r1", 3, "c", "small")
    assert resume_store.load_input(tmp_path, "r1", 3) == ("c", "small")
    assert resume_store.load_input(tmp_path, "r1", 2) == ("b", big)     # the one before stays
    assert resume_store.load_input(tmp_path, "r1", 1) is None


def test_sources_are_kept_whole(tmp_path):
    assert resume_store.load_sources(tmp_path, "r1") == {}
    resume_store.save_source(tmp_path, "r1", "plan", "p" * 100_000)
    resume_store.save_source(tmp_path, "r1", "ids", '["a"]')
    resume_store.save_source(tmp_path, "r1", "plan", "second")
    assert resume_store.load_sources(tmp_path, "r1") == {"plan": "second", "ids": '["a"]'}
