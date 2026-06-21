import multiprocessing as mp
from pathlib import Path
from durin.utils.file_lock import cross_process_lock


def _worker(target_str: str, out, idx: int):
    target = Path(target_str)
    with cross_process_lock(target, timeout=10.0):
        p = Path(target_str + ".counter")
        cur = int(p.read_text()) if p.exists() else 0
        # Non-atomic RMW: only the lock makes this safe across processes.
        import time as _t; _t.sleep(0.01)
        p.write_text(str(cur + 1))
    out.put(idx)


def test_lock_serializes_across_processes(tmp_path: Path):
    target = tmp_path / "shared.json"
    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(str(target), out, i)) for i in range(8)]
    for p in procs: p.start()
    for p in procs: p.join(15)
    for p in procs: assert p.exitcode == 0
    assert int((tmp_path / "shared.json.counter").read_text()) == 8  # no lost updates
