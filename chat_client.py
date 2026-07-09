# Embedded mobile browser client (chat + file sharing). Served inline by web_server.
CHAT_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>LAN Chat</title>
    <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/resumable.js/1.1.0/resumable.min.js"></script>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <style>
        html, body { height: 100%; }
        #feed { -webkit-overflow-scrolling: touch; }
        .bubble { max-width: 78%; word-break: break-word; }
    </style>
</head>
<body class="bg-gray-900 text-gray-100 font-sans flex flex-col h-screen antialiased">

    <!-- Nickname gate -->
    <div id="nickGate" class="fixed inset-0 z-50 bg-gray-900 flex items-center justify-center p-6">
        <div class="w-full max-w-sm bg-gray-800 rounded-2xl p-6 border border-gray-700 shadow-xl">
            <h1 class="text-xl font-bold mb-1 text-center">LAN Chat</h1>
            <p class="text-sm text-gray-400 text-center mb-5">Pick a nickname to join the room.</p>
            <input id="nickInput" maxlength="40" placeholder="Your nickname"
                   class="w-full px-4 py-3 rounded-xl bg-gray-700 border border-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500 mb-3">
            <button id="joinBtn" class="w-full bg-blue-600 hover:bg-blue-700 font-bold py-3 rounded-xl transition">Join Chat</button>
            <p id="nickErr" class="text-red-400 text-sm mt-3 text-center hidden"></p>
        </div>
    </div>

    <!-- Header -->
    <header class="flex items-center justify-between px-4 py-3 bg-gray-800 border-b border-gray-700 shadow-sm">
        <div>
            <div class="font-bold leading-tight">LAN Chat Room</div>
            <div class="text-xs text-gray-400"><span id="userCount">0</span> online</div>
        </div>
        <div id="meBadge" class="text-xs font-semibold bg-gray-700 px-3 py-1 rounded-full text-blue-300"></div>
    </header>

    <!-- Feed -->
    <main id="feed" class="flex-1 overflow-y-auto px-3 py-4 space-y-2"></main>

    <!-- Typing indicator -->
    <div id="typing" class="px-4 h-5 text-xs text-gray-400 italic"></div>

    <!-- Upload progress -->
    <div id="uploadBar" class="hidden px-4 pb-1">
        <div class="flex justify-between text-[11px] text-gray-400 mb-1">
            <span id="uploadName">Uploading...</span><span id="uploadPct">0%</span>
        </div>
        <div class="w-full bg-gray-700 rounded-full h-1.5">
            <div id="uploadFill" class="bg-blue-500 h-1.5 rounded-full transition-all" style="width:0%"></div>
        </div>
    </div>

    <!-- Composer -->
    <footer class="flex items-center gap-2 px-3 py-3 bg-gray-800 border-t border-gray-700">
        <button id="attachBtn" title="Share a file"
                class="flex-shrink-0 w-11 h-11 rounded-full bg-gray-700 hover:bg-gray-600 flex items-center justify-center text-lg transition">📎</button>
        <input type="file" id="fileInput" class="hidden" multiple>
        <input id="msgInput" placeholder="Message"
               class="flex-1 px-4 py-3 rounded-full bg-gray-700 border border-gray-600 focus:outline-none focus:ring-2 focus:ring-blue-500">
        <button id="sendBtn" class="flex-shrink-0 w-11 h-11 rounded-full bg-blue-600 hover:bg-blue-700 flex items-center justify-center text-lg transition">➤</button>
    </footer>

    <script>
        // ITEM 16: unified transport (websocket first, polling fallback) — same config the file layer uses.
        const socket = io({ transports: ['websocket', 'polling'] });

        let myNick = localStorage.getItem('lanchat_nick') || '';
        let joined = false;
        let typingTimer = null;

        const feed = document.getElementById('feed');
        const esc = (s) => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
        const atBottom = () => feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
        const scrollDown = () => { feed.scrollTop = feed.scrollHeight; };

        function addSystem(text) {
            const row = document.createElement('div');
            row.className = 'text-center text-xs text-gray-500 my-1';
            row.innerHTML = esc(text);
            feed.appendChild(row); if (atBottom()) scrollDown();
        }

        function addMessage(m) {
            const mine = m.sender === myNick;
            const wrap = document.createElement('div');
            wrap.className = 'flex ' + (mine ? 'justify-end' : 'justify-start');
            const b = document.createElement('div');
            b.className = 'bubble rounded-2xl px-3 py-2 ' +
                (mine ? 'bg-blue-600 text-white rounded-br-sm' : 'bg-gray-700 text-gray-100 rounded-bl-sm');
            let inner = mine ? '' : `<div class="text-[11px] font-bold text-blue-300 mb-0.5">${esc(m.sender)}</div>`;
            if (m.type === 'file') {
                inner += `<a href="${m.url}" class="flex items-center gap-2 underline decoration-dotted" download>
                            <span class="text-xl">📄</span>
                            <span><span class="font-semibold">${esc(m.name)}</span>
                            <span class="block text-[11px] opacity-80">${esc(m.size_str)} · tap to download</span></span>
                          </a>`;
            } else {
                inner += `<div>${esc(m.text)}</div>`;
            }
            inner += `<div class="text-[10px] opacity-60 text-right mt-1">${esc(m.ts || '')}</div>`;
            b.innerHTML = inner;
            wrap.appendChild(b); feed.appendChild(wrap);
            if (mine || atBottom()) scrollDown();
        }

        // --- Join flow ---
        const gate = document.getElementById('nickGate');
        const nickInput = document.getElementById('nickInput');
        if (myNick) nickInput.value = myNick;

        function doJoin() {
            const n = nickInput.value.trim();
            if (!n) { showNickErr('Enter a nickname.'); return; }
            myNick = n; localStorage.setItem('lanchat_nick', n);
            socket.emit('join', { nick: n });
        }
        function showNickErr(t) {
            const e = document.getElementById('nickErr');
            e.textContent = t; e.classList.remove('hidden');
        }
        document.getElementById('joinBtn').onclick = doJoin;
        nickInput.addEventListener('keydown', e => { if (e.key === 'Enter') doJoin(); });

        socket.on('join_ok', (data) => {
            joined = true;
            gate.style.display = 'none';
            document.getElementById('meBadge').textContent = myNick;
            (data.history || []).forEach(m => (m.system ? addSystem(m.text) : addMessage(m)));
            scrollDown();
        });
        socket.on('join_rejected', (d) => showNickErr(d.reason || 'Could not join.'));

        socket.on('message', addMessage);
        socket.on('system', (d) => addSystem(d.text));
        socket.on('user_count', (d) => document.getElementById('userCount').textContent = d.count);

        socket.on('typing', (d) => {
            const t = document.getElementById('typing');
            t.textContent = d.nick ? `${d.nick} is typing…` : '';
            clearTimeout(typingTimer);
            if (d.nick) typingTimer = setTimeout(() => t.textContent = '', 2500);
        });

        socket.on('connect', () => { if (myNick && !joined) socket.emit('join', { nick: myNick }); });
        socket.on('disconnect', () => addSystem('Disconnected — reconnecting…'));

        // --- Send message ---
        const msgInput = document.getElementById('msgInput');
        function send() {
            const text = msgInput.value.trim();
            if (!text || !joined) return;
            socket.emit('message', { text });
            msgInput.value = '';
        }
        document.getElementById('sendBtn').onclick = send;
        msgInput.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
        msgInput.addEventListener('input', () => { if (joined) socket.emit('typing', {}); });

        // --- File sharing (Resumable.js -> /upload_chunk) ---
        const r = new Resumable({
            target: '/upload_chunk', chunkSize: 1 * 1024 * 1024,
            simultaneousUploads: 2, testChunks: false,
            query: () => ({ sender: myNick })
        });
        r.assignBrowse(document.getElementById('attachBtn'));
        r.assignBrowse(document.getElementById('fileInput'));

        const uBar = document.getElementById('uploadBar');
        r.on('fileAdded', (file) => {
            if (!joined) return;
            document.getElementById('uploadName').textContent = 'Uploading ' + file.fileName;
            uBar.classList.remove('hidden'); r.upload();
        });
        r.on('fileProgress', () => {
            const p = Math.floor(r.progress() * 100);
            document.getElementById('uploadFill').style.width = p + '%';
            document.getElementById('uploadPct').textContent = p + '%';
        });
        r.on('fileSuccess', () => { setTimeout(() => uBar.classList.add('hidden'), 600); });
        r.on('fileError', (file, msg) => {
            uBar.classList.add('hidden');
            let reason = 'Upload failed.';
            try { reason = JSON.parse(msg).error || reason; } catch (e) {}
            addSystem('⚠️ ' + file.fileName + ': ' + reason);
        });

        // Auto-join if returning user
        if (myNick) setTimeout(doJoin, 300);
    </script>
</body>
</html>
"""
