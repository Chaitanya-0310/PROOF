import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Coordinator answers are GitHub-flavoured markdown (headings, bold, bullet
// lists, tables, inline code). Rendering them as plain text leaves the raw
// `**` / `###` / `-` syntax on screen, so every answer goes through here.
// Links open in a new tab; raw HTML in the model output is not rendered.
const components = {
  a: ({ node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
  table: ({ node, ...props }) => (
    <div className="md-table-wrap">
      <table {...props} />
    </div>
  ),
};

export default function Markdown({ children, className = "" }) {
  return (
    <div className={`md ${className}`.trim()}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children || ""}
      </ReactMarkdown>
    </div>
  );
}
