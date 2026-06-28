// webui/src/components/rich/ChartPreview.tsx
export default function ChartPreview({ code }: { code: string }) {
  return <pre className="p-4 text-xs">{code}</pre>;
}
