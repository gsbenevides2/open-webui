<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { getContext, onDestroy, onMount } from 'svelte';
	import { readable, type Readable } from 'svelte/store';
	const i18n: any = getContext('i18n');

	import { config, settings } from '$lib/stores';
	import { copyToClipboard } from '$lib/utils';
	import { injectCsp } from '$lib/utils/csp';
	import {
		DEFAULT_ARTIFACT_TOOL_TIMEOUT,
		injectArtifactToolRuntime,
		resolveArtifactToolName
	} from '$lib/utils/artifacts';
	import { callArtifactTool, listArtifactTools, type ArtifactToolSpec } from '$lib/apis/artifacts';

	import Icon from './FileNav/Icon.svelte';
	import Tooltip from '../common/Tooltip.svelte';
	import SvgPanZoom from '../common/SVGPanZoom.svelte';
	import ConfirmDialog from '../common/ConfirmDialog.svelte';
	import Spinner from '../common/Spinner.svelte';

	export let overlay = false;
	export let artifacts: Readable<{ type: string; content: string; messageId?: string }[]>;
	/** The code block to show; a new object selects it again. */
	export let selection: { code: string } | null = null;
	export let chatId = '';
	/** Tools selected in the chat: the artifact may call these and nothing else. */
	export let artifactToolIds: Readable<string[]> = readable([]);
	$: toolIds = $artifactToolIds ?? [];

	let contents: { type: string; content: string; messageId?: string }[] = [];
	let selectedContentIdx = 0;
	let appliedSelection: typeof selection = null;

	const updateContents = (next: { type: string; content: string; messageId?: string }[]) => {
		if (next.length === 0) selectedContentIdx = 0;
		else if (next.length > contents.length) selectedContentIdx = next.length - 1;
		else if (selectedContentIdx > next.length - 1) selectedContentIdx = next.length - 1;
		return next;
	};
	$: contents = updateContents($artifacts ?? []);
	$: if (selection && selection !== appliedSelection && contents.length > 0) {
		appliedSelection = selection;
		const codeIdx = contents.findIndex((content) => content.content.includes(selection.code));
		if (codeIdx !== -1) selectedContentIdx = codeIdx;
	}

	let copied = false;
	let iframeElement: HTMLIFrameElement;

	function navigateContent(direction: 'prev' | 'next') {
		selectedContentIdx =
			direction === 'prev'
				? Math.max(selectedContentIdx - 1, 0)
				: Math.min(selectedContentIdx + 1, contents.length - 1);
	}

	const iframeLoadHandler = () => {
		iframeElement.contentWindow.addEventListener(
			'click',
			function (e) {
				const target = e.target.closest('a');
				if (target && target.href) {
					e.preventDefault();
					const url = new URL(target.href, iframeElement.baseURI);
					if (url.origin === window.location.origin) {
						iframeElement.contentWindow.history.pushState(
							null,
							'',
							url.pathname + url.search + url.hash
						);
					} else {
						console.info('External navigation blocked:', url.href);
					}
				}
			},
			true
		);

		// Cancel drag when hovering over iframe
		iframeElement.contentWindow.addEventListener('mouseenter', function (e) {
			e.preventDefault();
			iframeElement.contentWindow.addEventListener('dragstart', (event) => {
				event.preventDefault();
			});
		});
	};

	const showFullScreen = () => {
		if (iframeElement.requestFullscreen) {
			iframeElement.requestFullscreen();
		} else if (iframeElement.webkitRequestFullscreen) {
			iframeElement.webkitRequestFullscreen();
		} else if (iframeElement.msRequestFullscreen) {
			iframeElement.msRequestFullscreen();
		}
	};

	const downloadArtifact = () => {
		const blob = new Blob([contents[selectedContentIdx].content], { type: 'text/html' });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = `artifact-${chatId}-${selectedContentIdx}.html`;
		document.body.appendChild(a);
		a.click();
		document.body.removeChild(a);
		URL.revokeObjectURL(url);
	};

	// Artifact → tools bridge.
	//
	// The artifact document gets a tiny runtime that speaks to this component over a
	// private MessageChannel. The iframe never holds a token: every call is re-issued
	// here with the viewer's session and the backend re-checks access grants.
	$: artifactToolBridgeEnabled =
		($config?.features?.enable_artifact_tools ?? false) &&
		($settings?.iframeSandboxAllowScripts ?? true) &&
		toolIds.length > 0;

	let allowlistKey = '';
	let artifactTools: ArtifactToolSpec[] = [];
	let toolsResolved = false;
	let toolTimeout = DEFAULT_ARTIFACT_TOOL_TIMEOUT;
	let runningTool = '';
	let bridgePort: MessagePort | null = null;
	let approvedTools = new Set<string>();
	let consent: { show: boolean; title: string; text: string; resolve: ((v: boolean) => void) | null } = {
		show: false,
		title: '',
		text: '',
		resolve: null
	};

	$: {
		const nextKey = [...toolIds].sort().join(',');
		if (nextKey !== allowlistKey) {
			allowlistKey = nextKey;
			toolsResolved = false;
			artifactTools = [];
			approvedTools = new Set();
		}
	}

	$: currentArtifactMessageId = contents[selectedContentIdx]?.messageId ?? null;
	$: artifactSrcdoc = contents.length ? renderArtifactDocument(contents[selectedContentIdx].content) : '';

	function renderArtifactDocument(content: string) {
		const withRuntime = artifactToolBridgeEnabled
			? injectArtifactToolRuntime(content, { timeout: toolTimeout })
			: content;

		return injectCsp(withRuntime, $config?.ui?.iframe_csp ?? '');
	}

	const respond = (port: MessagePort, id: number, payload: Record<string, unknown>) => {
		try {
			port.postMessage({ type: 'owui:artifact:result', id, ...payload });
		} catch (error) {
			console.debug('Artifact response dropped', error);
		}
	};

	const finishConsent = (accepted: boolean) => {
		const resolve = consent.resolve;
		consent = { show: false, title: '', text: '', resolve: null };
		resolve?.(accepted);
	};

	// Escape or a backdrop click closes the dialog without resolving; never leak a promise.
	$: if (consent.resolve && !consent.show) finishConsent(false);

	const requestConsent = (name: string, args: Record<string, unknown>) =>
		new Promise<boolean>((resolve) => {
			if (approvedTools.has(name)) {
				resolve(true);
				return;
			}

			consent = {
				show: true,
				title: $i18n.t('Allow this artifact to run a tool?'),
				text: `${name}\n\n${JSON.stringify(args ?? {}, null, 2)}`,
				resolve
			};
		});

	const loadTools = async () => {
		if (toolsResolved) return artifactTools;

		const response = await listArtifactTools(localStorage.token, chatId || 'local', {
			message_id: currentArtifactMessageId,
			tool_ids: toolIds
		});
		artifactTools = response.tools ?? [];
		toolTimeout = response.timeout ?? DEFAULT_ARTIFACT_TOOL_TIMEOUT;
		toolsResolved = true;

		return artifactTools;
	};

	const handleRpc = async (port: MessagePort, payload: any) => {
		const id = payload?.id;
		if (typeof id !== 'number') return;

		try {
			if (payload.method === 'tools/list') {
				respond(port, id, { ok: true, result: await loadTools() });
				return;
			}

			if (payload.method !== 'tools/call') throw new Error('Unsupported artifact request');

			const params = payload.params ?? {};
			const tools = await loadTools();
			const name = resolveArtifactToolName(
				tools,
				String(params.name ?? ''),
				typeof params.server_id === 'string' ? params.server_id : undefined,
				typeof params.tool === 'string' ? params.tool : undefined
			);

			if (!name) throw new Error('Tool not available to this artifact');

			const args =
				params.arguments && typeof params.arguments === 'object' && !Array.isArray(params.arguments)
					? params.arguments
					: {};

			if (!(await requestConsent(name, args))) throw new Error('Tool call was not approved');

			runningTool = name;
			try {
				const result = await callArtifactTool(localStorage.token, chatId || 'local', {
					message_id: currentArtifactMessageId,
					tool_ids: toolIds,
					name,
					arguments: args
				});
				respond(port, id, { ok: true, result });
			} finally {
				runningTool = '';
			}
		} catch (error: any) {
			respond(port, id, { ok: false, error: error?.message ?? 'Artifact request failed' });
		}
	};

	const handleBridgeConnect = (event: MessageEvent) => {
		if (!artifactToolBridgeEnabled) return;
		if (!iframeElement || event.source !== iframeElement.contentWindow) return;
		if (event.data?.type !== 'owui:artifact:connect') return;

		const port = event.ports?.[0];
		if (!port) return;

		bridgePort?.close();
		bridgePort = port;
		port.onmessage = (message) => {
			const data = message.data;
			if (data?.type !== 'owui:artifact:rpc') return;
			void handleRpc(port, data);
		};
		port.start?.();
		// The runtime retries until it hears this, so a late mount still connects.
		port.postMessage({ type: 'owui:artifact:connected' });
	};

	onMount(() => {
		window.addEventListener('message', handleBridgeConnect);
	});

	onDestroy(() => {
		window.removeEventListener('message', handleBridgeConnect);
		bridgePort?.close();
		bridgePort = null;
		finishConsent(false);
	});
