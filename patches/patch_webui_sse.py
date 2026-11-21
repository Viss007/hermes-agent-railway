"""Build-time resilience patch for Hermes WebUI chat SSE reconnects."""
from __future__ import annotations
import os, stat, sys, tempfile
from pathlib import Path

MARKER = "RAILWAY_CHAT_SSE_RECONNECT_V2"
OLD_STATE = """  let _reconnectAttempted=false;
  let _terminalStateReached=false;
  let _deferredStreamRecoveryBound=false;
"""
NEW_STATE = r"""  // RAILWAY_CHAT_SSE_RECONNECT_V2: bounded, cursor-aware chat stream recovery.
  const _MAX_CHAT_STREAM_RECONNECTS=8;
  let _reconnectAttempts=0;
  let _reconnectTimer=null;
  let _reconnectStableTimer=null;
  let _terminalStateReached=false;
  let _deferredStreamRecoveryBound=false;

  function _scheduleChatStreamReconnect(source){
    if(_terminalStateReached||_streamFinalized||!streamId||!_isSessionCurrentPane(activeSid)) return false;
    if(_reconnectAttempts>=_MAX_CHAT_STREAM_RECONNECTS) return false;
    if(_reconnectTimer!==null){clearTimeout(_reconnectTimer);_reconnectTimer=null;}
    _reconnectAttempts+=1;
    const attempt=_reconnectAttempts;
    const delay=Math.min(1500*Math.pow(2,attempt-1),15000);
    setComposerStatus('Reconnecting… ('+attempt+'/'+_MAX_CHAT_STREAM_RECONNECTS+')');
    _reconnectTimer=setTimeout(async()=>{
      _reconnectTimer=null;
      if(_terminalStateReached||_streamFinalized||!_isSessionCurrentPane(activeSid)) return;
      try{
        const st=await api('/api/chat/stream/status?stream_id='+encodeURIComponent(streamId));
        if(st.active||st.replay_available){
          setComposerStatus(st.active?'Reconnected':'Restoring stream…');
          // Keep the last run-journal cursor on active reconnects too, avoiding
          // missing or duplicated tool/token events across the outage.
          const path='api/chat/stream?stream_id='+encodeURIComponent(streamId)+_runJournalReplayParams();
          _wireSSE(new EventSource(new URL(path,document.baseURI||location.href).href,{withCredentials:true}));
          return;
        }
      }catch(_){
        if(_deferStreamErrorIfOffline()||_deferStreamErrorIfPageHidden(source)) return;
      }
      if(await _restoreSettledSession(source)) return;
      if(_deferStreamErrorIfOffline()||_deferStreamErrorIfPageHidden(source)) return;
      if(_reconnectAttempts<_MAX_CHAT_STREAM_RECONNECTS){
        _scheduleChatStreamReconnect(source);
        return;
      }
      _handleStreamError(source);
    },delay);
    return true;
  }
"""
OLD_ERROR = r"""      // Attempt one reconnect if the stream is still active server-side
      if(!_reconnectAttempted && streamId){
        _reconnectAttempted=true;
        setComposerStatus('Reconnecting…');
        setTimeout(async()=>{
          try{
            const st=await api(`/api/chat/stream/status?stream_id=${encodeURIComponent(streamId)}`);
            if(st.active){
              setComposerStatus('Reconnected');
              _wireSSE(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}`,document.baseURI||location.href).href,{withCredentials:true}));
              return;
            }
            if(st.replay_available){
              setComposerStatus('Restoring stream…');
              _wireSSE(new EventSource(new URL(`api/chat/stream?stream_id=${encodeURIComponent(streamId)}${_runJournalReplayParams()}`,document.baseURI||location.href).href,{withCredentials:true}));
              return;
            }
          }catch(_){
            if(_deferStreamErrorIfOffline()) return;
          }
          if(await _restoreSettledSession(source)) return;
          if(_deferStreamErrorIfOffline()) return;
          if(_deferStreamErrorIfPageHidden(source)) return;
          _handleStreamError(source);
        },1500);
        return;
      }
      if(await _restoreSettledSession(source)) return;
      if(_deferStreamErrorIfOffline()) return;
      if(_deferStreamErrorIfPageHidden(source)) return;
      _handleStreamError(source);"""

