/**
 * Runtime injected into artifact documents so their code can ask the host page to run
 * tools on the viewer's behalf.
 *
 * The iframe stays in the current sandbox (scripts only, no same-origin): it never sees
 * a token, a cookie or the API. Everything the artifact asks for travels over a private
 * `MessageChannel` to `Artifacts.svelte`, which re-issues the request over HTTP with the
 * viewer's session and answers on the same port.
 */

export const ARTIFACT_RUNTIME_ATTRIBUTE = 'data-owui-artifact-tools';
export const DEFAULT_ARTIFACT_TOOL_TIMEOUT = 120;
const CONNECT_INTERVAL_MS = 200;
const CONNECT_ATTEMPTS = 50;

/**
 * The bootstrap source. Written as plain ES5 inside an IIFE on purpose: it is inlined in
 * the artifact document before the model's own HTML/JS, in a context with no bundler.
 */
const runtimeSource = (timeout: number): string => `
(function () {
	if (window.owui && window.owui.version) return;

	var TIMEOUT = ${Math.max(1, Math.round(timeout))} * 1000;
	var CONNECT_INTERVAL = ${CONNECT_INTERVAL_MS};
	var CONNECT_ATTEMPTS = ${CONNECT_ATTEMPTS};

	var pending = new Map();
	var seq = 0;
	var connected = false;
	var activeChannel = null;
	var attempts = 0;
	var connectTimer = null;
	var settleReady;
	var ready = new Promise(function (resolve) { settleReady = resolve; });

	function settle(message) {
		var entry = pending.get(message.id);
		if (!entry) return;
		pending.delete(message.id);
		clearTimeout(entry.timer);
		if (message.ok) entry.resolve(message.result);
		else entry.reject(new Error(message.error || 'Artifact tool call failed'));
	}

	function onPortMessage(event) {
		var data = (event && event.data) || {};
		if (data.type === 'owui:artifact:connected') {
			connected = true;
			attempts = 0;
			if (connectTimer) { clearInterval(connectTimer); connectTimer = null; }
			if (settleReady) { settleReady(); settleReady = null; }
			return;
		}
		if (data.type === 'owui:artifact:result') settle(data);
	}

	function connect() {
		if (connected) return;
		var channel = new MessageChannel();
		channel.port1.onmessage = onPortMessage;
		try {
			parent.postMessage({ type: 'owui:artifact:connect' }, '*', [channel.port2]);
			activeChannel = channel;
		} catch (error) {
			// The host may not be listening yet; the retry loop tries again.
		}
		attempts += 1;
		if (attempts >= CONNECT_ATTEMPTS && connectTimer) {
			clearInterval(connectTimer);
			connectTimer = null;
		}
	}

	function send(method, params) {
		return new Promise(function (resolve, reject) {
			var id = ++seq;
			var timer = setTimeout(function () {
				if (!pending.has(id)) return;
				pending.delete(id);
				reject(new Error('Artifact tool call timed out'));
			}, TIMEOUT);
			pending.set(id, { resolve: resolve, reject: reject, timer: timer });
			try {
				activeChannel.port1.postMessage({
					type: 'owui:artifact:rpc',
					id: id,
					method: method,
					params: params || {}
				});
			} catch (error) {
				clearTimeout(timer);
				pending.delete(id);
				reject(error);
			}
		});
	}

	function request(method, params) {
		return ready.then(function () {
			return send(method, params);
		});
	}

	connect();
	connectTimer = setInterval(connect, CONNECT_INTERVAL);

	window.addEventListener('pagehide', function () {
		pending.forEach(function (entry) {
			clearTimeout(entry.timer);
			entry.reject(new Error('Artifact closed'));
		});
		pending.clear();
	});

	window.owui = {
		version: 1,
		tools: {
			list: function () {
				return request('tools/list', {});
			},
			call: function (name, args) {
				return request('tools/call', { name: name, arguments: args || {} });
			}
		},
		mcp: {
			call: function (serverId, tool, args) {
				return request('tools/call', {
					name: serverId + '_' + tool,
					arguments: args || {},
					server_id: serverId,
					tool: tool
				});
			}
		}
	};
})();
`;

const escapeAttribute = (value: string) => value.replace(/&/g, '&amp;').replace(/"/g, '&quot;');

/**
 * Inline the runtime at the top of the artifact's `<body>` so it exists before any of
 * the model's markup or scripts run.
 */
export const injectArtifactToolRuntime = (
	html: string,
	options: { timeout?: number } = {}
): string => {
	const timeout =
		Number.isFinite(options.timeout) && (options.timeout as number) > 0
			? (options.timeout as number)
			: DEFAULT_ARTIFACT_TOOL_TIMEOUT;
	const tag = `<script ${ARTIFACT_RUNTIME_ATTRIBUTE}="${escapeAttribute(String(timeout))}">${runtimeSource(timeout)}</` + `script>`;

	const bodyMatch = /<body[^>]*>/i.exec(html);
	if (bodyMatch) {
		const insertAt = bodyMatch.index + bodyMatch[0].length;
		return html.slice(0, insertAt) + tag + html.slice(insertAt);
	}

	const headClose = html.toLowerCase().lastIndexOf('</head>');
	if (headClose !== -1) {
		return html.slice(0, headClose) + tag + html.slice(headClose);
	}

	return tag + html;
};

/** Resolve the model-facing name of an MCP tool the way the backend names it. */
export const resolveArtifactToolName = (
	tools: Array<{ name: string; label?: string; server_id?: string | null }>,
	name: string,
	serverId?: string,
	tool?: string
): string | null => {
	if (tools.some((item) => item.name === name)) return name;

	if (serverId && tool) {
		const exact = `${serverId}_${tool}`;
		if (tools.some((item) => item.name === exact)) return exact;

		const match = tools.find(
			(item) =>
				item.server_id === serverId && (item.label === tool || item.name.endsWith(`_${tool}`))
		);
		if (match) return match.name;
	}

	return null;
};
