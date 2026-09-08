from durin.config.schema import MemoryConfig


def test_memory_library_defaults():
    lib = MemoryConfig().library
    assert lib.awareness_max_docs == 20
    assert lib.awareness_abstracts is False