def patch_messages_js(source: str) -> str:
    if MARKER in source:
        return source
    if source.count(OLD_STATE) != 1:
        raise RuntimeError("Pinned WebUI SSE state anchor changed; refusing to patch")
    if source.count(OLD_ERROR) != 1:
        raise RuntimeError("Pinned WebUI SSE error-handler anchor changed; refusing to patch")
    patched = source.replace(OLD_STATE, NEW_STATE, 1)
    patched = patched.replace(OLD_ERROR, """      if(_reconnectStableTimer!==null){clearTimeout(_reconnectStableTimer);_reconnectStableTimer=null;}
      if(_scheduleChatStreamReconnect(source)) return;
      if(await _restoreSettledSession(source)) return;
      if(_deferStreamErrorIfOffline()) return;
      if(_deferStreamErrorIfPageHidden(source)) return;
      _handleStreamError(source);""", 1)
    open_anchor = "    LIVE_STREAMS[activeSid]={streamId,source};\n"
    open_listener = r"""    LIVE_STREAMS[activeSid]={streamId,source};
    source.addEventListener('open',()=>{
      if(_reconnectStableTimer!==null) clearTimeout(_reconnectStableTimer);
      _reconnectStableTimer=setTimeout(()=>{
        _reconnectStableTimer=null;
        const live=LIVE_STREAMS[activeSid];
        if(!_terminalStateReached&&!_streamFinalized&&live&&live.source===source){
          _reconnectAttempts=0;
        }
      },20000);
    });
"""
    if patched.count(open_anchor) != 1:
        raise RuntimeError("Pinned WebUI SSE source-registration anchor changed; refusing to patch")
    patched = patched.replace(open_anchor, open_listener, 1)
    if "_reconnectAttempted" in patched:
        raise RuntimeError("Legacy single-reconnect flag remains after patch")
    return patched

INDEX_BASE_MARKER = "RAILWAY_STATIC_INDEX_BASE_FIX_V1"
INDEX_BASE_ANCHOR = "marker='/session/',i=path.indexOf(marker),p;"
INDEX_BASE_REPLACEMENT = (
    "marker='/session/',i=path.indexOf(marker),p;"
    "/* RAILWAY_STATIC_INDEX_BASE_FIX_V1: /static/index.html is an HTML entry point, "
    "not a directory; resolve its relative assets and API URLs from the app root. */"
    "if(path==='/static/index.html'||path==='/static/'){path='/';}"
)


def patch_index_html(source: str) -> str:
    if INDEX_BASE_MARKER in source:
        return source
    if source.count(INDEX_BASE_ANCHOR) != 1:
        raise RuntimeError("Pinned WebUI index base anchor changed; refusing to patch")
    return source.replace(INDEX_BASE_ANCHOR, INDEX_BASE_REPLACEMENT, 1)


def _atomic_write(path: Path, content: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    owner = path.stat()
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        try:
            os.chown(tmp_name, owner.st_uid, owner.st_gid)
        except PermissionError:
            pass
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def apply_patch(path: Path) -> bool:
    path = path.resolve(strict=True)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("WebUI messages.js must be a regular file")
    index_path = path.with_name("index.html")
    if not index_path.is_file() or index_path.is_symlink():
        raise RuntimeError("WebUI index.html must be a regular file")

    original_messages = path.read_text(encoding="utf-8")
    original_index = index_path.read_text(encoding="utf-8")
    patched_messages = patch_messages_js(original_messages)
    patched_index = patch_index_html(original_index)

    messages_changed = patched_messages != original_messages
    index_changed = patched_index != original_index
    if not messages_changed and not index_changed:
        print("[webui-patch] already applied")
        return False

    # Both outputs are written atomically so interrupted builds cannot leave a
    # half-written JavaScript or HTML file behind.
    if messages_changed:
        _atomic_write(path, patched_messages)
    if index_changed:
        _atomic_write(index_path, patched_index)

    print(
        "[webui-patch] applied: bounded SSE recovery + safe /static/index.html base"
    )
    return True

if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv)>1 else Path("/opt/hermes-webui/static/messages.js")
    apply_patch(target)
