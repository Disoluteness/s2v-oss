from packages.storage_kit import LocalObjectStorage


def test_local_object_storage_puts_reads_and_signs_links(tmp_path) -> None:
    storage = LocalObjectStorage(
        root=tmp_path,
        bucket="script2video",
        link_secret="test-secret",
    )

    stored = storage.put_bytes(
        object_key="runs/run_1/asset_1/demo.txt",
        content=b"hello asset",
        mime_type="text/plain",
    )
    link = storage.temporary_url(
        asset_id="asset_1",
        storage_url=stored.storage_url,
        expires_in_seconds=60,
    )
    expires = int(link.url.split("expires=")[1].split("&")[0])
    signature = link.url.split("signature=")[1]

    assert stored.storage_url == "local://script2video/runs/run_1/asset_1/demo.txt"
    assert stored.file_size == len(b"hello asset")
    assert storage.read_bytes(stored.storage_url) == b"hello asset"
    assert storage.verify(
        asset_id="asset_1",
        storage_url=stored.storage_url,
        expires=expires,
        signature=signature,
    )
    assert not storage.verify(
        asset_id="asset_1",
        storage_url=stored.storage_url,
        expires=0,
        signature=signature,
    )
