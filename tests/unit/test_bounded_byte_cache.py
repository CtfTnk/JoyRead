"""Tests for BoundedByteCache and NamespacedPageCache."""

from __future__ import annotations

from threading import Thread

from joyread.core.services.cache_service import (
    BoundedByteCache,
    NamespacedPageCache,
    SharedThumbnailCache,
    ThumbnailCacheKey,
    ThumbnailSourceIdentity,
)


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


def test_shared_thumbnail_cache_hits_refresh_the_global_lru() -> None:
    cache = SharedThumbnailCache(max_bytes=6)
    client = cache.issue_client("detail")
    first = ThumbnailCacheKey("book", 0, 100, 142)
    second = ThumbnailCacheKey("book", 1, 100, 142)
    third = ThumbnailCacheKey("book", 2, 100, 142)

    client.put(first, b"aaa")
    client.put(second, b"bbb")
    assert client.get(first) == b"aaa"
    client.put(third, b"ccc")

    assert client.get(first) == b"aaa"
    assert client.get(second) is None
    assert client.get(third) == b"ccc"
    assert cache.current_bytes == 6


def test_shared_thumbnail_cache_allows_pinned_overage_then_shrinks_on_release() -> None:
    cache = SharedThumbnailCache(max_bytes=4)
    detail = cache.issue_client("detail")
    reader = cache.issue_client("reader")
    detail_key = ThumbnailCacheKey("book", 0, 100, 142)
    reader_key = ThumbnailCacheKey("book", 1, 100, 142)

    detail.set_pins(frozenset({detail_key}))
    reader.set_pins(frozenset({reader_key}))
    detail.put(detail_key, b"aaaa")
    reader.put(reader_key, b"bbbb")

    assert cache.current_bytes == 8
    assert detail.get(detail_key) == b"aaaa"
    assert reader.get(reader_key) == b"bbbb"

    detail.release()

    assert cache.current_bytes == 4
    assert detail.get(detail_key) is None
    assert reader.get(reader_key) == b"bbbb"


def test_thumbnail_source_identity_separates_limits_and_sensitive_sessions() -> None:
    shared = ThumbnailSourceIdentity("file:42", "limits-a").cache_id
    other_limits = ThumbnailSourceIdentity("file:42", "limits-b").cache_id
    sensitive = ThumbnailSourceIdentity("file:42", "limits-a", "session-auth-7").cache_id

    assert shared != other_limits
    assert shared != sensitive
    assert ThumbnailSourceIdentity("file:42", "limits-a").cache_id == shared


def test_shared_thumbnail_cache_promotes_payloads_and_viewport_pins() -> None:
    cache = SharedThumbnailCache(max_bytes=8)
    reader = cache.issue_client("reader")
    old = ThumbnailCacheKey("session:old", 2, 100, 142)
    target = ThumbnailCacheKey("external:sha256:abc", 2, 100, 142)
    reader.set_pins(frozenset({old}))
    reader.put(old, b"page")

    reader.promote_source(old.source_id, target.source_id)

    assert reader.get(old) is None
    assert reader.get(target) == b"page"
    assert reader.pins == frozenset({target})
