import threading

from dulwich.repo import Repo

from durin.utils.gitstore import GitStore, _repo_write_lock


def test_repo_write_lock_is_reentrant_and_per_repo(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    la1 = _repo_write_lock(a)
    la2 = _repo_write_lock(a)
    lb = _repo_write_lock(b)
    assert la1 is la2          # same repo → same lock (cached)
    assert la1 is not lb        # different repo → different lock
    # reentrant: acquiring twice on one thread must not deadlock
    with la1:
        with la1:
            assert True


def test_concurrent_auto_commit_all_land(tmp_path):
    store = GitStore(tmp_path, subtree=True, label="test")
    store.init()
    n = 8
    errors = []

    def worker(i):
        try:
            (tmp_path / f"f{i}.txt").write_text(f"content {i}", encoding="utf-8")
            store.auto_commit(f"add f{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    for i in range(n):
        assert (tmp_path / f"f{i}.txt").exists()
    # HEAD is valid (repo not corrupted) and there is at least one commit.
    with Repo(str(tmp_path)) as repo:
        assert repo.head()
