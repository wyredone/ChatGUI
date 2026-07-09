import os
import time
import shutil
import uuid
from flask import request, jsonify, Response, send_file
from werkzeug.utils import secure_filename

from config import (SERVER_CONFIG, err_logger, conn_logger, format_size,
                    extension_allowed, file_size_ok, rate_limit_ok,
                    run_file_hooks, STATE_LOCK)

# Set by web_server so uploads can announce themselves in the chat feed.
_chat_broadcast = None

def set_chat_broadcast(func):
    """func(sender_nick, file_meta) -> emits a chat 'file message' (ITEM 18)."""
    global _chat_broadcast
    _chat_broadcast = func


def _within_base(path, base):
    return os.path.abspath(path).startswith(os.path.abspath(base))


def _safe_final_name(base_dir, raw_name):
    """ITEM 7: sanitize; also avoid clobbering existing files."""
    name = secure_filename(raw_name) or f"file_{uuid.uuid4().hex[:8]}"
    final = os.path.join(base_dir, name)
    if os.path.exists(final):
        stem, ext = os.path.splitext(name)
        final = os.path.join(base_dir, f"{stem}_{uuid.uuid4().hex[:6]}{ext}")
    return final


def handle_upload_chunk():
    # Top-level guard: never let an unexpected error surface as a bare 500.
    try:
        return _handle_upload_chunk_impl()
    except Exception as e:
        import traceback
        err_logger.error(f"upload_chunk fatal: {e}\n{traceback.format_exc()}")
        return jsonify({"error": "server_error", "detail": str(e)}), 500


def _handle_upload_chunk_impl():
    ip = request.remote_addr

    with STATE_LOCK:
        if ip in SERVER_CONFIG["blocked_ips"]:
            return jsonify({"error": "blocked"}), 403

    # ITEM 3: rate limit uploads per IP
    if not rate_limit_ok(ip, "upload"):
        return jsonify({"error": "rate_limited"}), 429

    # Resumable.js sends its params in the QUERY STRING (and sometimes the form).
    # request.values merges args + form, so this works either way.
    params = request.values
    try:
        total_size = int(params.get('resumableTotalSize', 0) or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "bad_request"}), 400

    resumable_filename = params.get('resumableFilename', '')
    sender = (params.get('sender') or 'Anonymous')[:40]

    # ITEM 8: extension + size validation up front (fail on first chunk)
    if not extension_allowed(resumable_filename):
        return jsonify({"error": "extension_not_allowed"}), 415
    if not file_size_ok(total_size):
        return jsonify({"error": "file_too_large",
                        "max_mb": SERVER_CONFIG["max_file_size_mb"]}), 413

    base_dir = os.path.abspath(SERVER_CONFIG["shared_dir"])

    # Disk-space guard
    try:
        _, _, free = shutil.disk_usage(base_dir)
        if (free - total_size) / (1024 ** 3) < SERVER_CONFIG["min_disk_space_gb"]:
            return jsonify({"error": "insufficient_disk"}), 507
    except Exception:
        pass

    try:
        chunk_number = int(params.get('resumableChunkNumber'))
        total_chunks = int(params.get('resumableTotalChunks'))
        identifier = secure_filename(params.get('resumableIdentifier', '')) or uuid.uuid4().hex
    except (TypeError, ValueError):
        return jsonify({"error": "bad_request"}), 400

    os.makedirs(SERVER_CONFIG["upload_tmp_dir"], exist_ok=True)
    chunk_dir = os.path.join(SERVER_CONFIG["upload_tmp_dir"], identifier)
    os.makedirs(chunk_dir, exist_ok=True)

    if 'file' not in request.files:
        return jsonify({"error": "no_file"}), 400
    request.files['file'].save(os.path.join(chunk_dir, f"part_{chunk_number}"))

    # ITEM 10: correct completion check under simultaneous uploads.
    # Verify EACH expected part exists (don't trust listdir count), then
    # assemble to a temp file and atomically rename into place.
    all_present = all(
        os.path.exists(os.path.join(chunk_dir, f"part_{i}"))
        for i in range(1, total_chunks + 1)
    )
    if not all_present:
        return jsonify({"status": "chunk_saved"})

    final_path = _safe_final_name(base_dir, resumable_filename)
    tmp_assembled = final_path + f".part-{uuid.uuid4().hex[:6]}"
    try:
        with open(tmp_assembled, 'wb') as target:
            for i in range(1, total_chunks + 1):
                part = os.path.join(chunk_dir, f"part_{i}")
                with open(part, 'rb') as sf:
                    shutil.copyfileobj(sf, target)
        os.replace(tmp_assembled, final_path)  # atomic on same volume
    except Exception as e:
        err_logger.error(f"Assembly failed for {resumable_filename}: {e}")
        if os.path.exists(tmp_assembled):
            try:
                os.remove(tmp_assembled)
            except Exception:
                pass
        return jsonify({"error": "assembly_failed"}), 500
    finally:
        shutil.rmtree(chunk_dir, ignore_errors=True)

    size = os.path.getsize(final_path)
    meta = {
        "type": "file",
        "name": os.path.basename(final_path),
        "size": size,
        "size_str": format_size(size),
        "url": f"/download/{os.path.basename(final_path)}",
        "sender": sender,
        "ts": time.strftime('%H:%M:%S'),
    }
    conn_logger.info(f"File received: {meta['name']} ({meta['size_str']}) from {sender}@{ip}")

    # ITEM 24: optional processing hooks (nothing imported unless registered)
    run_file_hooks(final_path, meta)

    # ITEM 18: announce as a chat file-message
    if _chat_broadcast:
        _chat_broadcast(sender, meta)

    return jsonify({"status": "complete", "file": meta})


def handle_download(filename):
    ip = request.remote_addr
    with STATE_LOCK:
        if ip in SERVER_CONFIG["blocked_ips"]:
            return "Access Denied.", 403

    safe = secure_filename(filename)
    base_abs = os.path.abspath(SERVER_CONFIG["shared_dir"])
    file_path = os.path.abspath(os.path.join(base_abs, safe))

    if not _within_base(file_path, base_abs) or not os.path.isfile(file_path):
        return "Not found", 404

    file_size = os.path.getsize(file_path)
    range_header = request.headers.get('Range', None)

    if not range_header:
        return send_file(file_path, as_attachment=True,
                         download_name=os.path.basename(file_path))

    try:
        parts = range_header.replace('bytes=', '').split('-')
        start = int(parts[0])
        end = int(parts[1]) if parts[1] else file_size - 1
    except (ValueError, IndexError):
        return "Invalid range", 400

    start = max(0, start)
    end = min(end, file_size - 1)
    chunk_length = end - start + 1

    def generate_partial():
        with open(file_path, 'rb') as f:
            f.seek(start)
            remaining = chunk_length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)

    return Response(generate_partial(), 206, headers={
        'Content-Type': 'application/octet-stream',
        'Content-Disposition': f'attachment; filename="{os.path.basename(file_path)}"',
        'Content-Range': f'bytes {start}-{end}/{file_size}',
        'Content-Length': str(chunk_length),
        'Accept-Ranges': 'bytes'
    })


def sweep_orphaned_chunks(max_age_sec=3600):
    """ITEM 11 (bundled): remove abandoned chunk dirs older than max_age."""
    tmp = SERVER_CONFIG["upload_tmp_dir"]
    if not os.path.isdir(tmp):
        return
    now = time.time()
    for entry in os.listdir(tmp):
        p = os.path.join(tmp, entry)
        try:
            if os.path.isdir(p) and (now - os.path.getmtime(p)) > max_age_sec:
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
