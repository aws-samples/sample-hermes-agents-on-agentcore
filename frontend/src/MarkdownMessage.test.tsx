import assert from 'node:assert/strict';
import test from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { MarkdownMessage } from './MarkdownMessage';

test('renders GitHub-flavored Markdown tables', () => {
  const html = renderToStaticMarkup(
    <MarkdownMessage>{'| Name | Status |\n| --- | --- |\n| Hermes | Ready |'}</MarkdownMessage>,
  );

  assert.match(html, /<table>/);
  assert.match(html, /<th>Name<\/th>/);
  assert.match(html, /<td>Ready<\/td>/);
});
