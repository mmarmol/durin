import { useCallback, useEffect, useState } from "react";
import { Drama, Loader2, Pencil, Plus, ScrollText, Star, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  ApiError,
  deletePersona,
  deleteSoul,
  listPersonas,
  listSouls,
  savePersona,
  saveSoul,
  setDefaultPersona,
  testPersona,
  type PersonaItem,
  type SoulItem,
} from "@/lib/api";

import { ModelSelectField } from "@/components/ModelSelectField";

import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "./primitives";

const DEFAULT_SOUL_SLUG = "default";

function errLabel(e: unknown): string {
  return e instanceof ApiError ? `HTTP ${e.status}` : (e as Error).message;
}

const labelClass = "block text-[12px] font-medium text-foreground/80 mb-1";
const inputClass =
  "w-full rounded-md border border-border/60 bg-background px-3 py-1.5 text-[13px] text-foreground " +
  "placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring";
const selectClass =
  "rounded-md border border-border/60 bg-background px-2 py-1.5 text-[13px] text-foreground " +
  "focus:outline-none focus:ring-1 focus:ring-ring";

/** Shared inline "Delete? [Delete] [Cancel]" affordance — no native
 *  window.confirm (durin webui convention). Mirrors the McpSettings row
 *  delete pattern. */
