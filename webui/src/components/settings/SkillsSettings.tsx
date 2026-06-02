import { useCallback, useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  ApiError,
  getSkill,
  listSkills,
  saveSkill,
  setSkillMode,
  type SkillDetail,
  type SkillRow,
} from "@/lib/api";
import { SettingsGroup, SettingsRow, SettingsSectionTitle } from "./primitives";

export function SkillsSettings({ token }: { token: string }) {
  const { t } = useTranslation();
  const [rows, setRows] = useState<SkillRow[] | null>(null);
  const [selected, setSelected] = useState<SkillDetail | null>(null);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setRows(await listSkills(token));
    } catch (e) {
      setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const open = useCallback(
    async (name: string) => {
      setError(null);
      try {
        const detail = await getSkill(token, name);
        setSelected(detail);
        setDraft(detail.content);
      } catch (e) {
        setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
      }
    },
    [token],
  );

  const toggleMode = useCallback(
    async (row: SkillRow) => {
      setBusy(row.name);
      try {
        await setSkillMode(token, row.name, row.mode === "auto" ? "manual" : "auto");
        await refresh();
        if (selected?.name === row.name) await open(row.name);
      } catch (e) {
        setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
      } finally {
        setBusy(null);
      }
    },
    [token, refresh, selected, open],
  );

  const save = useCallback(async () => {
    if (!selected) return;
    setBusy(selected.name);
    try {
      await saveSkill(token, selected.name, draft);
      await refresh();
    } catch (e) {
      setError(e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message);
    } finally {
      setBusy(null);
    }
  }, [token, selected, draft, refresh]);

  if (loading) {
    return (
      <div className="flex h-40 items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        {t("settings.status.loading")}
      </div>
    );
  }

  return (
    <SettingsGroup>
      <SettingsSectionTitle>{t("settings.nav.skills")}</SettingsSectionTitle>
      {error && <p className="text-sm text-destructive">{error}</p>}
      {(rows ?? []).map((row) => (
        <SettingsRow
          key={row.name}
          title={`${row.name}  ·  ${row.mode}`}
          description={
            `${row.source}` +
            (row.description ? ` — ${row.description}` : "") +
            (row.provenance?.source ? ` · from ${row.provenance.source}` : "")
          }
        >
          <div className="flex gap-2">
            <Button variant="outline" size="sm" onClick={() => void open(row.name)}>
              View
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={busy === row.name}
              onClick={() => void toggleMode(row)}
            >
              {row.mode === "auto" ? "Make manual" : "Make auto"}
            </Button>
          </div>
        </SettingsRow>
      ))}

      {selected && (
        <div className="mt-4 space-y-2">
          <p className="text-sm font-medium">
            {selected.name} ({selected.mode})
          </p>
          <textarea
            className="h-64 w-full rounded-md border bg-background p-2 font-mono text-xs"
            value={draft}
            disabled={selected.mode !== "manual"}
            onChange={(e) => setDraft(e.target.value)}
          />
          {selected.mode !== "manual" ? (
            <p className="text-xs text-muted-foreground">
              Read-only: this skill is in <code>auto</code> mode (managed by the agent). Switch it to{" "}
              <code>manual</code> to edit.
            </p>
          ) : (
            <Button size="sm" disabled={busy === selected.name} onClick={() => void save()}>
              Save
            </Button>
          )}
        </div>
      )}
    </SettingsGroup>
  );
}
