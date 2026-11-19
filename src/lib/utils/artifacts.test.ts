import { describe, expect, it } from 'vitest';
import {
	DEFAULT_ARTIFACT_TOOL_TIMEOUT,
	ARTIFACT_RUNTIME_ATTRIBUTE,
	injectArtifactToolRuntime,
	resolveArtifactToolName
} from './artifacts';

const document = (body: string, head = '<head></head>') =>
	`<!DOCTYPE html><html>${head}<body>${body}</body></html>`;

describe('injectArtifactToolRuntime', () => {
	it('installs the runtime before the artifact body content', () => {
		const html = document('<h1>hi</h1>');
		const injected = injectArtifactToolRuntime(html);

		const runtimeAt = injected.indexOf(ARTIFACT_RUNTIME_ATTRIBUTE);
		const bodyAt = injected.indexOf('<h1>hi</h1>');

		expect(runtimeAt).toBeGreaterThan(-1);
		expect(bodyAt).toBeGreaterThan(runtimeAt);
		expect(injected).toContain('window.owui');
	});

	it('survives a body tag with attributes', () => {
		const injected = injectArtifactToolRuntime(
			'<html><body class="dark" data-x="1"><p>x</p></body></html>'
		);

		expect(injected.indexOf(ARTIFACT_RUNTIME_ATTRIBUTE)).toBeGreaterThan(
			injected.indexOf('<body class="dark" data-x="1">')
		);
		expect(injected.indexOf(ARTIFACT_RUNTIME_ATTRIBUTE)).toBeLessThan(injected.indexOf('<p>x</p>'));
	});

	it('falls back to the head and then to the document start', () => {
		expect(injectArtifactToolRuntime('<html><head></head></html>')).toContain(
			ARTIFACT_RUNTIME_ATTRIBUTE
		);
		expect(injectArtifactToolRuntime('<p>fragment</p>').startsWith('<script')).toBe(true);
	});

	it('carries the timeout into both the attribute and the runtime', () => {
		const injected = injectArtifactToolRuntime(document(''), { timeout: 15 });

		expect(injected).toContain(`${ARTIFACT_RUNTIME_ATTRIBUTE}="15"`);
		expect(injected).toContain('var TIMEOUT = 15 * 1000;');
	});

	it('falls back to the default timeout for unusable values', () => {
		const injected = injectArtifactToolRuntime(document(''), { timeout: -1 });

		expect(injected).toContain(`var TIMEOUT = ${DEFAULT_ARTIFACT_TOOL_TIMEOUT} * 1000;`);
	});

	it('does not emit a literal closing script tag inside the runtime', () => {
		const injected = injectArtifactToolRuntime(document(''));
		const runtime = injected.slice(injected.indexOf('<script'));

		expect(runtime.match(/<\/script>/g)?.length).toBe(1);
	});
});

describe('resolveArtifactToolName', () => {
	const tools = [
		{ name: 'search_docs', label: 'search_docs' },
		{ name: 'github_list_issues', label: 'list_issues', server_id: 'github' },
		{ name: 'averylongservername_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_1234abcd', label: 'lookup', server_id: 'averylongservername' }
	];

	it('accepts an exact model-facing name', () => {
		expect(resolveArtifactToolName(tools, 'search_docs')).toBe('search_docs');
	});

	it('resolves the server + tool pair the way the backend names MCP tools', () => {
		expect(resolveArtifactToolName(tools, 'github_list_issues', 'github', 'list_issues')).toBe(
			'github_list_issues'
		);
	});

	it('resolves a hashed MCP name by its server and label', () => {
		expect(
			resolveArtifactToolName(tools, 'ignored', 'averylongservername', 'lookup')
		).toBe('averylongservername_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_1234abcd');
	});

	it('refuses tools that are not in the allowlist', () => {
		expect(resolveArtifactToolName(tools, 'delete_everything')).toBeNull();
		expect(resolveArtifactToolName(tools, 'list_issues', 'other', 'list_issues')).toBeNull();
	});
});
