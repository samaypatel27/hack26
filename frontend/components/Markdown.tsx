import ReactMarkdown from "react-markdown";

/** Post text is community-written: render markdown, but never raw HTML (react-markdown's default). */
export function Markdown({ text }: { text: string }) {
  return (
    <div className="prose-paper break-words">
      <ReactMarkdown>{text}</ReactMarkdown>
    </div>
  );
}
