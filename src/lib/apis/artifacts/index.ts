import { WEBUI_API_BASE_URL } from '$lib/constants';

export type ArtifactToolSpec = {
	name: string;
	label?: string;
	description?: string;
	parameters?: Record<string, unknown>;
	type?: string;
	server_id?: string | null;
};

export type ArtifactToolListResponse = {
	tools: ArtifactToolSpec[];
	timeout?: number;
};

export type ArtifactToolCallResponse = {
	name: string;
	ok: boolean;
	result: unknown;
	files?: unknown[];
	embeds?: unknown[];
	duration?: number;
};

/**
 * Describe the tools an artifact is allowed to call.
 *
 * The backend re-resolves every id against the viewer's access grants, so the
 * returned list is authoritative: an id the viewer cannot use is simply absent.
 */
export const listArtifactTools = async (
	token: string,
	chatId: string,
	payload: { message_id?: string | null; tool_ids: string[] },
	signal?: AbortSignal
): Promise<ArtifactToolListResponse> => {
	const response = await fetch(
		`${WEBUI_API_BASE_URL}/chats/${encodeURIComponent(chatId)}/artifacts/tools/list`,
		{
			method: 'POST',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				authorization: `Bearer ${token}`
			},
			body: JSON.stringify(payload),
			signal
		}
	);

	if (!response.ok) {
		const error = await response.json().catch(() => null);
		throw new Error(
			typeof error?.detail === 'string' ? error.detail : `Artifact request failed (${response.status})`
		);
	}

	return response.json();
};

/** Run one tool on behalf of the viewer. */
export const callArtifactTool = async (
	token: string,
	chatId: string,
	payload: {
		message_id?: string | null;
		tool_ids: string[];
		name: string;
		arguments?: Record<string, unknown>;
	},
	signal?: AbortSignal
): Promise<ArtifactToolCallResponse> => {
	const response = await fetch(
		`${WEBUI_API_BASE_URL}/chats/${encodeURIComponent(chatId)}/artifacts/tools/call`,
		{
			method: 'POST',
			headers: {
				Accept: 'application/json',
				'Content-Type': 'application/json',
				authorization: `Bearer ${token}`
			},
			body: JSON.stringify(payload),
			signal
		}
	);

	if (!response.ok) {
		const error = await response.json().catch(() => null);
		throw new Error(
			typeof error?.detail === 'string' ? error.detail : `Artifact request failed (${response.status})`
		);
	}

	return response.json();
};
