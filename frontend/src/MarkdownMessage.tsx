import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export function MarkdownMessage({ children }: { children: string }) {
  return <Markdown remarkPlugins={[remarkGfm]}>{children}</Markdown>;
}
