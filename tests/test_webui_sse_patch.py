from patches.patch_webui_sse import MARKER, OLD_ERROR, patch_index_html, patch_messages_js

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



def test_index_base_patch_fixes_direct_static_entrypoint():
    source = (
        "<script>(function(){var path=location.pathname,marker='/session/',i=path.indexOf(marker),p;"
        "i>=0?p=(path.slice(0,i+1)||'/'):p=(path.endsWith('/')?path:(path.replace(/\\/[^\\/]*$/,'/')||'/'));"
        "document.write('<base href="'+location.origin+p+'">');})()</script>"
    )
    patched = patch_index_html(source)
    assert "RAILWAY_STATIC_INDEX_BASE_FIX_V1" in patched
    assert "path==='/static/index.html'||path==='/static/'" in patched
    assert patch_index_html(patched) == patched


def test_index_base_patch_fails_closed_when_upstream_changes():
    try:
        patch_index_html("<html><head></head></html>")
    except RuntimeError as exc:
        assert "index base anchor changed" in str(exc)
    else:
        raise AssertionError("unexpected index source must not be patched")
