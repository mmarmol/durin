"""Who owns a run: a process, and one particular start of it."""
import os

from durin.utils import process_tree
from durin.utils.process_tree import process_alive, process_identity


def test_an_identity_names_this_start_of_the_process():
    me = process_identity()
    assert me["pid"] == os.getpid() and me["boot"] == process_tree.BOOT_ID
    assert process_alive(me) is True


def test_the_same_pid_from_another_start_is_dead():
    earlier = {**process_identity(), "boot": "an-earlier-start"}
    assert process_alive(earlier) is False


def test_an_owner_recorded_before_boot_ids_is_judged_as_before():
    legacy = {k: v for k, v in process_identity().items() if k != "boot"}
    assert process_alive(legacy) is True
    assert process_alive({"pid": 2**22 + 4242, "started": "never"}) is False
