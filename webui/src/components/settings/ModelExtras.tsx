import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  getConfig,
  setConfigValue,
  testModel,
  type ModelTestResult,
} from "@/lib/api";

interface AuxModel {
  model: string;
  provider: string;
}

function readAux(config: Record<string, unknown> | null, kind: string): AuxModel | null {
  const agents = config?.agents as Record<string, unknown> | undefined;
  const aux = agents?.auxModels as Record<string, unknown> | undefined;
  const entry = aux?.[kind] as Record<string, unknown> | undefined;
  if (!entry || typeof entry.model !== "string") return null;
  return {
    model: entry.model,
    provider: typeof entry.provider === "string" ? entry.provider : "auto",
  };
}

/** One aux-model row (vision or audio): model + provider, save / clear. */
function AuxModelRow({
  label,
  hint,
  current,
  busy,
  onSave,
  onClear,
}: {
  label: string;
  hint: string;
  current: AuxModel | null;
  busy: boolean;
  onSave: (value: AuxModel) => void;
  onClear: () => void;
}) {
  const { t } = useTranslation();
  const [model, setModel] = useState(current?.model ?? "");
  const [provider, setProvider] = useState(current?.provider ?? "auto");
  useEffect(() => {
    setModel(current?.model ?? "");
    setProvider(current?.provider ?? "auto");
  }, [current]);

  const dirty =
    model.trim() !== (current?.model ?? "") ||
    provider.trim() !== (current?.provider ?? (current ? "auto" : ""));

  return (
    <div className="flex flex-col gap-2 px-4 py-3.5 sm:px-5">
      <div className="text-[14px] font-medium text-foreground">{label}</div>
      <div className="text-[12px] text-muted-foreground">{hint}</div>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <Input
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder={t("settings.models.modelPlaceholder")}
          className="w-[220px]"
        />
        <Input
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          placeholder={t("settings.models.providerPlaceholder")}
          className="w-[150px]"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={!dirty || busy || !model.trim()}
          onClick={() => onSave({ model: model.trim(), provider: provider.trim() || "auto" })}
          className="rounded-full"
        >
          {t("settings.models.save")}
        </Button>
        {current ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={onClear}
            className="rounded-full text-muted-foreground"
          >
            {t("settings.models.clear")}
          </Button>
        ) : null}
      </div>
    </div>
  );
}

/** General's model extras: vision / audio aux models + a real
 *  round-trip test of the configured model. */
export function ModelExtras({ token }: { token: string }) {
  const { t } = useTranslation();
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [test, setTest] = useState<ModelTestResult | null>(null);
  const [testing, setTesting] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const snap = await getConfig(token);
      setConfig(snap.config);
    } catch {
      setError(t("settings.models.loadError"));
    } finally {
      setLoading(false);
    }
  }, [token, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const saveAux = useCallback(
    async (kind: string, value: AuxModel | null) => {
      setBusy(kind);
      setError(null);
      try {
        const next = await setConfigValue(token, `agents.auxModels.${kind}`, value);
        setConfig(next);
      } catch {
        setError(t("settings.models.saveError"));
      } finally {
        setBusy(null);
      }
    },
    [token, t],
  );

  const runTest = useCallback(async () => {
    setTesting(true);
    setTest(null);
    try {
      setTest(await testModel(token));
    } catch {
      setTest({ status: "fail", message: t("settings.models.testError"), fix: "" });
    } finally {
      setTesting(false);
    }
  }, [token, t]);

  if (loading) return null;

  return (
    <div className="space-y-5">
      <section>
        <h2 className="mb-2 px-1 text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
          {t("settings.models.auxTitle")}
        </h2>
        <p className="mb-2 px-1 text-[12px] leading-5 text-muted-foreground">
          {t("settings.models.auxDescription")}
        </p>
        <div className="overflow-hidden rounded-[22px] border border-border/45 bg-card/86">
          <div className="divide-y divide-border/45">
            <AuxModelRow
              label={t("settings.models.vision")}
              hint={t("settings.models.visionHint")}
              current={readAux(config, "vision")}
              busy={busy === "vision"}
              onSave={(v) => void saveAux("vision", v)}
              onClear={() => void saveAux("vision", null)}
            />
            <AuxModelRow
              label={t("settings.models.audio")}
              hint={t("settings.models.audioHint")}
              current={readAux(config, "audio")}
              busy={busy === "audio"}
              onSave={(v) => void saveAux("audio", v)}
              onClear={() => void saveAux("audio", null)}
            />
          </div>
        </div>
      </section>

      <section>
        <h2 className="mb-2 px-1 text-[13px] font-semibold tracking-[-0.01em] text-foreground/85">
          {t("settings.models.testTitle")}
        </h2>
        <div className="overflow-hidden rounded-[22px] border border-border/45 bg-card/86">
          <div className="flex min-h-[58px] items-center justify-between gap-3 px-4 py-3 sm:px-5">
            <div className="min-w-0">
              {test ? (
                <div
                  className={
                    test.status === "ok"
                      ? "flex items-center gap-1.5 text-[13px] text-emerald-600 dark:text-emerald-400"
                      : "flex items-center gap-1.5 text-[13px] text-destructive"
                  }
                >
                  {test.status === "ok" ? (
                    <CheckCircle2 className="h-4 w-4" aria-hidden />
                  ) : (
                    <XCircle className="h-4 w-4" aria-hidden />
                  )}
                  <span className="truncate">{test.message}</span>
                </div>
              ) : (
                <span className="text-[13px] text-muted-foreground">
                  {t("settings.models.testHint")}
                </span>
              )}
            </div>
            <Button
              size="sm"
              variant="outline"
              disabled={testing}
              onClick={() => void runTest()}
              className="shrink-0 rounded-full"
            >
              {testing ? (
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : null}
              {testing ? t("settings.models.testing") : t("settings.models.testButton")}
            </Button>
          </div>
        </div>
      </section>

      {error ? (
        <div className="rounded-[18px] border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}
    </div>
  );
}