function InlineDelete({
  confirming,
  busy,
  disabled,
  onAsk,
  onConfirm,
  onCancel,
}: {
  confirming: boolean;
  busy: boolean;
  disabled?: boolean;
  onAsk: () => void;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  if (disabled) return null;
  return confirming ? (
    <div className="flex items-center gap-1.5">
      <span className="text-[12px] text-muted-foreground">
        {t("settings.personas.confirmDelete")}
      </span>
      <Button
        size="sm"
        variant="ghost"
        disabled={busy}
        onClick={onConfirm}
        className="rounded-full text-destructive hover:text-destructive"
      >
        <Trash2 className="mr-1.5 h-3.5 w-3.5" aria-hidden />
        {t("settings.personas.delete")}
      </Button>
      <Button size="sm" variant="ghost" disabled={busy} onClick={onCancel} className="rounded-full">
        {t("settings.personas.cancel")}
      </Button>
    </div>
  ) : (
    <Button
      size="sm"
      variant="ghost"
      disabled={busy}
      onClick={onAsk}
      className="rounded-full text-destructive hover:text-destructive"
      title={t("settings.personas.delete")}
    >
      <Trash2 className="h-3.5 w-3.5" aria-hidden />
    </Button>
  );
}

/** Collapsible create/edit form for a SOUL document (slug + body). When
 *  editing an existing soul the slug is locked (it's the identity). */
function SoulForm({
  token,
  editSoul,
  onDone,
  onCancel,
}: {
  token: string;
  editSoul: SoulItem | null;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [slug, setSlug] = useState(editSoul?.slug ?? "");
  const [body, setBody] = useState(editSoul?.body ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await saveSoul(token, { slug: slug.trim(), body });
      onDone();
    } catch (e) {
      setError(errLabel(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={(e) => void handleSubmit(e)} className="space-y-4 px-5 py-4">
      <div>
        <label htmlFor="soul-slug" className={labelClass}>
          {t("settings.personas.fieldSlug")}
        </label>
        <input
          id="soul-slug"
          className={inputClass}
          value={slug}
          onChange={(e) => setSlug(e.target.value)}
          placeholder={t("settings.personas.slugPlaceholder")}
          disabled={editSoul !== null}
          required
          autoComplete="off"
        />
      </div>
      <div>
        <label htmlFor="soul-body" className={labelClass}>
          {t("settings.personas.fieldBody")}
        </label>
        <textarea
          id="soul-body"
          className={cn(inputClass, "resize-y min-h-[160px] font-mono")}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          required
        />
      </div>
      {error ? <p className="text-[12px] text-destructive">{error}</p> : null}
      <div className="flex items-center justify-end gap-2 pt-1">
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={saving}>
          {t("settings.personas.cancel")}
        </Button>
        <Button type="submit" size="sm" disabled={saving}>
          {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
          {t("settings.personas.save")}
        </Button>
      </div>
    </form>
  );
}

type TestResult = { ok: boolean; reply?: string | null; error?: string | null; model?: string | null };

/** Collapsible create/edit form for a persona: name + model + soul +
 *  description. When editing, the name is locked (it's the identity). */
function PersonaForm({
  token,
  editPersona,
  souls,
  onDone,
  onCancel,
}: {
  token: string;
  editPersona: PersonaItem | null;
  souls: SoulItem[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(editPersona?.name ?? "");
  const [model, setModel] = useState(editPersona?.model ?? "");
  const [soul, setSoul] = useState(editPersona?.soul ?? DEFAULT_SOUL_SLUG);
  const [description, setDescription] = useState(editPersona?.description ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestResult | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await savePersona(token, {
        name: name.trim(),
        model: model.trim() || null,
        soul: soul || DEFAULT_SOUL_SLUG,
        description: description.trim() || null,
      });
      onDone();
    } catch (e) {
      setError(errLabel(e));
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await testPersona(token, {
        model: model.trim() || null,
        soul: soul || null,
      });
      setTestResult(res);
    } catch (e) {
      setTestResult({ ok: false, error: errLabel(e) });
    } finally {
      setTesting(false);
    }
  };

  // Clear test result when model or soul changes.
  const handleModelChange = (ref: string) => {
    setModel(ref);
    setTestResult(null);
  };
  const handleSoulChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    setSoul(e.target.value);
    setTestResult(null);
  };

  // The default soul is always selectable even if listSouls hasn't loaded
  // it (it's built in), so the select never has an empty option set.
  const soulSlugs = souls.some((s) => s.slug === DEFAULT_SOUL_SLUG)
    ? souls.map((s) => s.slug)
    : [DEFAULT_SOUL_SLUG, ...souls.map((s) => s.slug)];

  return (
    <form onSubmit={(e) => void handleSubmit(e)} className="space-y-4 px-5 py-4">
      <div>
        <label htmlFor="persona-name" className={labelClass}>
          {t("settings.personas.fieldName")}
        </label>
        <input
          id="persona-name"
          className={inputClass}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={t("settings.personas.namePlaceholder")}
          disabled={editPersona !== null}
          required
          autoComplete="off"
        />
      </div>
      <div>
        <label className={labelClass}>{t("settings.personas.fieldModel")}</label>
        <ModelSelectField value={model} onChange={handleModelChange} />
      </div>
      <div>
        <label htmlFor="persona-soul" className={labelClass}>
          {t("settings.personas.fieldSoul")}
        </label>
        <select
          id="persona-soul"
          className={cn(selectClass, "w-full")}
          value={soul}
          onChange={handleSoulChange}
        >
          {soulSlugs.map((slug) => (
            <option key={slug} value={slug}>
              {slug}
            </option>
          ))}
        </select>
      </div>
      <div>
        <label htmlFor="persona-description" className={labelClass}>
          {t("settings.personas.fieldDescription")}
        </label>
        <input
          id="persona-description"
          className={inputClass}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder={t("settings.personas.descriptionPlaceholder")}
          autoComplete="off"
        />
      </div>
      {error ? <p className="text-[12px] text-destructive">{error}</p> : null}

      {testResult ? (
        <div
          className={cn(
            "rounded-lg border px-3 py-2.5 text-[12px]",
            testResult.ok
              ? "border-green-500/20 bg-green-500/5 text-foreground"
              : "border-destructive/20 bg-destructive/5 text-destructive",
          )}
        >
          <div className="mb-1 font-medium">
            {testResult.ok ? t("settings.personas.testOk") : t("settings.personas.testError")}
            {testResult.ok && testResult.model ? (
              <span className="ml-1.5 font-mono font-normal text-muted-foreground">
                ({testResult.model})
              </span>
            ) : null}
          </div>
          <p className="whitespace-pre-wrap leading-relaxed">
            {testResult.ok ? (testResult.reply ?? "") : (testResult.error ?? "")}
          </p>
        </div>
      ) : null}

      <div className="flex items-center justify-between pt-1">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={testing || saving}
          onClick={() => void handleTest()}
          className="text-[12px] text-muted-foreground"
        >
          {testing ? (
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
          ) : null}
          {testing ? t("settings.personas.testing") : t("settings.personas.test")}
        </Button>
        <div className="flex items-center gap-2">
          <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={saving}>
            {t("settings.personas.cancel")}
          </Button>
          <Button type="submit" size="sm" disabled={saving}>
            {saving ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
            {t("settings.personas.save")}
          </Button>
        </div>
      </div>
    </form>
  );
}

/** Settings → Personas. Two CRUD sections over the persona subsystem:
 *  a SOUL library (reusable identity documents) and persona presets that
 *  pair a SOUL with an optional model + description, with one marked the
 *  session default. Mirrors CronSettings' list/form/busy/refresh shape. */
export function PersonasSettings({ token }: { token: string }) {
  const { t } = useTranslation();
  const [souls, setSouls] = useState<SoulItem[] | null>(null);
  const [personas, setPersonas] = useState<PersonaItem[] | null>(null);
  const [defaultPersona, setDefaultName] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  // null = closed; 'new' = create; item = edit.
  const [soulForm, setSoulForm] = useState<"new" | SoulItem | null>(null);
  const [personaForm, setPersonaForm] = useState<"new" | PersonaItem | null>(null);
  const [confirmSoul, setConfirmSoul] = useState<string | null>(null);
  const [confirmPersona, setConfirmPersona] = useState<string | null>(null);
  const [testingRow, setTestingRow] = useState<string | null>(null);
  const [rowTest, setRowTest] = useState<{ name: string; res: TestResult } | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [s, p] = await Promise.all([listSouls(token), listPersonas(token)]);
      setSouls(s);
      setPersonas(p.personas);
      setDefaultName(p.default);
    } catch (e) {
      setError(errLabel(e));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Set of soul slugs referenced by any loaded persona (used to disable delete).
  const inUseSouls = new Map<string, number>();
  for (const p of personas ?? []) {
    if (p.soul) {
      inUseSouls.set(p.soul, (inUseSouls.get(p.soul) ?? 0) + 1);
    }
  }

  const removeSoul = async (slug: string) => {
    setBusyId(slug);
    try {
      await deleteSoul(token, slug);
      setConfirmSoul(null);
      await refresh();
    } catch (e) {
      setError(errLabel(e));
    } finally {
      setBusyId(null);
    }
  };

  const removePersona = async (name: string) => {
    setBusyId(name);
    try {
      await deletePersona(token, name);
      setConfirmPersona(null);
      await refresh();
    } catch (e) {
      setError(errLabel(e));
    } finally {
      setBusyId(null);
    }
  };

  const changeDefault = async (name: string | null) => {
    setBusyId(name ?? "__clear__");
    try {
      await setDefaultPersona(token, name);
      await refresh();
    } catch (e) {
      setError(errLabel(e));
    } finally {
      setBusyId(null);
    }
  };

  const testRow = async (persona: PersonaItem) => {
    setTestingRow(persona.name);
    setRowTest(null);
    try {
      const res = await testPersona(token, { model: persona.model ?? null, soul: persona.soul });
      setRowTest({ name: persona.name, res });
    } catch (e) {
      setRowTest({ name: persona.name, res: { ok: false, error: errLabel(e) } });
    } finally {
      setTestingRow(null);
    }
  };

  const handleSoulDone = async () => {
    setSoulForm(null);
    await refresh();
  };
  const handlePersonaDone = async () => {
    setPersonaForm(null);
    await refresh();
  };

  return (
    <div className="space-y-8">
      {error ? (
        <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      {/* ── SOUL library ─────────────────────────────────────────── */}
      <section>
        <div className="mb-2 flex items-center justify-between px-1">
          <SettingsSectionTitle>{t("settings.personas.soulsTitle")}</SettingsSectionTitle>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setSoulForm("new")}
            className="h-7 gap-1 text-[12px]"
          >
            <Plus className="h-3.5 w-3.5" aria-hidden />
            {t("settings.personas.newSoul")}
          </Button>
        </div>
        <p className="px-1 pb-3 text-[12px] text-muted-foreground">
          {t("settings.personas.soulsDescription")}
        </p>

        {soulForm !== null ? (
          <div className="mb-4 overflow-hidden rounded-[22px] border border-border/45 bg-card/86 shadow-[0_18px_65px_rgba(15,23,42,0.075)] backdrop-blur-xl dark:border-white/10">
            <div className="border-b border-border/45 px-5 py-2.5 text-[12px] font-semibold text-foreground/70">
              {soulForm === "new"
                ? t("settings.personas.newSoul")
                : t("settings.personas.editSoul")}
            </div>
            <SoulForm
              token={token}
              editSoul={soulForm === "new" ? null : soulForm}
              onDone={() => void handleSoulDone()}
              onCancel={() => setSoulForm(null)}
            />
          </div>
        ) : null}

        <SettingsGroup>
          {loading ? (
            <SettingsRow title={t("settings.personas.soulsLoading")}>
              <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-hidden />
            </SettingsRow>
          ) : !souls || souls.length === 0 ? (
            <SettingsRow title={t("settings.personas.soulsEmpty")}>
              <span className="text-[12px] text-muted-foreground">
                {t("settings.personas.soulsEmptyHint")}
              </span>
            </SettingsRow>
          ) : (
            souls.map((soul) => {
              const isDefault = soul.slug === DEFAULT_SOUL_SLUG;
              const useCount = inUseSouls.get(soul.slug) ?? 0;
              const isInUse = useCount > 0;
              const deleteDisabled = isDefault || isInUse;
              return (
                <SettingsRow
                  key={soul.slug}
                  title={
                    <span className="flex items-center gap-2">
                      <ScrollText className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
                      <span className="font-mono">{soul.slug}</span>
                      {isDefault ? (
                        <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                          {t("settings.personas.defaultBadge")}
                        </span>
                      ) : null}
                      {isInUse ? (
                        <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                          {t("settings.personas.inUseBy", { count: useCount })}
                        </span>
                      ) : null}
                    </span>
                  }
                >
                  <div className="flex items-center gap-2">
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busyId === soul.slug}
                      onClick={() => setSoulForm(soul)}
                      className="rounded-full text-muted-foreground"
                      title={t("settings.personas.editSoul")}
                      aria-label={t("settings.personas.editSoul")}
                    >
                      <Pencil className="h-3.5 w-3.5" aria-hidden />
                    </Button>
                    <InlineDelete
                      confirming={confirmSoul === soul.slug}
                      busy={busyId === soul.slug}
                      disabled={deleteDisabled}
                      onAsk={() => setConfirmSoul(soul.slug)}
                      onConfirm={() => void removeSoul(soul.slug)}
                      onCancel={() => setConfirmSoul(null)}
                    />
                  </div>
                </SettingsRow>
              );
            })
          )}
        </SettingsGroup>
      </section>

      {/* ── Personas ─────────────────────────────────────────────── */}
      <section>
        <div className="mb-2 flex items-center justify-between px-1">
          <SettingsSectionTitle>{t("settings.personas.personasTitle")}</SettingsSectionTitle>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setPersonaForm("new")}
            className="h-7 gap-1 text-[12px]"
          >
            <Plus className="h-3.5 w-3.5" aria-hidden />
            {t("settings.personas.newPersona")}
          </Button>
        </div>
        <p className="px-1 pb-3 text-[12px] text-muted-foreground">
          {t("settings.personas.personasDescription")}
        </p>

        {personaForm !== null ? (
          <div className="mb-4 overflow-hidden rounded-[22px] border border-border/45 bg-card/86 shadow-[0_18px_65px_rgba(15,23,42,0.075)] backdrop-blur-xl dark:border-white/10">
            <div className="border-b border-border/45 px-5 py-2.5 text-[12px] font-semibold text-foreground/70">
              {personaForm === "new"
                ? t("settings.personas.newPersona")
                : t("settings.personas.editPersona")}
            </div>
            <PersonaForm
              token={token}
              editPersona={personaForm === "new" ? null : personaForm}
              souls={souls ?? []}
              onDone={() => void handlePersonaDone()}
              onCancel={() => setPersonaForm(null)}
            />
          </div>
        ) : null}

        <SettingsGroup>
          {loading ? (
            <SettingsRow title={t("settings.personas.personasLoading")}>
              <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-hidden />
            </SettingsRow>
          ) : !personas || personas.length === 0 ? (
            <SettingsRow title={t("settings.personas.personasEmpty")}>
              <span className="text-[12px] text-muted-foreground">
                {t("settings.personas.personasEmptyHint")}
              </span>
            </SettingsRow>
          ) : (
            personas.map((persona) => {
              const isDefault = persona.name === defaultPersona;
              const busy = busyId === persona.name;
              return (
                <SettingsRow
                  key={persona.name}
                  title={
                    <span className="flex items-center gap-2">
                      <Drama className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
                      <span>{persona.name}</span>
                      {persona.builtin && persona.name !== "default" ? (
                        <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
                          {t("settings.personas.builtinBadge")}
                        </span>
                      ) : null}
                      {isDefault ? (
                        <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-primary">
                          <Star className="h-2.5 w-2.5 fill-current" aria-hidden />
                          {t("settings.personas.defaultMarker")}
                        </span>
                      ) : null}
                    </span>
                  }
                  description={
                    <span className="flex flex-col gap-0.5 text-[11px] text-muted-foreground">
                      <span className="font-mono">
                        {t("settings.personas.fieldSoul")}: {persona.soul}
                        {" · "}{persona.model || t("settings.personas.modelDefault")}
                      </span>
                      {persona.description ? <span>{persona.description}</span> : null}
                      {rowTest?.name === persona.name ? (
                        <span
                          className={cn(
                            "mt-1 block whitespace-pre-wrap rounded-md border px-2 py-1.5 text-[11px] leading-relaxed",
                            rowTest.res.ok
                              ? "border-green-500/20 bg-green-500/5 text-foreground"
                              : "border-destructive/20 bg-destructive/5 text-destructive",
                          )}
                        >
                          {rowTest.res.ok ? (rowTest.res.reply ?? "") : (rowTest.res.error ?? "")}
                        </span>
                      ) : null}
                    </span>
                  }
                >
                  <div className="flex items-center gap-2">
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={testingRow === persona.name}
                      onClick={() => void testRow(persona)}
                      className="rounded-full text-[12px] text-muted-foreground"
                      title={t("settings.personas.test")}
                    >
                      {testingRow === persona.name ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                      ) : (
                        t("settings.personas.test")
                      )}
                    </Button>
                    {!isDefault ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={busy}
                        onClick={() => void changeDefault(persona.name)}
                        className="rounded-full text-muted-foreground"
                        title={t("settings.personas.setDefault")}
                        aria-label={t("settings.personas.setDefault")}
                      >
                        <Star className="h-3.5 w-3.5" aria-hidden />
                      </Button>
                    ) : null}
                    {persona.builtin ? null : (
                      <>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={busy}
                          onClick={() => setPersonaForm(persona)}
                          className="rounded-full text-muted-foreground"
                          title={t("settings.personas.editPersona")}
                          aria-label={t("settings.personas.editPersona")}
                        >
                          <Pencil className="h-3.5 w-3.5" aria-hidden />
                        </Button>
                        <InlineDelete
                          confirming={confirmPersona === persona.name}
                          busy={busy}
                          onAsk={() => setConfirmPersona(persona.name)}
                          onConfirm={() => void removePersona(persona.name)}
                          onCancel={() => setConfirmPersona(null)}
                        />
                      </>
                    )}
                  </div>
                </SettingsRow>
              );
            })
          )}
        </SettingsGroup>
      </section>
    </div>
  );
}
