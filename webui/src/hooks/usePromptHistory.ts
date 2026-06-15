import { useCallback, useEffect, useRef, useState } from "react";

const STORAGE_KEY = "durin.promptHistory";
const MAX_ENTRIES = 50;

function loadHistory(): string[] {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr.filter((s) => typeof s === "string").slice(0, MAX_ENTRIES) : [];
  } catch {
    return [];
  }
}

function saveHistory(entries: string[]) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(entries.slice(0, MAX_ENTRIES)));
  } catch {
    // ignore storage errors
  }
}

export function usePromptHistory() {
  const [history] = useState<string[]>(() => loadHistory());
  const indexRef = useRef<number>(-1);
  const draftRef = useRef<string>("");

  const addEntry = useCallback((text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    const current = loadHistory();
    const filtered = current.filter((s) => s !== trimmed);
    const next = [trimmed, ...filtered].slice(0, MAX_ENTRIES);
    saveHistory(next);
  }, []);

  const navigateUp = useCallback(
    (currentText: string): string | null => {
      if (history.length === 0) return null;
      if (indexRef.current === -1) {
        draftRef.current = currentText;
        indexRef.current = 0;
      } else if (indexRef.current < history.length - 1) {
        indexRef.current += 1;
      } else {
        return null;
      }
      return history[indexRef.current] ?? null;
    },
    [history],
  );

  const navigateDown = useCallback((): string | null => {
    if (indexRef.current === -1) return null;
    if (indexRef.current === 0) {
      indexRef.current = -1;
      return draftRef.current;
    }
    indexRef.current -= 1;
    return history[indexRef.current] ?? null;
  }, [history]);

  const reset = useCallback(() => {
    indexRef.current = -1;
    draftRef.current = "";
  }, []);

  useEffect(() => {
    return reset;
  }, [reset]);

  return { addEntry, navigateUp, navigateDown, reset };
}
