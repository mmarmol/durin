// webui/src/components/rich/MermaidPreview.tsx
export default function MermaidPreview({ code }: { code: string }) {
  return <pre className="p-4 text-xs">{code}</pre>;
}