</script>

<div class="w-full h-full relative flex flex-col">
	<div class="w-full min-h-0 flex flex-col flex-1 relative">
		{#if contents.length > 0}
			<div
				class="pointer-events-auto z-20 shrink-0 flex items-center justify-between gap-2 px-2 pb-1.5"
			>
				<div class="flex min-w-0 items-center gap-0.5" dir="ltr">
					<button
						aria-label={$i18n.t('Previous version')}
						class="flex h-5 w-5 shrink-0 items-center justify-center rounded text-gray-400 transition-colors duration-100 hover:text-gray-600 disabled:text-gray-200 dark:text-gray-500 dark:hover:text-gray-300 dark:disabled:text-gray-700"
						on:click={() => navigateContent('prev')}
						disabled={selectedContentIdx === 0}
						><Icon name="chevron-left" size={11} strokeWidth={1.5} /></button
					>
					<span class="truncate text-xs text-gray-500 dark:text-gray-400">
						{$i18n.t('Version {{selectedVersion}} of {{totalVersions}}', {
							selectedVersion: selectedContentIdx + 1,
							totalVersions: contents.length
						})}
					</span>
					<button
						aria-label={$i18n.t('Next version')}
						class="flex h-5 w-5 shrink-0 items-center justify-center rounded text-gray-400 transition-colors duration-100 hover:text-gray-600 disabled:text-gray-200 dark:text-gray-500 dark:hover:text-gray-300 dark:disabled:text-gray-700"
						on:click={() => navigateContent('next')}
						disabled={selectedContentIdx === contents.length - 1}
						><Icon name="chevron-right" size={11} strokeWidth={1.5} /></button
					>
				</div>
				<div class="flex shrink-0 items-center gap-0.5">
					{#if artifactToolBridgeEnabled && artifactTools.length > 0}
						<Tooltip content={$i18n.t('Tools available to this artifact')}>
							<div
								class="flex h-5 items-center gap-0.5 rounded px-1 text-xs text-gray-400 dark:text-gray-500"
							>
								<Icon name="tools" size={11} strokeWidth={1.4} />
								<span>{artifactTools.length}</span>
							</div>
						</Tooltip>
					{/if}
					<Tooltip content={copied ? $i18n.t('Copied') : $i18n.t('Copy')}>
						<button
							aria-label={copied ? $i18n.t('Copied') : $i18n.t('Copy')}
							class="flex h-5 w-5 items-center justify-center rounded text-gray-400 transition-colors duration-100 hover:text-gray-600 dark:text-gray-500 dark:hover:text-gray-300"
							on:click={() => {
								copyToClipboard(contents[selectedContentIdx].content);
								copied = true;
								setTimeout(() => {
									copied = false;
								}, 2000);
							}}><Icon name={copied ? 'check' : 'copy'} size={11} strokeWidth={1.4} /></button
						>
					</Tooltip>
					<Tooltip content={$i18n.t('Download')}>
						<button
							aria-label={$i18n.t('Download')}
							class="flex h-5 w-5 items-center justify-center rounded text-gray-400 transition-colors duration-100 hover:text-gray-600 dark:text-gray-500 dark:hover:text-gray-300"
							on:click={downloadArtifact}
							><Icon name="download" size={11} strokeWidth={1.4} /></button
						>
					</Tooltip>
					{#if contents[selectedContentIdx].type === 'iframe'}
						<Tooltip content={$i18n.t('Open in full screen')}>
							<button
								aria-label={$i18n.t('Open in full screen')}
								class="flex h-5 w-5 items-center justify-center rounded text-gray-400 transition-colors duration-100 hover:text-gray-600 dark:text-gray-500 dark:hover:text-gray-300"
								on:click={showFullScreen}><Icon name="expand" size={11} strokeWidth={1.4} /></button
							>
						</Tooltip>
					{/if}
				</div>
			</div>
		{/if}

		{#if overlay}
			<div class=" absolute top-0 left-0 right-0 bottom-0 z-10"></div>
		{/if}

		{#if runningTool}
			<div
				class="pointer-events-none absolute inset-x-0 top-0 z-30 flex items-center justify-center gap-2 bg-white/80 px-2 py-1 text-xs text-gray-600 dark:bg-gray-850/80 dark:text-gray-300"
			>
				<Spinner />
				<span class="truncate">
					{$i18n.t('Running {{name}} from this artifact', { name: runningTool })}
				</span>
			</div>
		{/if}

		<div class="flex-1 w-full min-h-0 bg-white dark:bg-gray-850">
			<div class=" h-full flex flex-col">
				{#if contents.length > 0}
					<div class="max-w-full w-full h-full">
						{#if contents[selectedContentIdx].type === 'iframe'}
							<iframe
								bind:this={iframeElement}
								title={$i18n.t('Content')}
								srcdoc={artifactSrcdoc}
								class="w-full border-0 h-full rounded-none"
								sandbox="{($settings?.iframeSandboxAllowScripts ?? true)
									? 'allow-scripts'
									: ''}{($settings?.iframeSandboxAllowDownloads ?? true)
									? ' allow-downloads'
									: ''}{($settings?.iframeSandboxAllowForms ?? true)
									? ' allow-forms'
									: ''}{($settings?.iframeSandboxAllowSameOrigin ?? false)
									? ' allow-same-origin'
									: ''}"
								on:load={iframeLoadHandler}
							></iframe>
						{:else if contents[selectedContentIdx].type === 'svg'}
							<SvgPanZoom
								className=" w-full h-full max-h-full overflow-hidden"
								svg={contents[selectedContentIdx].content}
							/>
						{/if}
					</div>
				{:else}
					<div class="m-auto p-6 text-center font-normal text-xs text-gray-900 dark:text-white">
						{$i18n.t('No HTML, CSS, or JavaScript content found.')}
					</div>
				{/if}
			</div>
		</div>
	</div>
</div>

{#if artifactToolBridgeEnabled}
	<ConfirmDialog
		bind:show={consent.show}
		title={consent.title}
		confirmLabel={$i18n.t('Allow')}
		cancelLabel={$i18n.t('Cancel')}
		on:confirm={() => finishConsent(true)}
		on:cancel={() => finishConsent(false)}
	>
		<pre class="whitespace-pre-wrap break-words text-xs">{consent.text}</pre>
	</ConfirmDialog>
{/if}
