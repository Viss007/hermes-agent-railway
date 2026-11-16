from patches.patch_webui_sse import MARKER, OLD_ERROR, patch_messages_js

def test_patch_adds_bounded_reconnects_and_replay_cursor():
    source = (
        "  let _reconnectAttempted=false;\n"
        "  let _terminalStateReached=false;\n"
        "  let _deferredStreamRecoveryBound=false;\n"
        "    LIVE_STREAMS[activeSid]={streamId,source};\n"
        + OLD_ERROR + "\n"
    )
    patched = patch_messages_js(source)
    assert MARKER in patched
    assert "_MAX_CHAT_STREAM_RECONNECTS=8" in patched
    assert "1500*Math.pow(2,attempt-1)" in patched
    assert "_runJournalReplayParams()" in patched
    assert "source.addEventListener('open'" in patched
    assert "_reconnectAttempted" not in patched

def test_patch_is_idempotent():
    source = (
        "  let _reconnectAttempted=false;\n"
        "  let _terminalStateReached=false;\n"
        "  let _deferredStreamRecoveryBound=false;\n"
        "    LIVE_STREAMS[activeSid]={streamId,source};\n"
        + OLD_ERROR + "\n"
    )
    once = patch_messages_js(source)
    assert patch_messages_js(once) == once

def test_patch_fails_closed_when_upstream_changes():
    try:
        patch_messages_js("const unrelated = true;")
    except RuntimeError as exc:
        assert "anchor changed" in str(exc)
    else:
        raise AssertionError("unexpected upstream source must not be patched")
