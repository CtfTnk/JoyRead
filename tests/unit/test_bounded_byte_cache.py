"""Tests for BoundedByteCache and NamespacedPageCache."""

from __future__ import annotations

from threading import Thread

from joyread.core.services.cache_service import BoundedByteCache, NamespacedPageCache


def _payload(size: int, marker: int = 0) -> bytes:
    return bytes([marker % 256]) * size


def test_bounded_byte_cache_evicts_least_recently_used_until_under_budget() -> None:
    cache: BoundedByteCache[str, bytes] = BoundedByteCache(max_bytes=300)

    cache.put("a", _payload(100, 1))
    cache.put("b", _payload(100, 2))
    cache.put("c", _payload(100, 3))
    # Touch "a" so "b" becomes the LRU candidate.
    assert cache.get("a") == _payload(100, 1)
    cache.put("d", _payload(100, 4))

    assert cache.get("b") is None
    assert cache.get("a") == _payload(100, 1)
    assert cache.get("c") == _payload(100, 3)
    assert cache.get("d") == _payload(100, 4)
    assert cache.current_bytes == 300


def test_bounded_byte_cache_keeps_oversized_entry_just_inserted() -> None:
    cache: BoundedByteCache[str, bytes] = BoundedByteCache(max_bytes=10)

    cache.put("huge", _payload(50, 9))

    # Oversized values are accepted (the caller asked for it) and become the
    # only entry; the next put will evict them normally.
    assert cache.get("huge") == _payload(50, 9)
    cache.put("small", _payload(5, 1))
    assert cache.get("huge") is None
    assert cache.get("small") == _payload(5, 1)


def test_bounded_byte_cache_resize_shrinks_and_expands() -> None:
    cache: BoundedByteCache[str, bytes] = BoundedByteCache(max_bytes=500)
    for i in range(5):
        cache.put(f"k{i}", _payload(100, i))

    cache.resize(250)
    assert cache.current_bytes <= 250
    assert cache.get("k0") is None  # oldest evicted first
    assert cache.get("k4") is not None

    cache.resize(1024)
    # Existing entries stay; new puts can grow back without immediate eviction.
    before = cache.current_bytes
    cache.put("new", _payload(200, 9))
    assert cache.current_bytes == before + 200


def test_bounded_byte_cache_clear_empties_entries_and_bytes() -> None:
    cache: BoundedByteCache[str, bytes] = BoundedByteCache(max_bytes=1024)
    cache.put("a", _payload(100, 1))
    cache.put("b", _payload(100, 2))

    cache.clear()

    assert cache.get("a") is None
    assert cache.get("b") is None
    assert cache.current_bytes == 0


def test_bounded_byte_cache_purge_drops_only_matching_keys() -> None:
    cache: BoundedByteCache[tuple[str, int], bytes] = BoundedByteCache(max_bytes=1024)
    cache.put(("A", 0), _payload(100, 1))
    cache.put(("A", 1), _payload(100, 2))
    cache.put(("B", 0), _payload(100, 3))

    removed = cache.purge(lambda key: key[0] == "A")

    assert removed == 2
    assert cache.get(("A", 0)) is None
    assert cache.get(("A", 1)) is None
    assert cache.get(("B", 0)) == _payload(100, 3)
    assert cache.current_bytes == 100


def test_bounded_byte_cache_is_thread_safe_under_concurrent_writes() -> None:
    cache: BoundedByteCache[int, bytes] = BoundedByteCache(max_bytes=4096)

    def worker(base: int) -> None:
        for i in range(64):
            cache.put(base * 1000 + i, _payload(8, base))
            cache.get(base * 1000 + i)

    threads = [Thread(target=worker, args=(t,)) for t in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # The cache must respect its budget regardless of interleavings.
    assert cache.current_bytes <= 4096


def test_namespaced_page_cache_isolates_sessions_and_clears_per_session() -> None:
    backing: BoundedByteCache[tuple[str, int], bytes] = BoundedByteCache(max_bytes=4096)
    session_a = NamespacedPageCache(backing, session_id="A")
    session_b = NamespacedPageCache(backing, session_id="B")

    session_a.put(0, _payload(100, 1))
    session_a.put(1, _payload(100, 2))
    session_b.put(0, _payload(100, 9))

    assert session_a.get(0) == _payload(100, 1)
    # Different sessions cannot read each other's namespaced entries.
    assert session_b.get(1) is None
    assert session_b.get(0) == _payload(100, 9)

    removed = session_a.clear()

    assert removed == 2
    assert session_a.get(0) is None
    # Closing one session only frees its own bytes; B is untouched.
    assert session_b.get(0) == _payload(100, 9)
    assert backing.current_bytes == 100


def test_namespaced_page_cache_respects_shared_byte_budget_across_sessions() -> None:
    backing: BoundedByteCache[tuple[str, int], bytes] = BoundedByteCache(max_bytes=200)
    session_a = NamespacedPageCache(backing, session_id="A")
    session_b = NamespacedPageCache(backing, session_id="B")

    session_a.put(0, _payload(100, 1))
    session_a.put(1, _payload(100, 2))
    # A second reader joining should not be able to push total memory past the
    # shared budget; eviction comes from the LRU end (oldest A page).
    session_b.put(0, _payload(100, 9))

    assert backing.current_bytes <= 200
    assert session_a.get(0) is None  # the oldest entry was evicted
    assert session_a.get(1) == _payload(100, 2)
    assert session_b.get(0) == _payload(100, 9)
