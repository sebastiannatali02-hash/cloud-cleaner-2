from datetime import datetime, timedelta, timezone

import pytest

from cloudcleaner.dedup import (
    DuplicateGroup,
    choose_redundant,
    find_duplicates,
    total_reclaimable,
)
from cloudcleaner.models import StorageObject

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


def obj(key, size=1024, etag=None, age_days=0):
    return StorageObject(
        key=key,
        size_bytes=size,
        last_modified=NOW - timedelta(days=age_days),
        storage_class="STANDARD",
        etag=etag,
    )


def test_finds_group_with_matching_etag_and_size():
    objects = [
        obj("a", size=1024, etag='"abc"'),
        obj("b", size=1024, etag='"abc"'),
        obj("c", size=1024, etag='"different"'),
    ]
    groups = find_duplicates(objects)
    assert len(groups) == 1
    g = groups[0]
    assert g.etag == "abc"
    assert g.size_bytes == 1024
    assert {m.key for m in g.members} == {"a", "b"}


def test_same_etag_different_size_not_grouped():
    objects = [
        obj("a", size=1024, etag='"abc"'),
        obj("b", size=2048, etag='"abc"'),
    ]
    assert find_duplicates(objects) == []


def test_skips_multipart_etag():
    objects = [
        obj("a", size=1024, etag='"abc-2"'),
        obj("b", size=1024, etag='"abc-2"'),
    ]
    assert find_duplicates(objects) == []


def test_skips_missing_etag():
    objects = [
        obj("a", size=1024, etag=None),
        obj("b", size=1024, etag=None),
        obj("c", size=1024, etag=""),
    ]
    assert find_duplicates(objects) == []


def test_singletons_not_returned():
    objects = [
        obj("a", size=1024, etag='"abc"'),
        obj("b", size=2048, etag='"xyz"'),
    ]
    assert find_duplicates(objects) == []


def test_respects_min_size():
    objects = [
        obj("small1", size=100, etag='"s"'),
        obj("small2", size=100, etag='"s"'),
        obj("big1", size=5000, etag='"b"'),
        obj("big2", size=5000, etag='"b"'),
    ]
    groups = find_duplicates(objects, min_size=1000)
    assert len(groups) == 1
    assert groups[0].etag == "b"


def test_redundant_bytes():
    g = DuplicateGroup(
        etag="abc",
        size_bytes=1024,
        members=(obj("a", etag='"abc"'), obj("b", etag='"abc"'), obj("c", etag='"abc"')),
    )
    assert g.count == 3
    # keep one, reclaim the other two
    assert g.redundant_bytes == 1024 * 2


def test_choose_redundant_keeps_oldest():
    old = obj("old", size=1024, etag='"abc"', age_days=10)
    mid = obj("mid", size=1024, etag='"abc"', age_days=5)
    new = obj("new", size=1024, etag='"abc"', age_days=1)
    g = find_duplicates([new, old, mid])[0]

    redundant = choose_redundant(g, keep="oldest")
    assert len(redundant) == g.count - 1
    kept = set(g.members) - set(redundant)
    assert len(kept) == 1
    assert next(iter(kept)).key == "old"


def test_choose_redundant_keeps_newest():
    old = obj("old", size=1024, etag='"abc"', age_days=10)
    new = obj("new", size=1024, etag='"abc"', age_days=1)
    g = find_duplicates([old, new])[0]

    redundant = choose_redundant(g, keep="newest")
    assert len(redundant) == 1
    assert redundant[0].key == "old"


def test_choose_redundant_invalid_keep():
    g = DuplicateGroup(etag="abc", size_bytes=1024, members=(obj("a"), obj("b")))
    with pytest.raises(ValueError):
        choose_redundant(g, keep="middle")


def test_total_reclaimable_sums():
    objects = [
        obj("a", size=1000, etag='"x"'),
        obj("b", size=1000, etag='"x"'),  # group x: 1 redundant -> 1000
        obj("c", size=2000, etag='"y"'),
        obj("d", size=2000, etag='"y"'),
        obj("e", size=2000, etag='"y"'),  # group y: 2 redundant -> 4000
    ]
    groups = find_duplicates(objects)
    assert total_reclaimable(groups) == 1000 + 4000
