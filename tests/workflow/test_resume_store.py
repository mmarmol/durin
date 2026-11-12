"""What a step receives is kept whole in files that belong to the run, beside its
working folder: the input of the step the record names, and the outputs of the
steps others read by name."""
from durin.workflow import resume_store
from durin.workflow.artifacts import keyed_work_dir


def test_an_input_is_kept_whole_and_the_older_ones_are_dropped(tmp_path):
    d = resume_store.store_dir(tmp_path, "r1")
    big = "x" * 500_000
    resume_store.save_input(d, 1, "a", None)
    resume_store.save_input(d, 2, "b", big)
    resume_store.save_input(d, 3, "c", "small")
    assert resume_store.load_input(d, 3) == ("c", "small")
    assert resume_store.load_input(d, 2) == ("b", big)     # the one before stays
    assert resume_store.load_input(d, 1) is None


def test_sources_are_kept_whole(tmp_path):
    d = resume_store.store_dir(tmp_path, "r1")
    assert resume_store.load_sources(d) == {}
    resume_store.save_source(d, "plan", "p" * 100_000)
    resume_store.save_source(d, "ids", '["a"]')
    resume_store.save_source(d, "plan", "second")
    assert resume_store.load_sources(d) == {"plan": "second", "ids": '["a"]'}


def test_an_empty_output_replaces_an_earlier_one(tmp_path):
    d = resume_store.store_dir(tmp_path, "r1")
    resume_store.save_source(d, "items", "one\ntwo")
    resume_store.save_source(d, "items", "")
    assert resume_store.load_sources(d) == {"items": ""}


def test_a_run_without_a_key_keeps_its_files_in_its_own_folder(tmp_path):
    assert resume_store.store_dir(tmp_path, "r1") == tmp_path / ".workflow" / "r1" / "resume"


def test_a_keyed_run_keeps_its_files_beside_the_keyed_folder(tmp_path):
    work = keyed_work_dir(tmp_path, "w", "k1")
    d = resume_store.store_dir(tmp_path, "r1", work_dir=str(work), keyed=True)
    assert d == work.parent / "resume" / "r1"


def test_a_reader_finds_the_folder_from_the_record(tmp_path):
    work = keyed_work_dir(tmp_path, "w", "k1")
    keyed = {"run_id": "r1", "work_dir": str(work), "work_key": "k1"}
    plain = {"run_id": "r2", "work_dir": str(tmp_path / ".workflow" / "r2" / "work"), "work_key": None}
    assert resume_store.store_dir_of(tmp_path, keyed) == work.parent / "resume" / "r1"
    assert resume_store.store_dir_of(tmp_path, plain) == tmp_path / ".workflow" / "r2" / "resume"


def test_discarding_removes_the_folder_and_can_be_repeated(tmp_path):
    d = resume_store.store_dir(tmp_path, "r1")
    resume_store.save_input(d, 1, "a", None)
    resume_store.discard(d)
    resume_store.discard(d)
    assert not d.exists()
